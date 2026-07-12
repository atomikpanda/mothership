from mship.core.spec_draft import build_draft_prompt


def test_build_draft_prompt_contains_intent_schema_and_apply():
    prompt = build_draft_prompt("decision-queue", "I want X away from the desk")
    assert "decision-queue" in prompt
    assert "I want X away from the desk" in prompt          # the intent
    assert "acceptance_criteria" in prompt                  # the JSON shape
    assert "open_questions" in prompt
    assert "mship spec apply decision-queue --from-json" in prompt  # how to apply
    assert "only" in prompt.lower()                         # "output only JSON"


from datetime import datetime, timezone

from mship.core.spec import (
    AcceptanceCriterion,
    AcceptanceEvidence,
    Spec,
    SpecDraft,
)
from mship.core.spec_draft import apply_draft
from mship.core.spec_body import validate_body_structure


def _spec():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(id="dq", title="DQ", status="draft", created_at=now, updated_at=now,
                task_slug="dq")


def test_apply_draft_merges_fields_and_assigns_ids():
    spec = _spec()
    draft = SpecDraft(
        problem="P", user_story="U", approach="A",
        non_goals=["chat"], risks=["scope"], affected_repos=["mothership"],
        acceptance_criteria=["view questions", "record answer"],
        open_questions=["Android in v0?"],
    )
    out = apply_draft(spec, draft)
    assert validate_body_structure(out.body) == []          # canonical body rendered
    assert out.non_goals == ["chat"] and out.affected_repos == ["mothership"]
    assert [c.id for c in out.acceptance_criteria] == ["ac1", "ac2"]
    assert out.acceptance_criteria[0].text == "view questions"
    assert out.acceptance_criteria[0].verdict == "unreviewed"
    assert [q.id for q in out.open_questions] == ["q1"]
    assert out.open_questions[0].answer is None
    assert out.id == "dq" and out.task_slug == "dq"         # identity preserved


def test_apply_draft_preserves_evidence_and_verdict_for_unchanged_ac():
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="view questions", verdict="approved",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/5")],
        ),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["view questions"])   # SAME text
    out = apply_draft(spec, draft)
    assert out.acceptance_criteria[0].verdict == "approved"     # preserved
    assert out.acceptance_criteria[0].evidence == [AcceptanceEvidence(kind="test", ref="test-runs/5")]


def test_apply_draft_resets_evidence_and_verdict_for_materially_changed_ac():
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="view questions", verdict="approved",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/5")],
        ),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["view questions AND record answers"])  # CHANGED
    out = apply_draft(spec, draft)
    assert out.acceptance_criteria[0].verdict == "unreviewed"   # fresh
    assert out.acceptance_criteria[0].evidence == []            # fresh


def test_apply_draft_preserves_evidence_across_insert_and_reorder():
    # Greptile #339: preservation is by TEXT, not positional id — inserting a NEW
    # criterion ahead of unchanged ones (which shifts their ac{i+1} ids) must NOT
    # reset the unchanged ones' evidence/verdict.
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="A", verdict="approved",
                            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1")]),
        AcceptanceCriterion(id="ac2", text="B", verdict="flagged",
                            evidence=[AcceptanceEvidence(kind="commit", ref="deadbeef")]),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["NEW", "A", "B"])   # NEW inserted first
    out = apply_draft(spec, draft)
    ids_texts = [(c.id, c.text) for c in out.acceptance_criteria]
    assert ids_texts == [("ac1", "NEW"), ("ac2", "A"), ("ac3", "B")]
    assert out.acceptance_criteria[0].verdict == "unreviewed"          # NEW: fresh
    assert out.acceptance_criteria[0].evidence == []
    assert out.acceptance_criteria[1].verdict == "approved"            # A: preserved despite id shift
    assert out.acceptance_criteria[1].evidence == [AcceptanceEvidence(kind="test", ref="test-runs/1")]
    assert out.acceptance_criteria[2].verdict == "flagged"             # B: preserved despite id shift
    assert out.acceptance_criteria[2].evidence == [AcceptanceEvidence(kind="commit", ref="deadbeef")]


