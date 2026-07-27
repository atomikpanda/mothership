"""Invariants of the remote-exact-copy feature that no single module can hold.

Everything else about this feature is tested where it lives. What lands here is
the class of claim that is about what the REST of the tree does not do, and so
can only be checked by looking at all of it.

ac14 has three clauses. Two are guarded below already: no code path may
branch from, merge, or open a pull request from the run namespace (the scan
this file started with), and the namespace exists only on run hosts (ac3,
pinned at the unit level in `tests/core/test_run_transfer.py::
test_the_push_goes_to_the_run_host_not_origin` — nothing here duplicates
it). Task 15 adds the third: `finish` still requires real commits. That is a
property of every file in `src/mship`, not of `core/run_ref.py` — whose
docstring cites this file, which is why it exists ahead of the task that fills
it out.

The scan is deliberately blunt: it reads source text, not behaviour, so it
cannot prove a run ref is never merged — only that nothing in the tree spells
out a merge, a branch, or a PR while naming the namespace. It is a tripwire for
the next person, not a proof. `_test_the_detector_fires` below is what keeps it
from being a tripwire that has quietly stopped working.
"""
import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "mship"

# Naming the run scratch namespace, in any of the three forms the tree uses it:
# the literal ref, the constant, or an import of the module that owns it.
# `refs/mship-probe/` (cli/workitem.py) is a DIFFERENT namespace and must not
# match; nor must `test_run_refs`, which is about test runs, not run refs.
_NAMES_RUN_NAMESPACE = re.compile(r"refs/mship/run|RUN_REF_PREFIX|\brun_ref\(|is_run_ref\(")

# Forming a branch, a merge, or a pull request. Matched as git/gh command
# shapes rather than bare words so that a `"branch"` dict key or a `.branch`
# attribute is not mistaken for one.
_FORMS_HISTORY = {
    "git branch": re.compile(r"""["'](branch|merge|rebase|cherry-pick|revert)["']"""),
    "git checkout -b / switch -c": re.compile(
        r"""["'](checkout|switch)["'].{0,80}?["'](-b|-c)["']""", re.S
    ),
    "shell git branch/merge": re.compile(r"\bgit\s+(branch|merge|rebase|cherry-pick)\b"),
    "gh pr create": re.compile(r"""gh\s+pr\s+create|["']pr["']\s*,\s*["']create["']"""),
}


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _calls_naming_the_namespace(path: Path) -> list[tuple[int, str]]:
    """Every call expression in `path` whose source names the run namespace.

    A call is the tightest scope that still holds a whole git invocation
    together, whatever shape it takes in this tree — an argv list
    (`subprocess.run(["git", ...])`), a varargs helper (`self._git("rebase",
    ...)`), or an f-string command line (`f"gh pr create ..."`). Nested calls
    yield the same text more than once, which costs nothing.
    """
    source = path.read_text(encoding="utf-8")
    if not _NAMES_RUN_NAMESPACE.search(source):
        return []                      # nothing to parse: most of the tree
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if _NAMES_RUN_NAMESPACE.search(segment):
            found.append((node.lineno, segment))
    return found


def test_no_code_path_branches_from_merges_or_opens_a_pr_from_the_run_namespace():
    """ac14. The scratch ref is not history and must never become any."""
    offences = []
    for path in _source_files():
        for lineno, segment in _calls_naming_the_namespace(path):
            for label, pattern in _FORMS_HISTORY.items():
                if pattern.search(segment):
                    offences.append(f"{path.relative_to(SRC.parent.parent)}:{lineno} ({label})")
    assert not offences, (
        "these call sites name refs/mship/run AND form a branch, merge or pull "
        "request, which spec ac14 forbids — the run namespace is a throwaway "
        f"hand-off, not history: {offences}"
    )


def test_the_scan_actually_reaches_the_modules_that_own_the_namespace():
    """Guards the guard: a scan that matched nothing would pass the test above
    for free. The two modules that own the namespace today must be seen."""
    seen = {
        path.name for path in _source_files()
        if _NAMES_RUN_NAMESPACE.search(path.read_text(encoding="utf-8"))
    }
    assert {"run_ref.py", "git_receive.py"} <= seen, seen


def test_a_different_mship_namespace_is_not_mistaken_for_the_run_namespace():
    """`cli/workitem.py` writes `refs/mship-probe/<ns>/branch` — throwaway refs
    for a commits-ahead probe, and nothing to do with ac14. A pattern loose
    enough to catch it would make the scan above cry wolf on unrelated work."""
    assert not _NAMES_RUN_NAMESPACE.search('"refs/mship-probe/x/branch"')
    assert not _NAMES_RUN_NAMESPACE.search("for ref in test_run_refs:")


