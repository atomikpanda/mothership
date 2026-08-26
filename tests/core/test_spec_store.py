from datetime import datetime, timezone
from pathlib import Path

from threading import Event, Thread

import pytest

from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, OpenQuestion, Spec
from mship.core.spec_store import (
    SpecArtifactConflict, SpecParseError, SpecStore, parse_spec, serialize_spec,
)
from mship.core.spec_storage import SpecStorage

def _spec():
    now = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
    return Spec(
        id="decision-queue", title="Decision queue", status="needs_review",
        created_at=now, updated_at=now,
        affected_repos=["mothership", "ground-control"],
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions")],
        open_questions=[OpenQuestion(id="q1", text="Android in v0?")],
        non_goals=["chat"],
        body="## Problem\n\nAgents block away from the desk.\n",
    )


def test_round_trip_is_identity():
    s = _spec()
    assert parse_spec(serialize_spec(s)) == s


def test_evidence_round_trips_through_serialize_parse():
    now = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)
    s = Spec(
        id="ev", title="Evidence", status="needs_review",
        created_at=now, updated_at=now,
        acceptance_criteria=[AcceptanceCriterion(
            id="ac1", text="does the thing", verdict="approved",
            evidence=[
                AcceptanceEvidence(kind="test", ref="test-runs/5.mothership"),
                AcceptanceEvidence(kind="commit", ref="deadbeef", note="the fix"),
            ],
        )],
        body="## Problem\n\nx\n",
    )
    parsed = parse_spec(serialize_spec(s))
    assert parsed == s
    assert parsed.acceptance_criteria[0].evidence[1].note == "the fix"


def test_legacy_spec_without_evidence_key_loads_with_empty_list():
    # A frontmatter block whose acceptance_criteria have NO evidence key at all
    # (an older on-disk spec) must load with evidence == [].
    text = (
        "---\n"
        "id: legacy\n"
        "title: Legacy\n"
        "status: needs_review\n"
        "created_at: '2026-07-12T10:00:00Z'\n"
        "updated_at: '2026-07-12T10:00:00Z'\n"
        "acceptance_criteria:\n"
        "- id: ac1\n"
        "  text: old criterion\n"
        "  verdict: approved\n"
        "---\n"
        "## Problem\n\nlegacy body\n"
    )
    spec = parse_spec(text)
    assert spec.acceptance_criteria[0].evidence == []


def test_body_is_preserved_verbatim():
    s = _spec()
    parsed = parse_spec(serialize_spec(s))
    assert parsed.body == "## Problem\n\nAgents block away from the desk.\n"


def test_missing_frontmatter_raises():
    with pytest.raises(SpecParseError):
        parse_spec("# just markdown, no frontmatter\n")


def test_unterminated_frontmatter_raises():
    with pytest.raises(SpecParseError):
        parse_spec("---\nid: foo\n")  # no closing ---


def test_invalid_schema_frontmatter_raises_spec_parse_error():
    # valid YAML, but missing required Spec fields -> SpecParseError, not raw ValidationError
    with pytest.raises(SpecParseError):
        parse_spec("---\nid: foo\n---\nbody\n")


def test_malformed_yaml_raises_spec_parse_error():
    with pytest.raises(SpecParseError):
        parse_spec("---\nid: [unclosed\n---\nbody\n")


def _new_spec(spec_id: str):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return Spec(id=spec_id, title=spec_id, status="draft", created_at=now, updated_at=now)