def test_apply_draft_duplicate_text_preserved_positionally_then_fresh():
    # Duplicate text: the exact-id+text pass (pass 1) preserves each prior dup at its
    # own position; a genuinely new duplicate (no prior at that id) starts fresh.
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="dup", verdict="approved",
                            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1")]),
        AcceptanceCriterion(id="ac2", text="dup", verdict="flagged",
                            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/2")]),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["dup", "dup", "dup"])
    out = apply_draft(spec, draft)
    assert [c.evidence for c in out.acceptance_criteria] == [
        [AcceptanceEvidence(kind="test", ref="test-runs/1")],   # ac1 exact-matched
        [AcceptanceEvidence(kind="test", ref="test-runs/2")],   # ac2 exact-matched
        [],                                                      # ac3 new → fresh
    ]
    assert [c.verdict for c in out.acceptance_criteria] == ["approved", "flagged", "unreviewed"]


def test_apply_draft_edit_into_text_collision_keeps_unchanged_and_never_moves_evidence():
    # Greptile #339 findings 2 ("Evidence Can Move") + 3 ("Unchanged Duplicate Loses
    # Evidence"), which are in tension. Prior: ac1 "view"(A), ac2 "edit"(B). Edit ac1
    # "view" → "edit" so the draft is ["edit", "edit"]. Correct outcome:
    #   - the UNCHANGED ac2 "edit" keeps its evidence B (finding 3), matched exactly
    #     by id+text in pass 1;
    #   - the EDITED criterion (now at ac1) does NOT receive B (finding 2) — pass 2
    #     finds no remaining "edit" prior, so it starts fresh.
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="view", verdict="approved",
                            evidence=[AcceptanceEvidence(kind="test", ref="A")]),
        AcceptanceCriterion(id="ac2", text="edit", verdict="flagged",
                            evidence=[AcceptanceEvidence(kind="commit", ref="B")]),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["edit", "edit"])
    out = apply_draft(spec, draft)
    # ac1 (the edited criterion) is fresh — evidence B was NOT moved onto it.
    assert out.acceptance_criteria[0].evidence == []
    assert out.acceptance_criteria[0].verdict == "unreviewed"
    # ac2 (unchanged) keeps its verdict + evidence B.
    assert out.acceptance_criteria[1].evidence == [AcceptanceEvidence(kind="commit", ref="B")]
    assert out.acceptance_criteria[1].verdict == "flagged"
    # Evidence A ("view", now gone) is not present anywhere; B appears exactly once.
    all_refs = [e.ref for c in out.acceptance_criteria for e in c.evidence]
    assert all_refs == ["B"]


def test_apply_draft_insert_plus_collision_tiebreak_is_positional_and_lossless():
    # Documents the one residual ambiguity (insert + edit-into-collision). Prior:
    # ac1 "A"(E1), ac2 "B"(E2). Draft inserts "NEW" and edits "A"→"B", giving
    # ["NEW","B","B"] — two "B"s but only one prior "B". The single E2 is preserved
    # exactly once (never duplicated, never dropped) and lands on a criterion whose
    # text is "B"; the id-aligned "B" (pass-1 exact match) wins the tie-break.
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="A", verdict="approved",
                            evidence=[AcceptanceEvidence(kind="test", ref="E1")]),
        AcceptanceCriterion(id="ac2", text="B", verdict="flagged",
                            evidence=[AcceptanceEvidence(kind="commit", ref="E2")]),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["NEW", "B", "B"])
    out = apply_draft(spec, draft)
    assert [(c.id, c.text) for c in out.acceptance_criteria] == [("ac1", "NEW"), ("ac2", "B"), ("ac3", "B")]
    assert out.acceptance_criteria[0].evidence == []                                    # NEW: fresh
    assert out.acceptance_criteria[1].evidence == [AcceptanceEvidence(kind="commit", ref="E2")]  # id-aligned B keeps E2
    assert out.acceptance_criteria[2].evidence == []                                    # shifted B: fresh
    # E1 ("A", edited away) is gone; E2 preserved exactly once, never duplicated.
    all_refs = [e.ref for c in out.acceptance_criteria for e in c.evidence]
    assert all_refs == ["E2"]


import pytest

from mship.core.spec_draft import new_spec, SPEC_BODY_TEMPLATE


def test_new_spec_defaults_id_from_title():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    spec = new_spec("Decision Queue", now=now)
    assert spec.id == "decision-queue"          # slugified title
    assert spec.title == "Decision Queue"
    assert spec.status == "draft"               # fresh specs start draft (MOS-240)
    assert spec.created_at == now and spec.updated_at == now
    assert spec.body == SPEC_BODY_TEMPLATE      # canonical empty body
    assert spec.affected_repos == [] and spec.task_slug is None


def test_new_spec_honors_explicit_id_repos_and_task():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    spec = new_spec("Anything", now=now, spec_id="custom",
                    affected_repos=["a", "b"], task_slug="t")
    assert spec.id == "custom"
    assert spec.affected_repos == ["a", "b"]
    assert spec.task_slug == "t"


def test_new_spec_unslugifiable_title_raises():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        new_spec("!!!", now=now)             # slug collapses to empty


def test_specdraft_accepts_additional_sections():
    d = SpecDraft(problem="P", user_story="U", approach="A",
                  additional_sections=[{"heading": "Architecture", "body": "arch"}])
    assert d.additional_sections[0].heading == "Architecture"
    assert d.additional_sections[0].body == "arch"


def test_apply_draft_renders_additional_sections():
    from mship.core.spec_body import parse_body_sections, validate_body_structure
    spec = _spec()
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      additional_sections=[{"heading": "Testing", "body": "the tests"}])
    out = apply_draft(spec, draft)
    sections = parse_body_sections(out.body)
    assert sections["Testing"] == "the tests"
    assert validate_body_structure(out.body) == []   # required still present


def test_build_draft_prompt_mentions_additional_sections():
    assert "additional_sections" in build_draft_prompt("x", "intent")
