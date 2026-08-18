"""Ambient-state invariants for daemon serve paths (#472).

A daemon serves MANY workspaces, so any read of `Path.cwd()`, `os.getcwd`,
`Path(".")`, or workspace-selecting env on a serve-reachable code path is a
latent wrong-workspace bug. The swept module set is DERIVED transitively from
the serve/host-app import graph — a hand-maintained list verifiably missed
serve-reachable modules (topology.py's egress probe sat behind GET
/net/topology) — with a superset canary so a walker bug can't silently shrink
coverage, plus detector self-tests so the sweep itself is trustworthy.

Every allowlist entry names its reason; the runtime poison test
(tests/core/daemon/test_poison_cwd.py) covers what static analysis can't —
that the allowlisted call sites are never used to SELECT a workspace.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PKG = "mship"

ROOTS = ["mship.core.serve", "mship.core.daemon.host_app", "mship.core.workspace_context"]

# (module suffix, qualname prefix, kind) -> reason. Kind: cwd|dot|env
ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("mship/util/shell.py", "*", "env"): "#473 seam: subprocess env construction, not workspace selection",
    ("mship/core/relay/token.py", "*", "env"): "#471 seam: MSHIP_SERVE_TOKEN host auth material",
    ("mship/core/daemon/host_app.py", "ensure_host_token", "env"): "#472 seam: MSHIP_SERVE_TOKEN host auth material",
    ("mship/core/daemon/host_app.py", "load_gh_app_credentials", "env"): "#472 seam: GitHub App host auth material",
    ("mship/core/gh_auth.py", "*", "env"): "#471 seam: broker URL/token + GH token, host-level auth",
    ("mship/core/evidence_url.py", "*", "env"): "env augmentation (GIT_SSH_COMMAND), not selection",
    ("mship/core/run_host/store.py", "RunHostStore.get", "env"): "run-host connection env override (per-machine, not workspace selection)",
    ("mship/core/topology.py", "probe_topology", "env"): "injectable host-env default (relay/run-host edges), not workspace selection",
    ("mship/core/doctor.py", "DoctorChecker.run", "dot"): "CLI-invocation fallback; serve's /doctor passes workspace_root (serve.py _doctor_payload)",
    ("mship/core/workspace_context.py", "_resolve_state_dir", "env"): "strips GIT_* vars — never selects",
    ("mship/core/daemon/run.py", "*", "env"): "#470 process-env boundary (socket path for the daemon itself)",
    ("mship/core/config.py", "ConfigLoader.discover", "env"): "CLI discovery only — daemon paths use build_workspace_context (poison test proves)",
    ("mship/core/pr.py", "PRManager.__init__", "dot"): "CLI-boundary default; serve/daemon pass workspace_root explicitly (poison test proves)",
}


def _module_file(mod: str) -> Path | None:
    rel = Path(*mod.split("."))
    for cand in (SRC / rel / "__init__.py", SRC / (str(rel) + ".py")):
        if cand.is_file():
            return cand
    return None


def _imports_of(path: Path, mod: str) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    pkg_parts = mod.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(PKG):
                    found.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - node.level + (0 if path.name != "__init__.py" else 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target.startswith(PKG):
                found.add(target)
                for a in node.names:
                    found.add(f"{target}.{a.name}")
    return found


def derive_closure(roots: list[str]) -> dict[str, Path]:
    """Transitive in-package import closure, module name -> file."""
    seen: dict[str, Path] = {}
    stack = [r for r in roots if _module_file(r) is not None]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        f = _module_file(mod)
        if f is None:
            continue
        seen[mod] = f
        for imp in _imports_of(f, mod):
            # try both the name and its parent (from X import name-of-def)
            for cand in (imp, imp.rsplit(".", 1)[0]):
                if cand.startswith(PKG) and cand not in seen and _module_file(cand) is not None:
                    stack.append(cand)
    return seen


class _Detector(ast.NodeVisitor):
    def __init__(self):
        self.hits: list[tuple[str, str, int]] = []  # (kind, qualname, line)
        self._stack: list[str] = []

    def _qual(self) -> str:
        return ".".join(self._stack) or "<module>"

    def visit_FunctionDef(self, node):
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr == "cwd" and isinstance(f.value, ast.Name) and f.value.id == "Path":
                self.hits.append(("cwd", self._qual(), node.lineno))
            if f.attr == "getcwd" and isinstance(f.value, ast.Name) and f.value.id == "os":
                self.hits.append(("cwd", self._qual(), node.lineno))
            if f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os":
                self.hits.append(("env", self._qual(), node.lineno))
        if (isinstance(f, ast.Name) and f.id == "Path" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "."):
            self.hits.append(("dot", self._qual(), node.lineno))
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os":
            self.hits.append(("env", self._qual(), node.lineno))
        self.generic_visit(node)


def _detect(source: str) -> list[tuple[str, str, int]]:
    det = _Detector()
    det.visit(ast.parse(source))
    return det.hits


def _allowed(rel: str, qualname: str, kind: str) -> bool:
    for (mod_sfx, qual, k), _reason in ALLOWLIST.items():
        if rel.endswith(mod_sfx) and k == kind and (qual == "*" or qualname.startswith(qual)):
            return True
    return False


def test_no_ambient_reads_on_serve_paths():
    closure = derive_closure(ROOTS)
    violations: list[str] = []
    for mod, f in sorted(closure.items()):
        rel = str(f.relative_to(SRC.parent))
        for kind, qual, line in _detect(f.read_text()):
            if not _allowed(rel, qual, kind):
                violations.append(f"{rel}:{line} [{kind}] in {qual}")
    assert not violations, (
        "ambient-state reads on daemon serve paths (workspace must be an explicit "
        "parameter — #472):\n  " + "\n  ".join(violations)
    )


def test_closure_is_superset_of_known_reachable():
    """Canary: a walker bug must not silently shrink coverage."""
    closure = set(derive_closure(ROOTS))
    known = {
        "mship.core.serve", "mship.core.pr", "mship.core.pr_watcher",
        "mship.core.remote_exec", "mship.core.git_receive", "mship.core.spec_dispatch",
        "mship.core.lifecycle_hooks", "mship.core.topology", "mship.core.gh_auth",
        "mship.core.evidence_url", "mship.core.doctor", "mship.core.run_host.store",
        "mship.core.workspace_context", "mship.util.shell", "mship.core.config",
    }
    missing = known - closure
    assert not missing, f"import-closure walker lost known serve-reachable modules: {sorted(missing)}"


def test_detector_fires_on_synthetic_positive():
    src = (
        "import os\nfrom pathlib import Path\n"
        "def f():\n"
        "    a = Path.cwd()\n"
        "    b = os.getcwd()\n"
        "    c = os.environ.get('MSHIP_WORKSPACE')\n"
        "    d = os.getenv('X')\n"
        "    run(cwd=Path('.'))\n"
    )
    kinds = sorted(k for k, _, _ in _detect(src))
    assert kinds == ["cwd", "cwd", "dot", "env", "env"]


def test_detector_quiet_on_clean_sample():
    src = (
        "from pathlib import Path\n"
        "def f(cwd: Path, env: dict):\n"
        "    return run(cwd=cwd, env=dict(env))\n"
    )
    assert _detect(src) == []
