from mship.core.relay.tunnel import BACKOFF_JITTER, TunnelSupervisor


class FakeProc:
    def __init__(self): self._alive = True; self.terminated = False
    def poll(self): return None if self._alive else 0
    def terminate(self): self.terminated = True; self._alive = False
    def wait(self, timeout=None): self._alive = False; return 0


def test_start_then_stop_terminates_process():
    procs = []
    def factory(argv): p = FakeProc(); procs.append(p); return p
    sup = TunnelSupervisor(argv=["ssh", "..."], proc_factory=factory)
    sup.start()
    assert sup.is_running() and len(procs) == 1
    sup.stop()
    assert procs[0].terminated and not sup.is_running()


def test_restart_on_unexpected_exit():
    """tick() respawns the process when it exits unexpectedly (not via stop())."""
    procs = []
    def factory(argv): p = FakeProc(); procs.append(p); return p

    sup = TunnelSupervisor(argv=["ssh", "..."], proc_factory=factory, backoff_delay=0)
    sup.start()
    assert len(procs) == 1 and sup.is_running()

    # Simulate unexpected process exit (non-None poll return = process died)
    procs[0]._alive = False

    # tick() should detect the exit and spawn a replacement
    sup.tick()

    assert len(procs) == 2, "supervisor should have spawned a replacement proc"
    assert sup.is_running(), "supervisor should report running after respawn"


def test_backoff_gates_respawn():
    """tick() must NOT respawn until the backoff delay has elapsed."""
    procs = []
    def factory(argv): p = FakeProc(); procs.append(p); return p

    t = [0.0]
    def fake_clock(): return t[0]

    sup = TunnelSupervisor(
        argv=["ssh", "..."],
        proc_factory=factory,
        backoff_delay=1.0,
        clock=fake_clock,
    )
    sup.start()
    assert len(procs) == 1

    # Kill the process
    procs[0]._alive = False

    # tick() at t=0: delay=1.0, elapsed=0.0 → must NOT respawn
    t[0] = 0.0
    sup.tick()
    assert len(procs) == 1, "should NOT have respawned before backoff delay elapsed"

    # Advance clock past the backoff delay
    t[0] = 1.5
    sup.tick()
    assert len(procs) == 2, "should have respawned after backoff delay elapsed"
    assert sup.is_running()


# --- #471: an immortal daemon owns this supervisor, so the backoff must be
# --- clamped, jittered from an injected RNG, and reset by a healthy run.

def _sup(procs, clock, **kw):
    def factory(argv): p = FakeProc(); procs.append(p); return p
    kw.setdefault("backoff_delay", 5.0)
    kw.setdefault("max_backoff_delay", 60.0)
    kw.setdefault("rng", lambda: 0.0)          # no jitter unless a test asks
    return TunnelSupervisor(argv=["ssh", "..."], proc_factory=factory,
                            clock=lambda: clock[0], **kw)


def test_backoff_is_clamped_past_1024_restarts():
    """`backoff_delay * 2 ** restart_count` overflows a float at 1024 — ~17h of
    failures, unreachable for a CLI and routine for a daemon that never exits."""
    procs, t = [], [0.0]
    sup = _sup(procs, t)
    sup.start()
    sup._restart_count = 2000                  # what 17h of flapping leaves behind

    procs[0]._alive = False
    sup.tick()                                 # must not raise OverflowError
    assert len(procs) == 1, "capped delay still gates the respawn"

    t[0] = 60.0                                # the cap, not 5 * 2**2000
    sup.tick()
    assert len(procs) == 2
    assert sup.restart_count == 2001


def test_a_healthy_run_resets_the_restart_count():
    """A tunnel that held for longer than the worst-case backoff ended a failure
    streak; the next drop must retry fast rather than at the cap."""
    procs, t = [], [0.0]
    sup = _sup(procs, t)
    sup.start()
    sup._restart_count = 9

    t[0] = 500.0                               # the child held for 500s
    procs[0]._alive = False
    sup.tick()
    assert sup.restart_count == 0, "the streak ended when the run went healthy"

    t[0] = 505.0                               # one base delay later, not the cap
    sup.tick()
    assert len(procs) == 2 and sup.restart_count == 1


def test_a_short_run_does_not_reset_the_restart_count():
    procs, t = [], [0.0]
    sup = _sup(procs, t)
    sup.start()
    sup._restart_count = 3

    t[0] = 2.0                                 # died well inside the backoff cap
    procs[0]._alive = False
    sup.tick()
    assert sup.restart_count == 3


def test_jitter_comes_from_the_injected_rng_and_only_shortens():
    """Downward only: `MAX_BACKOFF_S` stays the true maximum, which is what
    `host_contract.DIRECTORY_STALE_S` is derived from."""
    for value in (0.0, 0.5, 1.0):
        procs, t = [], [0.0]
        sup = _sup(procs, t, rng=lambda v=value: v)
        sup.start()
        sup._restart_count = 2000
        procs[0]._alive = False

        sup.tick()                             # freezes the jittered delay
        delay = sup.next_delay()
        assert 60.0 * (1 - BACKOFF_JITTER) <= delay <= 60.0

        t[0] = delay - 0.001
        sup.tick()
        assert len(procs) == 1, "respawned before its own jittered delay"
        t[0] = delay
        sup.tick()
        assert len(procs) == 2


def test_two_supervisors_do_not_respawn_on_the_same_tick():
    """A fleet that all lost the relay in one second must not stampede it."""
    t = [0.0]
    slow_procs, fast_procs = [], []
    slow = _sup(slow_procs, t, rng=lambda: 0.0)     # full delay
    fast = _sup(fast_procs, t, rng=lambda: 1.0)     # 20% off it
    for sup, procs in ((slow, slow_procs), (fast, fast_procs)):
        sup.start()
        sup._restart_count = 2000
        procs[0]._alive = False
        sup.tick()

    t[0] = 60.0 * (1 - BACKOFF_JITTER)
    slow.tick(); fast.tick()
    assert len(fast_procs) == 2 and len(slow_procs) == 1
