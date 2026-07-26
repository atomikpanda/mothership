"""Image evidence in the PR body: embedded when GitHub can fetch the bytes,
named when it cannot.

There is no public API for uploading an image attachment to a PR, so an embed
only works when the artifact already lives somewhere GitHub's renderer can
reach — i.e. published to the target repo's evidence branch. This is the pure
half: `build_acceptance_block` deciding what to embed and what to name, given a
base URL. Whether such a URL exists at all is the publication question, covered
against real git in test_finish_evidence_publish.py.
"""
from datetime import datetime, timezone

from mship.core.pr import build_acceptance_block
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec

# A published-commit base: `<owner>/<repo>/<orphan-commit-sha>`, under which
# an artifact lives at `<spec-id>/<ref>`.
BASE = "https://raw.githubusercontent.com/o/r/abc123"
IMAGE_REF = "a1b2c3d4e5f6.png"


def _spec_with(*evidence: AcceptanceEvidence) -> Spec:
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    return Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac1", text="the screen renders", verdict="approved",
                evidence=list(evidence),
            )
        ],
    )


# --- rendering -------------------------------------------------------------


def test_image_artifact_is_embedded_when_a_base_url_is_available():
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF)),
        evidence_base_url=BASE,
    )
    assert f"![ac1]({BASE}/my-spec/{IMAGE_REF})" in body


def test_without_a_base_url_the_artifact_is_named_not_embedded():
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF)),
        evidence_base_url=None,
    )
    assert "![" not in body
    assert IMAGE_REF in body


def test_non_image_artifact_is_never_embedded():
    ref = "a1b2c3d4e5f6.xml"
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=ref)),
        evidence_base_url=BASE,
    )
    assert "![" not in body
    assert f"artifact:{ref}" in body


def test_encrypted_artifact_is_never_embedded():
    """The bytes on GitHub are ciphertext, so an embed would render broken."""
    ref = f"{IMAGE_REF}.enc"
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=ref)),
        evidence_base_url=BASE,
    )
    assert "![" not in body
    assert f"artifact:{ref}" in body


def test_artifact_ref_that_is_not_a_stored_ref_is_never_embedded():
    """Only refs the evidence store produced resolve under the base URL; a
    hand-written path would embed a URL that 404s."""
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref="docs/shot.png")),
        evidence_base_url=BASE,
    )
    assert "![" not in body
    assert "artifact:docs/shot.png" in body


def test_test_and_commit_refs_render_as_before():
    body = build_acceptance_block(
        _spec_with(
            AcceptanceEvidence(kind="test", ref="test-runs/7"),
            AcceptanceEvidence(kind="commit", ref="deadbee"),
        ),
        evidence_base_url=BASE,
    )
    assert "- [x] `ac1` the screen renders — test:test-runs/7, commit:deadbee" in body
    assert "![" not in body


def test_embedded_image_sits_on_its_own_line_beneath_the_criterion():
    body = build_acceptance_block(
        _spec_with(
            AcceptanceEvidence(kind="test", ref="test-runs/7"),
            AcceptanceEvidence(kind="artifact", ref=IMAGE_REF),
        ),
        evidence_base_url=BASE,
    )
    lines = body.splitlines()
    i = lines.index("- [x] `ac1` the screen renders — test:test-runs/7")
    assert lines[i + 1] == ""
    assert lines[i + 2] == f"  ![ac1]({BASE}/my-spec/{IMAGE_REF})"


def test_criterion_with_no_evidence_is_unchanged():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    spec = Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="does X")],
    )
    assert "- [ ] `ac1` does X — _no evidence_" in build_acceptance_block(
        spec, evidence_base_url=BASE
    )


def test_default_call_still_names_artifacts():
    """Existing callers pass no base URL and must be unaffected."""
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    )
    assert "![" not in body
    assert f"artifact:{IMAGE_REF}" in body
