"""Tests for the linked-issue merge close-out (#386)."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from mship.core.issue_close import close_linked_issues
from mship.core.workitem import ExternalLink
from mship.core.workitem_store import WorkItemStore


def _item_with_issue(tmp_path: Path, refs: list[str]) -> tuple[WorkItemStore, str]:
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind="bug", workspace="ws",
                      now=datetime.now(timezone.utc))
    for canonical in refs:
        slug, num = canonical.rsplit("#", 1)
        store.add_external_link(
            wi.id,
            ExternalLink(provider="github",
                         url=f"https://github.com/{slug}/issues/{num}",
                         title=canonical),
            now=datetime.now(timezone.utc))
    return store, wi.id


def _task(wi_id, pr_urls=None):
    return SimpleNamespace(work_item_id=wi_id,
                           pr_urls=pr_urls or {"shared": "https://github.com/acme/widgets/pull/99"})


class FakePRManager:
    def __init__(self, states: dict[str, str], fail_close: bool = False):
        self.states = states  # "owner/repo#N" -> state
        self.fail_close = fail_close
        self.closed_calls: list[tuple[str, int, str]] = []

    def issue_state(self, slug: str, number: int) -> str:
        return self.states.get(f"{slug}#{number}", "unknown")

    def close_issue(self, slug: str, number: int, comment: str) -> None:
        if self.fail_close:
            raise RuntimeError("gh boom")
        self.closed_calls.append((slug, number, comment))
        self.states[f"{slug}#{number}"] = "closed"


def test_closes_open_issue_with_shipped_comment(tmp_path):
    store, wi_id = _item_with_issue(tmp_path, ["acme/widgets#12"])
    pm = FakePRManager({"acme/widgets#12": "open"})
    warnings: list[str] = []
    result = close_linked_issues(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                                 pr_manager=pm, merged_count=1, closed_count=0,
                                 warn=warnings.append)
    assert result["closed"] == ["acme/widgets#12"]
    assert len(pm.closed_calls) == 1
    slug, num, comment = pm.closed_calls[0]
    assert (slug, num) == ("acme/widgets", 12)
    assert "Shipped in acme/widgets#99" in comment
    assert warnings == []


def test_already_closed_issue_is_skipped_without_comment(tmp_path):
    store, wi_id = _item_with_issue(tmp_path, ["acme/widgets#12"])
    pm = FakePRManager({"acme/widgets#12": "closed"})
    result = close_linked_issues(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                                 pr_manager=pm, merged_count=1, closed_count=0,
                                 warn=lambda m: None)
    assert result["skipped"] == ["acme/widgets#12"]
    assert pm.closed_calls == []


def test_close_failure_warns_and_never_raises(tmp_path):
    store, wi_id = _item_with_issue(tmp_path, ["acme/widgets#12"])
    pm = FakePRManager({"acme/widgets#12": "open"}, fail_close=True)
    warnings: list[str] = []
    result = close_linked_issues(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                                 pr_manager=pm, merged_count=1, closed_count=0,
                                 warn=warnings.append)
    assert result["failed"] == ["acme/widgets#12"]
    assert warnings and "acme/widgets#12" in warnings[0]


def test_second_run_is_a_noop(tmp_path):
    store, wi_id = _item_with_issue(tmp_path, ["acme/widgets#12"])
    pm = FakePRManager({"acme/widgets#12": "open"})
    kwargs = dict(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                  pr_manager=pm, merged_count=1, closed_count=0, warn=lambda m: None)
    first = close_linked_issues(**kwargs)
    second = close_linked_issues(**kwargs)
    assert first["closed"] == ["acme/widgets#12"]
    assert second["skipped"] == ["acme/widgets#12"]
    assert len(pm.closed_calls) == 1


def test_noop_unless_fully_merged(tmp_path):
    store, wi_id = _item_with_issue(tmp_path, ["acme/widgets#12"])
    pm = FakePRManager({"acme/widgets#12": "open"})
    for merged, closed in ((0, 0), (1, 1)):
        result = close_linked_issues(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                                     pr_manager=pm, merged_count=merged, closed_count=closed,
                                     warn=lambda m: None)
        assert result == {"closed": [], "skipped": [], "failed": []}
    assert pm.closed_calls == []


def test_noop_without_work_item_or_links(tmp_path):
    pm = FakePRManager({})
    result = close_linked_issues(task=_task(None), workitems_dir=tmp_path / "workitems",
                                 pr_manager=pm, merged_count=1, closed_count=0,
                                 warn=lambda m: None)
    assert result == {"closed": [], "skipped": [], "failed": []}

    store, wi_id = _item_with_issue(tmp_path, [])
    result = close_linked_issues(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                                 pr_manager=pm, merged_count=1, closed_count=0,
                                 warn=lambda m: None)
    assert result == {"closed": [], "skipped": [], "failed": []}


def test_noop_when_any_pr_still_open(tmp_path):
    """close --force with merged+open PRs must not close tracker issues (#410 review)."""
    store, wi_id = _item_with_issue(tmp_path, ["acme/widgets#12"])
    pm = FakePRManager({"acme/widgets#12": "open"})
    result = close_linked_issues(task=_task(wi_id), workitems_dir=tmp_path / "workitems",
                                 pr_manager=pm, merged_count=1, closed_count=0,
                                 open_count=1, warn=lambda m: None)
    assert result == {"closed": [], "skipped": [], "failed": []}
    assert pm.closed_calls == []
