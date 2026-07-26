import pytest
from mship.core.evidence_store import EvidenceModeError, resolve_evidence_mode


class _Cfg:
    def __init__(self, spec_storage, evidence_storage=None):
        self.spec_storage = spec_storage
        self.evidence_storage = evidence_storage


def test_unset_inherits_spec_storage():
    for mode in ("committed", "local", "encrypted"):
        assert resolve_evidence_mode(_Cfg(mode)) == mode


def test_explicit_mode_wins_when_not_more_exposed():
    assert resolve_evidence_mode(_Cfg("committed", "local")) == "local"
    assert resolve_evidence_mode(_Cfg("encrypted", "local")) == "local"
    assert resolve_evidence_mode(_Cfg("encrypted", "encrypted")) == "encrypted"


def test_evidence_more_exposed_than_spec_is_refused():
    with pytest.raises(EvidenceModeError) as e:
        resolve_evidence_mode(_Cfg("encrypted", "committed"))
    msg = str(e.value)
    assert "evidence_storage" in msg and "spec_storage" in msg


def test_local_spec_with_committed_evidence_is_refused():
    with pytest.raises(EvidenceModeError):
        resolve_evidence_mode(_Cfg("local", "committed"))
