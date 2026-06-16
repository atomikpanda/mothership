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
