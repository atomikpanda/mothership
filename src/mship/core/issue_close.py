"""Close WorkItem-linked GitHub tracker issues when a task's PRs merge (#386).

Called from both merge close-out triggers — `mship close` and the pr_watcher
merge sweep — right after `advance_workitem_on_close`. Idempotent (an
already-closed issue is skipped, so the second trigger is a no-op) and
fault-tolerant (an API failure warns and never blocks the close-out).
"""
from __future__ import annotations

import re
from pathlib import Path

_PR_URL = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def _shipped_comment(pr_url: str) -> str:
    m = _PR_URL.match(pr_url or "")
    if m:
        return f"Shipped in {m.group(1)}/{m.group(2)}#{m.group(3)} (merged)."
    return "Shipped (PR merged)."


def close_linked_issues(*, task, workitems_dir, pr_manager,
                        merged_count: int, closed_count: int, warn,
                        open_count: int = 0) -> dict:
    """Close every still-open GitHub issue linked to the task's WorkItem.

    Never raises: individual failures are reported through `warn` and the
    close-out proceeds. Returns {"closed": [...], "skipped": [...], "failed": [...]}
    of canonical 'owner/repo#N' refs. No-op unless the task is fully merged —
    including when ANY PR is still open (a forced close with a mixed
    merged+open task must not close tracker issues, #410 review).
    """
    result: dict = {"closed": [], "skipped": [], "failed": []}
    if not getattr(task, "work_item_id", None):
        return result
    if merged_count == 0 or closed_count > 0 or open_count > 0:
        return result
    try:
        from mship.core.issue_link import linked_issue_refs
        from mship.core.issue_refs import issue_slug_and_number
        from mship.core.workitem_store import WorkItemStore

        item = WorkItemStore(Path(workitems_dir)).get(task.work_item_id)
        if item is None:
            return result
        refs = linked_issue_refs(item)
        if not refs:
            return result
        pr_url = next(iter(getattr(task, "pr_urls", {}).values()), "")
        comment = _shipped_comment(pr_url)
        for canonical in refs:
            slug, num = issue_slug_and_number(canonical)
            try:
                state = pr_manager.issue_state(slug, num)
                if state == "closed":
                    result["skipped"].append(canonical)
                    continue
                if state != "open":
                    warn(f"could not determine state of issue {canonical}; leaving it open")
                    result["failed"].append(canonical)
                    continue
                pr_manager.close_issue(slug, num, comment)
                result["closed"].append(canonical)
            except Exception as e:
                warn(f"failed to close issue {canonical}: {e}")
                result["failed"].append(canonical)
    except Exception as e:
        warn(f"linked-issue close-out skipped: {e}")
    return result
