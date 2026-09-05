from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from mship.core.spec import (
    AcceptanceCriterion,
    AcceptanceEvidence,
    OpenQuestion,
    Spec,
    SpecDraft,
)
from mship.core.spec_body import validate_body_structure
from mship.core.spec_draft import (
    MissingSpec,
    ReviewDiscardRequired,
    SPEC_BODY_TEMPLATE,
    apply_draft,
    apply_draft_transaction,
    build_draft_prompt,
    new_spec,
)
from mship.core.spec_store import SpecStore




def test_build_draft_prompt_contains_intent_schema_and_apply():
    prompt = build_draft_prompt("decision-queue", "I want X away from the desk")
    assert "decision-queue" in prompt
    assert "I want X away from the desk" in prompt          # the intent
    assert "acceptance_criteria" in prompt                  # the JSON shape
    assert "open_questions" in prompt
    assert "mship spec apply decision-queue --from-json" in prompt  # how to apply
    assert "only" in prompt.lower()                         # "output only JSON"


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


def test_apply_draft_preserves_comment_for_unchanged_criterion():
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="view questions", verdict="approved", comment="reviewed",
        ),
    ]

    out = apply_draft(
        spec,
        SpecDraft(
            problem="P", user_story="U", approach="A",
            acceptance_criteria=["view questions"],
        ),
    )

    assert out.acceptance_criteria[0].comment == "reviewed"


def test_apply_draft_preserves_answered_questions_once_per_duplicate_text():
    spec = _spec()
    spec.open_questions = [
        OpenQuestion(id="q1", text="Who owns this?", answer="Platform"),
        OpenQuestion(id="q2", text="Who owns this?", answer="Infrastructure"),
    ]

    out = apply_draft(
        spec,
        SpecDraft(
            problem="P", user_story="U", approach="A",
            open_questions=["Who owns this?", "Who owns this?", "Who owns this?"],
        ),
    )

    assert [question.answer for question in out.open_questions] == [
        "Platform", "Infrastructure", None,
    ]


def test_apply_draft_preserves_prose_verdicts_for_unchanged_sections():
    from mship.core.spec import ProseVerdict
    from mship.core.spec_body import render_body
    spec = _spec()
    # Establish a known OLD body + list fields, then re-draft with IDENTICAL text —
    # every prose verdict must carry over unchanged.
    spec.body = render_body("P", "U", "A")
    spec.non_goals = ["chat"]
    spec.risks = ["scope"]
    spec.prose_verdicts = {"approach": ProseVerdict(verdict="approved"),
                           "problem": ProseVerdict(verdict="flagged", comment="c"),
                           "non_goals": ProseVerdict(verdict="approved"),
                           "risks": ProseVerdict(verdict="approved")}
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      non_goals=["chat"], risks=["scope"], acceptance_criteria=["x"])
    out = apply_draft(spec, draft)
    assert out.prose_verdicts["approach"].verdict == "approved"
    assert out.prose_verdicts["problem"].comment == "c"
    assert out.prose_verdicts["non_goals"].verdict == "approved"
    assert out.prose_verdicts["risks"].verdict == "approved"


def test_apply_draft_drops_prose_verdict_when_text_changed():
    # Greptile #344: a prose verdict is kept ONLY when the section text is unchanged.
    # A rewritten `approach` must NOT keep its stale approval (would slip past the
    # gate); an unchanged `problem` must keep its verdict.
    from mship.core.spec import ProseVerdict
    from mship.core.spec_body import render_body
    spec = _spec()
    spec.body = render_body("the problem", "U", "old approach")
    spec.prose_verdicts = {"approach": ProseVerdict(verdict="approved"),
                           "problem": ProseVerdict(verdict="approved")}
    draft = SpecDraft(problem="the problem", user_story="U", approach="new approach",
                      acceptance_criteria=["x"])       # approach CHANGED, problem SAME
    out = apply_draft(spec, draft)
    assert out.prose_verdicts["problem"].verdict == "approved"   # unchanged → preserved
    assert "approach" not in out.prose_verdicts                  # changed → re-review


