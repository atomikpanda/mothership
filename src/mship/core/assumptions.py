"""L1 store for product assumptions (product-assumptions-wave-2).

A single markdown-canonical doc, `docs/product_assumptions.md` (or `.md.enc`
under `mode="encrypted"`), holding the axis/options/position/triggers table
that a planning agent must disposition against. Mirrors `SpecStorage`'s
atomic-write + suffix-driven-read pattern and reuses `core.spec_key` for
optional at-rest encryption — no second encryption implementation."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mship.core import spec_key
from mship.core.plan import _normalize_axis
from mship.util.git import GitRunner

AssumptionMode = Literal["committed", "local", "encrypted"]

DOC_NAME = "product_assumptions.md"
ENC_SUFFIX = ".enc"
SOFT_CAP = 20

_COLUMNS = ("axis", "options", "position", "triggers")


@dataclass(frozen=True)
class AssumptionRow:
    axis: str
    options: str
    position: str
    triggers: str


# The 7 seed rows (with the backtest's 4 wording refinements folded in). Single
# seed definition — `AssumptionStore.seed()` is the only writer of these.
SEED_ROWS: tuple[AssumptionRow, ...] = (
    AssumptionRow(
        axis="repo topology",
        options="single / mono / meta",
        position=(
            "**meta** — the *workspace-under-management* is N repos, "
            "independent histories, shipped together"
        ),
        triggers="git/*, workspace/*, clone, branch, push",
    ),
    AssumptionRow(
        axis="credential locus",
        options="worker / relay / egress host",
        position="**attach-at-relay**; worker never holds the real credential",
        triggers="auth, token, push, credential",
    ),
    AssumptionRow(
        axis="execution locus",
        options="local / disposable cloud worker",
        position="**both**; cloud is the priority path",
        triggers="run, dispatch, worker, remote",
    ),
    AssumptionRow(
        axis="state durability",
        options="in-session / durable journal",
        position="**journal**; must survive process death",
        triggers="state, journal, persist, resume",
    ),
    AssumptionRow(
        axis="review surface",
        options="terminal / async client",
        position=(
            "**undecided — flag it** (D1). Disposition rule: *covered* when a "
            "plan surfaces/respects the open choice; *not-covered* only when "
            "it silently declares one surface canonical"
        ),
        triggers="review, approve, verdict, UI",
    ),
    AssumptionRow(
        axis="agent stream",
        options="live stream / journal-backed async",
        position=(
            "**journal-backed**. Scope: all run output (shell + agent), not "
            "agent-session output only"
        ),
        triggers="stream, output, follow, log",
    ),
    AssumptionRow(
        axis="dispatched model",
        options="orchestrator-class / weaker",
        position="**assume weaker**. N/A unless the plan itself dispatches agent work",
        triggers="(applies whenever the plan dispatches agent work)",
    ),
)


class AssumptionStore:
    """Markdown-canonical store for `docs/product_assumptions.md[.enc]`."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        docs_dir: str = "docs",
        mode: AssumptionMode = "committed",
        git: GitRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.docs_dir = docs_dir
        self.mode: AssumptionMode = mode
        self._git = git or GitRunner()

    @property
    def path(self) -> Path:
        base = self.workspace_root / self.docs_dir / DOC_NAME
        if self.mode == "encrypted":
            return base.with_name(base.name + ENC_SUFFIX)
        return base

    # --- read --------------------------------------------------------
    def load(self) -> list[AssumptionRow]:
        if not self.path.is_file():
            return []
        text = self._decode(self.path)
        return _parse_table(text)

    def axes(self) -> list[str]:
        return [_normalize_axis(row.axis) for row in self.load()]

    def render(self) -> str:
        rows = self.load()
        lines = ["## Assumptions checked"]
        for row in rows:
            lines.append(f"- {row.axis} — options: {row.options} · position: {row.position}")
        return "\n".join(lines) + "\n"

    # --- write -------------------------------------------------------
    def save(self, rows: list[AssumptionRow]) -> str | None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        text = _render_table(rows)
        if self.mode == "encrypted":
            key = spec_key.load_or_generate_key(self.workspace_root, git=self._git)
            self._atomic_write_bytes(path, spec_key.encrypt(key, text))
        else:
            self._atomic_write_bytes(path, text.encode("utf-8"))
            if self.mode == "local":
                self._git.add_to_gitignore(self.workspace_root, f"{self.docs_dir}/{DOC_NAME}")
        if len(rows) > SOFT_CAP:
            return (
                f"warning: {len(rows)} assumption rows exceeds the soft cap of "
                f"{SOFT_CAP}; consider pruning stale axes"
            )
        return None

    def seed(self) -> list[AssumptionRow]:
        if self.path.is_file():
            return self.load()
        self.save(list(SEED_ROWS))
        return list(SEED_ROWS)

    # --- helpers -------------------------------------------------------
    def _decode(self, path: Path) -> str:
        if path.name.endswith(ENC_SUFFIX):
            key = spec_key.load_key(self.workspace_root)
            if key is None:
                raise spec_key.SpecKeyMissing(
                    f"encrypted assumptions store requires a key at "
                    f"{spec_key.keyfile_path(self.workspace_root)}, but none was found."
                )
            return spec_key.decrypt(key, path.read_bytes())
        return path.read_text()

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with open(fd, "wb") as f:
                f.write(data)
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise


# A literal `|` in a cell would otherwise be read as a column separator and
# silently drop the whole row on load. Escape on render, unescape on parse, and
# split only on UNescaped pipes — free-text cells (position/options edited by
# hand via `mship assumptions`) are round-trip safe (Greptile #448 Wave-2 review).
_ESCAPED_PIPE = "\\|"


def _split_unescaped_pipes(body: str) -> list[str]:
    """Split a table row body on `|`, treating `\\|` as a literal pipe."""
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if body[i] == "|":
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(body[i])
        i += 1
    cells.append("".join(buf))
    return cells


def _render_table(rows: list[AssumptionRow]) -> str:
    header = "| " + " | ".join(_COLUMNS) + " |"
    separator = "| " + " | ".join("--" for _ in _COLUMNS) + " |"
    lines = [header, separator]
    for row in rows:
        cells = [getattr(row, col).replace("|", _ESCAPED_PIPE) for col in _COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def resolve_mode(workspace_root: Path) -> AssumptionMode:
    """Best-effort read of `assumption_storage` from the workspace's
    mothership.yaml. Mirrors `spec_storage.resolve_mode` (single source of
    truth is the workspace config; no config file -> committed; an invalid
    value fails loud via the config model)."""
    cfg_path = Path(workspace_root) / "mothership.yaml"
    if not cfg_path.is_file():
        return "committed"
    from mship.core.config import ConfigLoader

    config = ConfigLoader.load(cfg_path, require_paths=False)
    return getattr(config, "assumption_storage", "committed")


def _parse_table(text: str) -> list[AssumptionRow]:
    lines = [line for line in text.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return []
    rows = []
    for line in lines[2:]:  # skip header + separator
        body = line.strip()
        body = body[1:] if body.startswith("|") else body
        body = body[:-1] if body.endswith("|") else body
        cells = [cell.strip() for cell in _split_unescaped_pipes(body)]
        if len(cells) != len(_COLUMNS):
            continue
        rows.append(AssumptionRow(**dict(zip(_COLUMNS, cells))))
    return rows
