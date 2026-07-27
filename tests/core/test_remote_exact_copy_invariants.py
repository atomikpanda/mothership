"""Invariants of the remote-exact-copy feature that no single module can hold.

Everything else about this feature is tested where it lives. What lands here is
the class of claim that is about what the REST of the tree does not do, and so
can only be checked by looking at all of it.

ac14, the one guarded now: the run scratch namespace is throwaway, so no code
path may branch from it, merge it, or open a pull request from it. That is a
property of every file in `src/mship`, not of `core/run_ref.py` — whose
docstring cites this file, which is why it exists ahead of the task that fills
it out. Task 15 extends it with the rest of the feature's cross-cutting
invariants.

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