def test_apply_draft_drops_prose_verdict_when_list_field_changed():
    # non_goals/risks are list fields — changing the list must reset that section's
    # verdict too; an unchanged list keeps it.
    from mship.core.spec import ProseVerdict
    spec = _spec()
    spec.non_goals = ["a"]
    spec.risks = ["r"]
    spec.prose_verdicts = {"non_goals": ProseVerdict(verdict="approved"),
                           "risks": ProseVerdict(verdict="approved")}
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      non_goals=["a", "b"], risks=["r"],       # non_goals CHANGED, risks SAME
                      acceptance_criteria=["x"])
    out = apply_draft(spec, draft)
    assert "non_goals" not in out.prose_verdicts                 # changed → re-review
    assert out.prose_verdicts["risks"].verdict == "approved"     # unchanged → preserved


def test_apply_draft_preserves_uncomparable_prose_verdict():
    # A section id with no draft-derived text (e.g. scope_risk) has nothing to compare,
    # so its verdict carries over unchanged across a re-draft.
    from mship.core.spec import ProseVerdict
    spec = _spec()
    spec.prose_verdicts = {"scope_risk": ProseVerdict(verdict="approved")}
    draft = SpecDraft(problem="P2", user_story="U2", approach="A2", acceptance_criteria=["x"])
    out = apply_draft(spec, draft)
    assert out.prose_verdicts["scope_risk"].verdict == "approved"


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


def test_apply_draft_transaction_returns_missing_spec(tmp_path):
    result = apply_draft_transaction(
        SpecStore(tmp_path / "specs"),
        "missing",
        SpecDraft(problem="P", user_story="U", approach="A"),
    )

    assert result == MissingSpec(spec_id="missing")


@pytest.mark.parametrize("bypass_status_gate", [False, True])
def test_apply_draft_transaction_refuses_review_loss_without_discard(
    tmp_path, bypass_status_gate,
):
    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.status = "needs_review"
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="old", verdict="approved"),
    ]
    spec.open_questions = [OpenQuestion(id="q1", text="Why?", answer="Because.")]
    store.save(spec)

    with pytest.raises(ReviewDiscardRequired) as raised:
        apply_draft_transaction(
            store,
            spec.id,
            SpecDraft(
                problem="P", user_story="U", approach="A",
                acceptance_criteria=["new"],
                open_questions=["Different question?"],
            ),
            bypass_status_gate=bypass_status_gate,
        )

    assert raised.value.discarded_review_count == 2
    persisted = store.find_by_id(spec.id)
    assert persisted is not None
    assert persisted.acceptance_criteria[0].text == "old"
    assert persisted.open_questions[0].answer == "Because."


def test_apply_draft_transaction_refuses_lost_unreviewed_criterion_evidence(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="old",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1")],
        ),
    ]
    store.save(spec)

    with pytest.raises(ReviewDiscardRequired) as raised:
        apply_draft_transaction(
            store,
            spec.id,
            SpecDraft(
                problem="P", user_story="U", approach="A",
                acceptance_criteria=["new"],
            ),
        )

    assert raised.value.discarded_review_count == 1


def test_apply_draft_transaction_refuses_lost_unreviewed_criterion_comment(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="old", comment="Needs discussion."),
    ]
    store.save(spec)

    with pytest.raises(ReviewDiscardRequired) as raised:
        apply_draft_transaction(
            store,
            spec.id,
            SpecDraft(
                problem="P", user_story="U", approach="A",
                acceptance_criteria=["new"],
            ),
        )

    assert raised.value.discarded_review_count == 1


def test_apply_draft_transaction_refuses_lost_unreviewed_prose_comment(tmp_path):
    from mship.core.spec import ProseVerdict
    from mship.core.spec_body import render_body

    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.body = render_body("P", "U", "old approach")
    spec.prose_verdicts = {
        "approach": ProseVerdict(comment="Needs discussion."),
    }
    store.save(spec)

    with pytest.raises(ReviewDiscardRequired) as raised:
        apply_draft_transaction(
            store,
            spec.id,
            SpecDraft(problem="P", user_story="U", approach="new approach"),
        )

    assert raised.value.discarded_review_count == 1