@pytest.mark.parametrize("sample", [
    'subprocess.run(["git", "merge", run_ref(task, repo)])',
    'subprocess.run(["git", "checkout", "-b", "x", run_ref(task, repo)])',
    'self._git("branch", "x", f"{RUN_REF_PREFIX}t1/api")',
    'shell(f"gh pr create --head refs/mship/run/t1/api")',
    'run(["gh", "pr", "create", "--head", run_ref(task, repo)])',
])
def test_the_detector_fires_on_the_shapes_it_claims_to_catch(sample):
    """The scan above is green because the tree is clean. This is what says it
    is green for that reason and not because the patterns stopped matching —
    every command shape the tree actually uses, fed through the same two
    regexes the scan runs."""
    assert _NAMES_RUN_NAMESPACE.search(sample)
    assert any(p.search(sample) for p in _FORMS_HISTORY.values()), sample


# The gate that decides whether `finish` pushes a branch and opens a PR for a
# repo at all: `PRManager.count_commits_ahead` (`core/pr.py`) counts real
# commits between base and the task's own branch, purely from that branch's
# own git history; `finish` (`cli/worktree.py`) is the CLI command that acts
# on the result. Neither module can start treating scratch state as a
# substitute for a real commit if neither one can even name the scratch
# namespace — that is the property this pins.
FINISH_GATE_MODULES = (
    "core/pr.py",
    "cli/worktree.py",
)


def test_the_real_commits_gate_and_finish_do_not_know_the_run_namespace_exists():
    """ac14's third clause: `finish` still requires real commits.

    The scratch ref never reaches the operator's own branch (ac2, pinned in
    `test_run_transfer.py::test_local_state_is_identical_before_and_after` and
    `::test_the_commit_is_on_no_branch`), so today `count_commits_ahead` has
    nothing to see. The risk this guards is a FUTURE one: `core/pr.py` or
    `cli/worktree.py`'s `finish` growing a special case keyed on `is_run_ref`
    or `RUN_REF_PREFIX` — e.g. "a run ref exists for this repo, treat it as
    ready to finish" — which would let scratch state stand in for a commit
    the operator never made. If neither module can name the namespace at all,
    neither can special-case it.
    """
    for module in FINISH_GATE_MODULES:
        path = SRC / module
        assert path.exists(), f"{module} moved; update FINISH_GATE_MODULES"
        source = path.read_text(encoding="utf-8")
        assert not _NAMES_RUN_NAMESPACE.search(source), (
            f"{module} now names the run scratch namespace. `finish` must "
            f"keep deciding whether to push/open a PR from real branch "
            f"history alone (spec ac14) — if {module} genuinely needs to "
            f"consult run refs, that is a spec-level decision, not a silent "
            f"one."
        )


# --- ac19: the docs say what travels ----------------------------------------

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _remote_run_doc() -> str:
    return (DOCS / "remote-run.md").read_text().lower()


def test_docs_state_that_uncommitted_work_travels_and_never_reaches_origin():
    t = _remote_run_doc()
    assert "uncommitted" in t
    assert "untracked" in t
    assert "never" in t and "origin" in t


def test_docs_state_what_does_not_travel():
    """ac19: secrets, platform state, symlink_dirs/bind_files."""
    t = _remote_run_doc()
    assert ".env" in t or "secret" in t
    assert "symlink_dirs" in t and "bind_files" in t
    assert "gitignore" in t


def test_docs_state_that_dependencies_are_derived_by_setup():
    """ac19 + ac17, including that the first run on a fresh host is a one-time
    cost rather than a regression."""
    t = _remote_run_doc()
    assert "task setup" in t
    assert "setup_inputs" in t
    assert "first" in t


def test_docs_name_the_scratch_ref_as_throwaway():
    """ac13: an operator reading the output must not think it is their commit."""
    t = _remote_run_doc()
    assert "refs/mship/run" in t
    assert "throwaway" in t


def test_docs_no_longer_claim_a_dirty_tree_is_refused():
    """The #419 wording is now false; leaving it would be worse than silence."""
    t = _remote_run_doc()
    assert "tracked changes present" not in t


def test_configuration_documents_setup_inputs():
    """ac17: declaring them is what enables re-run-on-change."""
    t = (DOCS / "configuration.md").read_text().lower()
    assert "setup_inputs" in t
    assert "re-run" in t or "rerun" in t