def test_save_then_find_by_id(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    path = store.save(_new_spec("alpha"))
    assert path.name == "2026-06-13-alpha.md"
    assert path.is_file()
    found = store.find_by_id("alpha")
    assert found is not None and found.id == "alpha"


def test_find_by_id_is_exact_not_mtime(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    store.save(_new_spec("beta"))   # newer mtime
    assert store.find_by_id("alpha").id == "alpha"


def test_list_returns_all(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    store.save(_new_spec("beta"))
    assert sorted(s.id for s in store.list()) == ["alpha", "beta"]


def test_find_by_id_missing_returns_none(tmp_path: Path):
    assert SpecStore(tmp_path / "specs").find_by_id("nope") is None


def test_save_overwrites_and_reflects_update(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    updated = _new_spec("alpha")
    updated.status = "needs_review"
    store.save(updated)
    assert store.find_by_id("alpha").status == "needs_review"
    assert len(store.list()) == 1  # same path, overwritten not duplicated


def test_path_for_rejects_unsafe_id(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    bad = _new_spec("alpha")
    bad.id = "../escape"
    with pytest.raises(ValueError):
        store.save(bad)


def test_mutate_inbox_duplicate_restore_is_a_noop(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)

    store.mutate_inbox(spec.id, "restore", "restore-1", now)
    store.mutate_inbox(spec.id, "restore", "restore-1", now.replace(minute=1))

    saved = store.find_by_id(spec.id)
    assert saved.inbox.restored_at == now
    assert saved.inbox.mutation_ids == {"restore-1": "restore"}

def test_mutate_inbox_persists_new_state_equivalent_spec_action(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)

    _, applied = store.mutate_inbox(spec.id, "unpin", "unpin-1", now)

    assert applied is False
    saved = SpecStore(tmp_path / "specs").find_by_id(spec.id)
    assert saved.inbox.mutation_ids == {"unpin-1": "unpin"}
    assert saved.inbox.last_mutated_at is None

    with pytest.raises(ValueError):
        SpecStore(tmp_path / "specs").mutate_inbox(spec.id, "archive", "unpin-1", now)

def test_mutate_inbox_commits_spec_actions_in_order(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    archive_then_restore = _new_spec("one")
    restore_then_archive = _new_spec("two")
    store.save(archive_then_restore)
    store.save(restore_then_archive)

    store.mutate_inbox(archive_then_restore.id, "archive", "archive-1", now)
    store.mutate_inbox(archive_then_restore.id, "restore", "restore-1", now.replace(minute=1))
    store.mutate_inbox(restore_then_archive.id, "restore", "restore-2", now)
    store.mutate_inbox(restore_then_archive.id, "archive", "archive-2", now.replace(minute=1))

    assert store.find_by_id(archive_then_restore.id).inbox.manual_archived is False
    assert store.find_by_id(restore_then_archive.id).inbox.manual_archived is True


def test_mutate_inbox_pin_unpin_preserves_spec_manual_and_restore_metadata(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)

    store.mutate_inbox(spec.id, "archive", "archive-0", now)
    store.mutate_inbox(spec.id, "restore", "restore-1", now.replace(minute=1))
    store.mutate_inbox(spec.id, "archive", "archive-1", now.replace(minute=2))
    store.mutate_inbox(spec.id, "pin", "pin-1", now.replace(minute=3))
    store.mutate_inbox(spec.id, "unpin", "unpin-1", now.replace(minute=4))

    inbox = store.find_by_id(spec.id).inbox
    assert inbox.pinned is False
    assert inbox.manual_archived is True
    assert inbox.restored_at == now.replace(minute=1)


def test_mutate_inbox_does_not_change_spec_domain_content_or_lifecycle(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    spec.status = "implemented"
    store.save(spec)
    before = spec.model_dump(exclude={"inbox"})

    store.mutate_inbox(spec.id, "archive", "archive-1", datetime(2026, 8, 25, tzinfo=timezone.utc))
    assert store.find_by_id(spec.id).model_dump(exclude={"inbox"}) == before



def test_inbox_mutation_keeps_runtime_lock_out_of_specs_directory(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    specs_dir = tmp_path / "specs"
    store = SpecStore(specs_dir)
    spec = _new_spec("alpha")
    store.save(spec)

    store.mutate_inbox(spec.id, "archive", "archive-1", now)

    assert [path.name for path in specs_dir.iterdir()] == ["2026-06-13-alpha.md"]
    assert store._lock_path(spec.id) == tmp_path / ".mothership" / "locks" / "specs" / "alpha.lock"



@pytest.mark.parametrize("action", ["unknown", ""])
def test_mutate_inbox_rejects_unknown_spec_action(tmp_path: Path, action: str):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)

    with pytest.raises(ValueError):
        store.mutate_inbox(spec.id, action, "action-1", now)


def test_mutate_inbox_rejects_conflicting_spec_mutation_identity(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)
    store.mutate_inbox(spec.id, "archive", "mutation-1", now)

    with pytest.raises(ValueError):
        store.mutate_inbox(spec.id, "restore", "mutation-1", now.replace(minute=1))


def test_stale_lifecycle_save_preserves_current_inbox_metadata(tmp_path: Path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)
    stale_lifecycle_copy = store.find_by_id(spec.id)

    store.mutate_inbox(spec.id, "archive", "archive-1", now)
    stale_lifecycle_copy.status = "needs_review"
    store.save(stale_lifecycle_copy)

    saved = store.find_by_id(spec.id)
    assert saved.status == "needs_review"
    assert saved.inbox.manual_archived is True
    assert saved.inbox.mutation_ids == {"archive-1": "archive"}


def test_concurrent_inbox_mutation_cannot_overwrite_lifecycle_save(tmp_path: Path, monkeypatch):
    """The lifecycle save landing while mutation persistence is pending wins both fields."""
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)
    lifecycle_update = store.find_by_id(spec.id)
    lifecycle_update.status = "needs_review"

    original_write = SpecStorage.write
    lifecycle_write_started = Event()
    lifecycle_writer = Thread(target=lambda: store.save(lifecycle_update))
    writer_started = False

    def interleave_lifecycle_save(storage, path, text):
        nonlocal writer_started
        saved = parse_spec(text)
        if saved.inbox.manual_archived and not writer_started:
            writer_started = True
            lifecycle_writer.start()
            # Before the lock-safe owner, the lifecycle writer reaches storage here
            # and the pending inbox write then clobbers its status. With the owner,
            # it blocks until this write releases the per-spec lock.
            lifecycle_write_started.wait(timeout=0.2)
        if saved.status == "needs_review":
            lifecycle_write_started.set()
        return original_write(storage, path, text)

    monkeypatch.setattr(SpecStorage, "write", interleave_lifecycle_save)
    store.mutate_inbox(spec.id, "archive", "archive-1", now)
    lifecycle_writer.join()
    assert lifecycle_write_started.wait(timeout=0.2)

    saved = store.find_by_id(spec.id)
    assert saved.status == "needs_review"
    assert saved.inbox.manual_archived is True
    assert saved.inbox.mutation_ids == {"archive-1": "archive"}


def test_mutate_inbox_resolves_renamed_physical_spec_by_frontmatter_id(tmp_path):
    store = SpecStore(tmp_path / "specs")
    spec = _spec()
    original = store.save(spec)
    original.rename(original.with_name("renamed.md"))

    mutated, applied = store.mutate_inbox(
        spec.id, "archive", "device-archive", datetime(2026, 8, 25, tzinfo=timezone.utc),
    )

    assert applied is True
    assert mutated.inbox.manual_archived is True
    assert mutated.status == "needs_review"

def test_lifecycle_save_overwrites_renamed_artifact(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    original = store.save(spec)
    renamed = original.with_name("legacy-alpha.md")
    original.rename(renamed)
    spec.status = "needs_review"

    assert store.save(spec) == renamed
    assert renamed.is_file()
    assert not original.exists()


def test_mutation_fails_closed_when_canonical_and_alias_both_match(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    canonical = store.save(spec)
    canonical.with_name("legacy-alpha.md").write_text(canonical.read_text())

    with pytest.raises(SpecArtifactConflict):
        store.mutate_inbox(
            spec.id, "archive", "archive-1", datetime(2026, 8, 25, tzinfo=timezone.utc),
        )


def test_create_rejects_target_filename_collision_with_other_frontmatter(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    candidate = _new_spec("alpha")
    collision = _new_spec("other")
    target = store.path_for(candidate)
    target.parent.mkdir(parents=True)
    target.write_text(serialize_spec(collision))

    with pytest.raises(SpecArtifactConflict):
        store.create_if_absent(candidate)


def test_save_propagates_storage_write_failure(tmp_path: Path, monkeypatch):
    store = SpecStore(tmp_path / "specs")
    spec = _new_spec("alpha")
    store.save(spec)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(SpecStorage, "write", fail_write)
    with pytest.raises(OSError, match="disk full"):
        store.save(spec)
