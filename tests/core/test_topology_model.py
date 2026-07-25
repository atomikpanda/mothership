import json

from mship.core.topology import (
    SCHEMA_VERSION,
    STATUSES,
    Edge,
    Topology,
    topology_payload,
)


def _edge(**kw):
    base = dict(
        kind="relay", name="relay", status="fail",
        code="relay_unreachable", detail="could not reach https://x/health",
        fix="check the relay is up", facts={"host": "relay.example.com"},
    )
    base.update(kw)
    return Edge(**base)


def test_payload_carries_schema_version_and_edges():
    t = Topology(
        version=SCHEMA_VERSION,
        workspace="mship-workspace",
        probed_at="2026-07-25T16:00:00+00:00",
        edges=[_edge()],
    )
    payload = topology_payload(t)

    assert payload["version"] == SCHEMA_VERSION
    assert payload["workspace"] == "mship-workspace"
    assert payload["probed_at"] == "2026-07-25T16:00:00+00:00"
    assert payload["edges"][0] == {
        "kind": "relay",
        "name": "relay",
        "status": "fail",
        "code": "relay_unreachable",
        "detail": "could not reach https://x/health",
        "fix": "check the relay is up",
        "facts": {"host": "relay.example.com"},
    }


def test_payload_is_json_serializable():
    t = Topology(version=SCHEMA_VERSION, workspace="w", probed_at="t", edges=[_edge()])
    assert json.loads(json.dumps(topology_payload(t)))["version"] == SCHEMA_VERSION


def test_healthy_edge_has_no_fix():
    e = _edge(status="ok", code="relay_ok", fix=None)
    assert topology_payload(
        Topology(version=SCHEMA_VERSION, workspace="w", probed_at="t", edges=[e])
    )["edges"][0]["fix"] is None


def test_status_values_are_constrained():
    # The four statuses a caller may branch on; `absent` means "not configured
    # on this machine", which is not a problem to fix.
    assert STATUSES == ("ok", "warn", "fail", "absent")
