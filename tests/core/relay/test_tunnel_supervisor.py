from mship.core.relay.tunnel import TunnelSupervisor


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
