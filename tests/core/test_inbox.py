from datetime import datetime, timedelta, timezone

import pytest

from mship.core.inbox import InboxMetadata, apply_inbox_action, classify_spec, classify_thread
from mship.core.message import DecisionPayload, Message, Thread
from mship.core.spec import Spec


SEVEN_DAYS = timedelta(days=7)
NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def _thread(**changes) -> Thread:
    values = {
        "id": "thread-1",
        "subject": "Subject",
        "created_at": NOW - SEVEN_DAYS,
        "updated_at": NOW,
    }
    values.update(changes)
    return Thread(**values)


def _spec(**changes) -> Spec:
    values = {
        "id": "spec-1",
        "title": "Spec",
        "status": "draft",
        "created_at": NOW - SEVEN_DAYS,
        "updated_at": NOW,
    }
    values.update(changes)
    return Spec(**values)


@pytest.mark.parametrize(
    ("thread", "linked", "linked_terminal", "expected"),
    [
        pytest.param(
            _thread(inbox=InboxMetadata(pinned=True, manual_archived=True)),
            True,
            True,
            ("active", None),
            id="pin-overrides-every-archival-rule",
        ),
        pytest.param(
            _thread(
                inbox=InboxMetadata(manual_archived=True),
                messages=[
                    Message(
                        id="attention",
                        thread_id="thread-1",
                        role="agent",
                        text="Need you",
                        kind="needs_you",
                        created_at=NOW,
                    )
                ],
            ),
            True,
            True,
            ("active", None),
            id="needs-you-overrides-manual-archive",
        ),
        pytest.param(
            _thread(
                inbox=InboxMetadata(manual_archived=True),
                messages=[
                    Message(
                        id="decision",
                        thread_id="thread-1",
                        role="agent",
                        text="Choose",
                        kind="decision",
                        decision=DecisionPayload(options=["one"]),
                        created_at=NOW,
                    )
                ],
            ),
            True,
            True,
            ("active", None),
            id="decision-overrides-manual-archive",
        ),
        pytest.param(
            _thread(inbox=InboxMetadata(manual_archived=True)),
            False,
            False,
            ("archived", "manual"),
            id="manual-archive-precedes-inactivity",
        ),
        pytest.param(
            _thread(inbox=InboxMetadata(restored_at=NOW - SEVEN_DAYS + timedelta(microseconds=1))),
            True,
            True,
            ("active", None),
            id="restore-grace-is-strictly-less-than-seven-days",
        ),
        pytest.param(
            _thread(inbox=InboxMetadata(restored_at=NOW - SEVEN_DAYS)),
            True,
            True,
            ("archived", "linked_terminal"),
            id="restore-grace-expires-at-exact-seven-day-boundary",
        ),
        pytest.param(
            _thread(updated_at=NOW - SEVEN_DAYS + timedelta(microseconds=1)),
            False,
            False,
            ("active", None),
            id="unlinked-activity-is-active-before-boundary",
        ),
        pytest.param(
            _thread(updated_at=NOW - SEVEN_DAYS),
            False,
            False,
            ("archived", "inactive_unlinked"),
            id="unlinked-activity-archives-at-boundary",
        ),
        pytest.param(
            _thread(updated_at=NOW),
            False,
            False,
            ("active", None),
            id="new-unlinked-activity-resets-inactivity",
        ),
        pytest.param(
            _thread(),
            True,
            False,
            ("active", None),
            id="nonterminal-link-does-not-archive",
        ),
    ],
)
def test_classify_thread_precedence(thread, linked, linked_terminal, expected):
    result = classify_thread(thread, linked=linked, linked_terminal=linked_terminal, now=NOW)
    assert (result.state, result.archive_reason) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param(
            _spec(status="archived", inbox=InboxMetadata(pinned=True)),
            ("active", None),
            id="pin-overrides-lifecycle-archive",
        ),
        pytest.param(
            _spec(status="archived", inbox=InboxMetadata(manual_archived=True)),
            ("archived", "manual"),
            id="manual-archive-precedes-lifecycle-archive",
        ),
        pytest.param(
            _spec(status="archived", inbox=InboxMetadata(restored_at=NOW - SEVEN_DAYS + timedelta(microseconds=1))),
            ("active", None),
            id="restore-keeps-lifecycle-archived-spec-active-during-grace",
        ),
        pytest.param(
            _spec(status="archived", inbox=InboxMetadata(restored_at=NOW - SEVEN_DAYS)),
            ("archived", "lifecycle_archived"),
            id="lifecycle-archived-spec-restores-at-exact-boundary",
        ),
        pytest.param(
            _spec(status="implemented", updated_at=NOW - SEVEN_DAYS + timedelta(microseconds=1)),
            ("active", None),
            id="implemented-spec-active-before-boundary",
        ),
        pytest.param(
            _spec(status="implemented", updated_at=NOW - SEVEN_DAYS),
            ("archived", "implemented"),
            id="implemented-spec-archives-at-boundary",
        ),
        pytest.param(
            _spec(status="implemented", updated_at=NOW),
            ("active", None),
            id="implemented-spec-update-resets-inactivity",
        ),
        *[
            pytest.param(_spec(status=status), ("active", None), id=f"{status}-lifecycle-is-active")
            for status in ("draft", "needs_review", "approved", "dispatched")
        ],
    ],
)
def test_classify_spec_precedence(spec, expected):
    result = classify_spec(spec, now=NOW)
    assert (result.state, result.archive_reason) == expected


def test_unpin_falls_back_to_retained_manual_archive_metadata():
    thread = _thread(inbox=InboxMetadata(pinned=False, manual_archived=True, restored_at=NOW))

    result = classify_thread(thread, linked=False, linked_terminal=False, now=NOW)

    assert (result.state, result.archive_reason) == ("archived", "manual")


def test_new_inbox_action_stamps_change_once_and_retry_preserves_it():
    metadata = InboxMetadata()
    first = NOW + timedelta(seconds=1)

    assert apply_inbox_action(metadata, "archive", "device-1", first) is True
    assert metadata.last_mutated_at == first

    assert apply_inbox_action(metadata, "archive", "device-1", first + timedelta(seconds=1)) is False
    assert metadata.last_mutated_at == first


def test_naive_thread_updated_at_is_normalized_for_exact_inactivity_boundary():
    thread = _thread(updated_at=(NOW - SEVEN_DAYS).replace(tzinfo=None))

    result = classify_thread(thread, linked=False, linked_terminal=False, now=NOW)

    assert (result.state, result.archive_reason) == ("archived", "inactive_unlinked")


def test_naive_spec_updated_at_and_restore_are_normalized_at_exact_boundaries():
    implemented = _spec(status="implemented", updated_at=(NOW - SEVEN_DAYS).replace(tzinfo=None))
    restored = _spec(status="archived", inbox=InboxMetadata(
        restored_at=(NOW - SEVEN_DAYS).replace(tzinfo=None),
    ))

    assert classify_spec(implemented, now=NOW).archive_reason == "implemented"
    assert classify_spec(restored, now=NOW).archive_reason == "lifecycle_archived"