def test_apply_draft_transaction_persists_unchanged_review_state(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="keep", verdict="approved", comment="reviewed",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1")],
        ),
    ]
    spec.open_questions = [OpenQuestion(id="q1", text="Why?", answer="Because.")]
    store.save(spec)
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    result = apply_draft_transaction(
        store,
        spec.id,
        SpecDraft(
            problem="P", user_story="U", approach="A",
            acceptance_criteria=["keep"],
            open_questions=["Why?"],
        ),
        now=now,
    )

    assert result.discarded_review_count == 0
    assert result.spec.updated_at == now
    assert result.spec.status == "needs_review"
    assert result.spec.acceptance_criteria[0].comment == "reviewed"
    assert result.spec.open_questions[0].answer == "Because."
    assert result.path == store.path_for(result.spec)


def test_apply_draft_transaction_discards_only_replaced_review_units(tmp_path):
    from mship.core.spec import ProseVerdict
    from mship.core.spec_body import render_body

    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.body = render_body("P", "U", "old approach")
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="keep", verdict="approved", comment="kept",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1")],
        ),
        AcceptanceCriterion(id="ac2", text="replace", verdict="flagged"),
    ]
    spec.prose_verdicts = {
        "problem": ProseVerdict(verdict="approved", comment="still applies"),
        "approach": ProseVerdict(verdict="flagged"),
    }
    spec.open_questions = [
        OpenQuestion(id="q1", text="Keep?", answer="Yes."),
        OpenQuestion(id="q2", text="Replace?", answer="No."),
    ]
    store.save(spec)

    result = apply_draft_transaction(
        store,
        spec.id,
        SpecDraft(
            problem="P", user_story="U", approach="new approach",
            acceptance_criteria=["keep", "replacement"],
            open_questions=["Keep?", "Replacement?"],
        ),
        discard_review=True,
    )

    assert result.discarded_review_count == 3
    kept_criterion = result.spec.acceptance_criteria[0]
    assert kept_criterion.verdict == "approved"
    assert kept_criterion.comment == "kept"
    assert kept_criterion.evidence == [
        AcceptanceEvidence(kind="test", ref="test-runs/1"),
    ]
    assert result.spec.prose_verdicts == {
        "problem": ProseVerdict(verdict="approved", comment="still applies"),
    }
    assert [question.answer for question in result.spec.open_questions] == ["Yes.", None]


def test_apply_draft_transaction_keeps_lock_through_loss_check_and_save(
    tmp_path, monkeypatch,
):
    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(id="ac1", text="keep", verdict="approved"),
    ]
    store.save(spec)
    review_attempted = Event()
    review_acquired = Event()
    original_save = store.save_while_locked
    reviewer: Thread | None = None

    def record_review():
        review_attempted.set()
        with store.locked(spec.id) as artifact:
            assert artifact is not None
            review_acquired.set()
            artifact.spec.acceptance_criteria[0].comment = "late review"
            original_save(artifact.spec, artifact)

    def save_after_review_attempt(applied, artifact):
        nonlocal reviewer
        reviewer = Thread(target=record_review)
        reviewer.start()
        assert review_attempted.wait(timeout=1)
        assert not review_acquired.wait(timeout=0.1)
        return original_save(applied, artifact)

    monkeypatch.setattr(store, "save_while_locked", save_after_review_attempt)

    result = apply_draft_transaction(
        store,
        spec.id,
        SpecDraft(
            problem="P", user_story="U", approach="A",
            acceptance_criteria=["keep"],
        ),
    )
    assert reviewer is not None
    reviewer.join(timeout=1)

    assert not reviewer.is_alive()
    assert review_attempted.is_set()
    assert review_acquired.is_set()
    assert result.spec.acceptance_criteria[0].comment is None
    persisted = store.find_by_id(spec.id)
    assert persisted is not None
    assert persisted.acceptance_criteria[0].comment == "late review"
