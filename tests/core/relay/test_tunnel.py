from pathlib import Path

from mship.core.relay.tunnel import TunnelSupervisor, device_id, device_subdomain, subdomain_for

_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAA mship-relay\n"


def test_device_id_is_stable_short_hex():
    a = device_id(_PUBKEY)
    assert a == device_id(_PUBKEY)              # stable
    assert len(a) == 6 and all(c in "0123456789abcdef" for c in a)


def test_device_id_ignores_comment_and_whitespace():
    # same key body, different trailing comment/whitespace → same id
    assert device_id(_PUBKEY) == device_id("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAA other-comment")


def test_device_id_differs_per_key():
    assert device_id(_PUBKEY) != device_id("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDifferentBodyZZZZ x")


def test_device_subdomain_appends_id_and_is_dns_safe():
    sd = device_subdomain("mship-workspace", "abc123")
    assert sd == "mship-workspace-abc123"
    assert len(sd) <= 63 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in sd)


def test_device_subdomain_truncates_to_dns_limit():
    sd = device_subdomain("w" * 80, "abc123")
    assert len(sd) <= 63
    assert sd.endswith("-abc123")
    assert not sd.startswith("-") and "--" not in sd


# ---------------------------------------------------------------------------
# TunnelSupervisor: log capture + accessors (Task 2)
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, exits_after=0):
        self._polls = 0
        self._exits_after = exits_after

    def poll(self):
        self._polls += 1
        return None if self._polls <= self._exits_after else 1

    def terminate(self): pass

    def wait(self, timeout=None): return 0


def test_supervisor_exposes_recent_output(tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_text("Warning: remote port forwarding failed for listen port 80\n")
    sup = TunnelSupervisor(argv=["ssh"], proc_factory=lambda a: _FakeProc(0), log_path=log)
    sup.start()
    assert "remote port forwarding failed" in sup.recent_output()


def test_supervisor_counts_restarts(tmp_path):
    # proc that is always "dead" → tick respawns once backoff elapses
    sup = TunnelSupervisor(argv=["ssh"], proc_factory=lambda a: _FakeProc(0),
                           backoff_delay=0.0, log_path=tmp_path / "t.log")
    sup.start()
    assert sup.restart_count == 0
    sup.tick(); sup.tick()
    assert sup.restart_count >= 1
