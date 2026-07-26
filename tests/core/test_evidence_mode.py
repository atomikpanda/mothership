"""Resolving the effective evidence mode.

The two fields speak slightly different vocabularies on purpose: a spec is
`committed` into the workspace repo's tracked tree, while an artifact is
`published` onto an orphan branch in the repo whose PR embeds it. Inheritance
therefore MAPS rather than copies, and the exposure comparison has to happen in
the evidence vocabulary — a `spec_storage: committed` workspace must accept
`evidence_storage: published`, not reject it as "more exposed".
"""
import pytest
from mship.core.evidence_store import EvidenceModeError, resolve_evidence_mode


class _Cfg:
    def __init__(self, spec_storage, evidence_storage=None):
        self.spec_storage = spec_storage
        self.evidence_storage = evidence_storage


def test_unset_inherits_spec_storage_mapping_committed_to_published():
    assert resolve_evidence_mode(_Cfg("committed")) == "published"
    assert resolve_evidence_mode(_Cfg("local")) == "local"
    assert resolve_evidence_mode(_Cfg("encrypted")) == "encrypted"


def test_explicit_mode_wins_when_not_more_exposed():
    assert resolve_evidence_mode(_Cfg("committed", "local")) == "local"
    assert resolve_evidence_mode(_Cfg("encrypted", "local")) == "local"
    assert resolve_evidence_mode(_Cfg("encrypted", "encrypted")) == "encrypted"


def test_published_evidence_under_a_committed_spec_is_the_equal_case():
    """The one the mapping exists for: same exposure, different word."""
    assert resolve_evidence_mode(_Cfg("committed", "published")) == "published"


def test_evidence_more_exposed_than_spec_is_refused():
    with pytest.raises(EvidenceModeError) as e:
        resolve_evidence_mode(_Cfg("encrypted", "published"))
    msg = str(e.value)
    assert "evidence_storage" in msg and "spec_storage" in msg


def test_local_spec_with_published_evidence_is_refused():
    with pytest.raises(EvidenceModeError):
        resolve_evidence_mode(_Cfg("local", "published"))
