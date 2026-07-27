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

Two escapes it used to have, both found by mutating the source and watching the
whole suite stay green, and both closed here rather than left as folklore:

  * **it saw only the modules that spell the namespace out.** `\\brun_ref\\(`
    cannot see `build_run_ref(...)`, `push_run_ref(...)` or
    `cleanup_run_refs(...)` — the names the namespace actually crosses module
    boundaries under — so `core/remote_exec.py` (which resets the run host's
    worktree to the ref), `cli/exec.py` (which pushes it) and `cli/worktree.py`
    (which deletes it) were never parsed at all.
  * **it saw only single-expression call sites.**
    `shell.run(f"git merge {run_ref(t, r)}")` was caught; the same merge split
    over two statements — `cmd = f"git merge {run_ref(t, r)}"` then
    `shell.run(cmd)` — was not, because neither expression contains both halves.
    Simple local bindings are therefore followed (see `_run_ref_bindings`).
"""
import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "mship"

# Naming the run scratch namespace — the literal ref, the constant, the module
# that owns it, or any of the helpers the namespace travels under between
# modules. The helper names are what make the scan REACH `core/remote_exec.py`,
# `cli/exec.py` and `cli/worktree.py`, none of which ever spell `refs/mship/run`
# themselves.
#
# `refs/mship-probe/` (cli/workitem.py) is a DIFFERENT namespace and must not
# match; nor must `test_run_refs`, which is about test runs, not run refs.
_NAMES_RUN_NAMESPACE = re.compile(
    r"refs/mship/run|RUN_REF_PREFIX|\brun_refs?\b|\bis_run_ref\b|\bbuild_run_ref\b"
    r"|\bpush_run_ref\b|\bdelete_run_ref\b|\bcleanup_run_refs\b|\bRunRefNameError\b"
    # The import itself, so a helper named something nobody thought to list here
    # still cannot be reached from the finish gate below without showing up.
    r"|mship\.core\.run_(ref|transfer)"
)

# Forming a branch, a merge, or a pull request. Matched as git/gh command
# shapes rather than bare words so that a `"branch"` dict key or a `.branch`
# attribute is not mistaken for one.
_FORMS_HISTORY = {
    "git branch": re.compile(r"""["'](branch|merge|rebase|cherry-pick|revert)["']"""),
    "git checkout -b / switch -c": re.compile(
        r"""["'](checkout|switch)["'].{0,80}?["'](-b|-c)["']""", re.S
    ),
    # `(?![\w-])`, not `\b`: `\b` matches between `merge` and the `-` of
    # `git merge-base`, which `core/remote_preflight.py` runs legitimately and
    # which forms no history at all. Now that this scan reaches that module, a
    # bare `\b` would cry wolf there.
    "shell git branch/merge": re.compile(
        r"\bgit\s+(branch|merge|rebase|cherry-pick)(?![\w-])"
    ),
    "gh pr create": re.compile(r"""gh\s+pr\s+create|["']pr["']\s*,\s*["']create["']"""),
}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _scopes(tree: ast.AST):
    """Each function body, plus the module's own top level.

    Per FUNCTION rather than per module so that one function's `cmd` cannot
    stand in for another's — the same local name is reused all over this tree,
    and merging them would fire on unrelated code.
    """
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _own_nodes(scope: ast.AST):
    """The nodes `scope` owns — its subtree, not descending into a function
    nested inside it, which `_scopes` yields separately as its own scope."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _run_ref_bindings(source: str, scope: ast.AST) -> dict[str, frozenset[str]]:
    """Local names in `scope` holding a run-ref-derived value, mapped to every
    source fragment that went into them.

    ONLY namespace-carrying assignments are recorded, so the map stays tiny,
    and it is iterated to a fixed point so a chain lands WHOLE:
    `ref = run_ref(t, r)` then `cmd = "git branch x " + ref` leaves `cmd`
    carrying both fragments — the `git branch` that names the offence and the
    `run_ref(...)` that makes it this namespace's offence. Carrying only the
    nearest one would find the command and lose the ref, or the reverse.

    Fragments are a SET, so a chain that revisits a name converges instead of
    concatenating the same text on every pass.
    """
    assigns: list[tuple[str, str]] = []
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        text = ast.get_source_segment(source, value) or ""
        assigns.extend((t.id, text) for t in targets if isinstance(t, ast.Name))

    bound: dict[str, frozenset[str]] = {}
    for _ in range(len(assigns) + 1):
        grew = False
        for name, text in assigns:
            referenced = [bound[i] for i in _IDENT.findall(text) if i in bound]
            if not (_NAMES_RUN_NAMESPACE.search(text) or referenced):
                continue
            merged = bound.get(name, frozenset()) | {text} | frozenset().union(*referenced or [frozenset()])
            if merged != bound.get(name):
                bound[name] = merged
                grew = True
        if not grew:
            break
    return bound


def _with_bindings(segment: str, bound: dict[str, frozenset[str]]) -> str:
    extra = [
        fragment
        for name in dict.fromkeys(_IDENT.findall(segment)) if name in bound
        for fragment in sorted(bound[name])
    ]
    return " ".join([segment, *extra])


def _calls_naming_the_namespace(path: Path) -> list[tuple[int, str]]:
    """Every call expression in `path` whose source names the run namespace,
    with the text of any run-ref-derived local it references appended.

    A call is the tightest scope that still holds a whole git invocation
    together, whatever shape it takes in this tree — an argv list
    (`subprocess.run(["git", ...])`), a varargs helper (`self._git("rebase",
    ...)`), or an f-string command line (`f"gh pr create ..."`). Nested calls
    yield the same text more than once, which costs nothing.

    The appended binding text is what closes the two-statement escape: fed
    `shell.run(cmd)` where `cmd` was built from a run ref, the call reads as if
    the command line had been written inline.
    """
    source = path.read_text(encoding="utf-8")
    if not _NAMES_RUN_NAMESPACE.search(source):
        return []                      # nothing to parse: most of the tree
    tree = ast.parse(source)
    found = []
    for scope in _scopes(tree):
        bound = _run_ref_bindings(source, scope)
        for node in _own_nodes(scope):
            if not isinstance(node, ast.Call):
                continue
            segment = _with_bindings(
                ast.get_source_segment(source, node) or "", bound
            )
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


# Every module that HANDLES the run namespace, per `core/run_ref.py`'s own
# docstring: the one that owns the name, the endpoint that accepts pushes onto
# it, the client that pushes, the run host that materializes from it, the CLI
# that dispatches, and `mship close`, which deletes it. A scan that does not
# reach one of these cannot say anything about it.
NAMESPACE_MODULES = (
    "core/run_ref.py",
    "core/git_receive.py",
    "core/run_transfer.py",
    "core/remote_exec.py",
    "cli/exec.py",
    "cli/worktree.py",
)


def test_the_scan_actually_reaches_every_module_that_handles_the_namespace():
    """Guards the guard: a scan that matched nothing would pass the test above
    for free, and one that matched only SOME modules would pass it for the
    modules it never opened.

    That was the real hole. `core/remote_exec.py` resets the run host's
    worktree to the ref, `cli/exec.py` pushes it and `cli/worktree.py` deletes
    it — and none of the three ever writes `refs/mship/run` or calls
    `run_ref(...)` directly, so the original `\\brun_ref\\(` pattern opened none
    of them. A merge added in any of them was invisible.
    """
    seen = {
        module for module in NAMESPACE_MODULES
        if _NAMES_RUN_NAMESPACE.search((SRC / module).read_text(encoding="utf-8"))
    }
    assert set(NAMESPACE_MODULES) == seen, sorted(set(NAMESPACE_MODULES) - seen)


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


def _scan_module(tmp_path: Path, body: str) -> list[str]:
    """Run the WHOLE scanner — parser, bindings and patterns — over `body`,
    exactly as `test_no_code_path_branches_...` runs it over `src/mship`."""
    path = tmp_path / "sample.py"
    path.write_text(body, encoding="utf-8")
    return [
        label
        for _lineno, segment in _calls_naming_the_namespace(path)
        for label, pattern in _FORMS_HISTORY.items()
        if pattern.search(segment)
    ]


@pytest.mark.parametrize("body", [
    # The escape that mattered: split over two statements, neither expression
    # holding both the command and the ref. Confirmed to survive the previous
    # scanner with the whole suite green.
    'def go(shell, task, repo, root):\n'
    '    cmd = f"git merge {run_ref(task, repo)}"\n'
    '    return shell.run(cmd, cwd=root)\n',
    # And a chain, so one hop is not the limit.
    'def go(shell, task, repo, root):\n'
    '    ref = run_ref(task, repo)\n'
    '    cmd = "git branch keepme " + ref\n'
    '    return shell.run(cmd, cwd=root)\n',
    # Reached through an imported helper rather than the literal name — the
    # only way `cli/exec.py` and `cli/worktree.py` ever touch the namespace.
    'def go(shell, root, **kw):\n'
    '    ref = push_run_ref(shell, root, **kw)\n'
    '    return shell.run(f"git merge {ref}", cwd=root)\n',
])
def test_the_detector_fires_when_the_command_is_built_across_statements(tmp_path, body):
    assert _scan_module(tmp_path, body), body


@pytest.mark.parametrize("body", [
    # `git merge-base` forms no history, and `core/remote_preflight.py` — now
    # in scope for this scan — runs one on every clean repo.
    'def go(shell, root, task, repo):\n'
    '    ref = run_ref(task, repo)\n'
    '    return shell.run(f"git merge-base --is-ancestor {ref} HEAD", cwd=root)\n',
    # One function's `cmd` must not stand in for another's.
    'def a(shell, root):\n'
    '    cmd = "git merge origin/main"\n'
    '    return shell.run(cmd, cwd=root)\n'
    'def b(shell, root, task, repo):\n'
    '    cmd = run_ref(task, repo)\n'
    '    return shell.run(cmd, cwd=root)\n',
])
def test_the_detector_does_not_cry_wolf(tmp_path, body):
    """A tripwire that fires on legitimate work gets disabled, which is a
    slower way of having no tripwire at all."""
    assert _scan_module(tmp_path, body) == [], body


# The gate that decides whether `finish` pushes a branch and opens a PR for a
# repo at all: `PRManager.count_commits_ahead` (`core/pr.py`) counts real
# commits between base and the task's own branch, purely from that branch's
# own git history; `finish` (`cli/worktree.py`) is the CLI command that acts on
# the result. Neither can treat scratch state as a substitute for a real commit
# if neither can name the scratch namespace — that is the property this pins.
#
# Pinned per FUNCTION, not per module. `cli/worktree.py` as a whole DOES know
# the namespace exists: `close` calls `cleanup_run_refs` to delete the task's
# scratch refs from the run host (spec ac8). Deleting them is the opposite of
# treating them as history, so a module-wide assertion would be false as
# written — and would have to be deleted rather than tightened the first time
# anyone read it.
FINISH_GATE_FUNCTIONS = (
    ("core/pr.py", "count_commits_ahead"),
    ("cli/worktree.py", "finish"),
)


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{path.name} has no function {name!r}; update FINISH_GATE_FUNCTIONS")


def test_the_real_commits_gate_and_finish_do_not_consult_the_run_namespace():
    """ac14's third clause: `finish` still requires real commits.

    The scratch ref never reaches the operator's own branch (ac2, pinned in
    `test_run_transfer.py::test_local_state_is_identical_before_and_after` and
    `::test_the_commit_is_on_no_branch`), so today `count_commits_ahead` has
    nothing to see. The risk this guards is a FUTURE one: either function
    growing a special case keyed on the run namespace — e.g. "a run ref exists
    for this repo, treat it as ready to finish" — which would let scratch state
    stand in for a commit the operator never made.

    Matched with the same broadened pattern the scan above uses, so the case
    that actually reaches these two — an imported helper like
    `push_run_ref` / `cleanup_run_refs`, never the literal ref string — is
    caught rather than read straight past.
    """
    for module, function in FINISH_GATE_FUNCTIONS:
        path = SRC / module
        assert path.exists(), f"{module} moved; update FINISH_GATE_FUNCTIONS"
        assert not _NAMES_RUN_NAMESPACE.search(_function_source(path, function)), (
            f"{module}:{function} now names the run scratch namespace. `finish` "
            f"must keep deciding whether to push/open a PR from real branch "
            f"history alone (spec ac14) — if it genuinely needs to consult run "
            f"refs, that is a spec-level decision, not a silent one."
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
