from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import typer
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static, Tree

from mship.cli.view._base import ViewApp
from mship.core.view.diff_sources import WorktreeDiff, collect_worktree_diff


_LARGE_WORKTREE_THRESHOLD = 20


class DiffView(ViewApp):
    CSS = """
    Tree#diff-tree {
        width: 30%;
        min-width: 24;
        border-right: tall $accent;
    }
    """

    BINDINGS = ViewApp.BINDINGS + [
        Binding("e", "toggle_lockfile", "Expand lockfile", show=True),
        Binding("z", "toggle_worktree", "Toggle worktree", show=True),
        Binding("h", "focus_tree", "Focus tree", show=False),
        Binding("l", "focus_diff", "Focus diff", show=False),
    ]

    def __init__(
        self,
        worktree_paths: Iterable[Path],
        use_delta: bool | None = None,
        scope_to_active_path: Path | None = None,
        **kw,
    ):
        super().__init__(**kw)
        all_paths = list(worktree_paths)
        if scope_to_active_path is not None:
            resolved = Path(scope_to_active_path).resolve()
            filtered = [p for p in all_paths if Path(p).resolve() == resolved]
            # If the active path isn't in the list, fall back to showing everything.
            self._paths = filtered if filtered else all_paths
        else:
            self._paths = all_paths
        if use_delta is None:
            use_delta = shutil.which("delta") is not None
        self._use_delta = use_delta

        # Populated in _refresh_content
        self._worktrees: dict[Path, WorktreeDiff | Exception] = {}
        self._selected: tuple[Path, str] | None = None
        self._expanded_lockfiles: set[tuple[Path, str]] = set()
        self._collapsed_worktrees: set[Path] = set()
        self._ever_mounted: set[Path] = set()

        # Test-only hook: if set, skips collect_worktree_diff and uses the
        # provided mapping directly. Must be a dict[Path, WorktreeDiff].
        self._test_override: dict[Path, WorktreeDiff] | None = None

        # Widget refs (populated in compose)
        self._tree: Tree | None = None
        self._diff_static: Static | None = None
        self._diff_scroll: VerticalScroll | None = None

    # --- Textual lifecycle ---
    def compose(self) -> ComposeResult:
        self._tree = Tree("diff", id="diff-tree")
        self._tree.root.expand()
        self._tree.show_root = False
        self._diff_static = Static("", expand=True)
        self._diff_scroll = VerticalScroll(self._diff_static)
        yield Horizontal(self._tree, self._diff_scroll)

    def on_mount(self) -> None:
        self._refresh_content()
        if self._watch:
            self.set_interval(self._interval, self._refresh_content)

    # --- Data loading ---
    def _load_worktrees(self) -> dict[Path, WorktreeDiff | Exception]:
        if self._test_override is not None:
            return dict(self._test_override)
        out: dict[Path, WorktreeDiff | Exception] = {}
        for p in self._paths:
            try:
                out[p] = collect_worktree_diff(p)
            except Exception as e:  # noqa: BLE001 — view must stay alive
                out[p] = e
        return out

    # --- Refresh ---
    def _refresh_content(self) -> None:
        self._worktrees = self._load_worktrees()
        self._rebuild_tree()
        self._render_selected()

    def _rebuild_tree(self) -> None:
        assert self._tree is not None
        self._tree.clear()
        root = self._tree.root

        for p in self._paths:
            wd = self._worktrees.get(p)
            if isinstance(wd, Exception):
                root.add_leaf(f"▶ {p}  (error: {wd})", data=("err", p))
                continue
            if wd is None:
                continue
            if not wd.files:
                continue
            add_total = sum(f.additions for f in wd.files)
            del_total = sum(f.deletions for f in wd.files)
            label = f"▶ {p}  ·  {len(wd.files)} files  ·  +{add_total} -{del_total}"
            node = root.add(label, data=("wt", p))
            for f in wd.files:
                suffix = "(binary)" if "new binary file" in f.body else f"+{f.additions} -{f.deletions}"
                node.add_leaf(f"{f.path}  {suffix}", data=("file", p, f.path))

            # First-time auto-collapse when large; honour user's toggle afterward.
            if p not in self._ever_mounted:
                self._ever_mounted.add(p)
                if len(wd.files) > _LARGE_WORKTREE_THRESHOLD:
                    self._collapsed_worktrees.add(p)

            if p in self._collapsed_worktrees:
                node.collapse()
            else:
                node.expand()

        # Selection preservation: _selected is a 2-tuple (worktree, file_path)
        prev = self._selected
        self._selected = None
        if prev is not None and len(prev) == 2 and prev[1] is not None:
            # Try to keep the previously selected file
            wd = self._worktrees.get(prev[0])
            if isinstance(wd, WorktreeDiff) and any(f.path == prev[1] for f in wd.files):
                self._selected = (prev[0], prev[1])
        if self._selected is None:
            # Pick first available file in declared path order
            for p in self._paths:
                wd = self._worktrees.get(p)
                if isinstance(wd, WorktreeDiff) and wd.files:
                    self._selected = (p, wd.files[0].path)
                    break

    def _render_selected(self, reset_scroll: bool = False) -> None:
        assert self._diff_static is not None and self._diff_scroll is not None
        if self._selected is None:
            self._diff_static.update(Text("No changes.", justify="center"))
            return
        worktree, file_path = self._selected
        wd = self._worktrees.get(worktree)
        if not isinstance(wd, WorktreeDiff):
            self._diff_static.update(Text(f"error loading {worktree}"))
            return
        file = next((f for f in wd.files if f.path == file_path), None)
        if file is None:
            self._diff_static.update(Text("No changes."))
            return

        key = (worktree, file_path)
        if file.is_lockfile and key not in self._expanded_lockfiles:
            placeholder = (
                f"{file.path}: +{file.additions} -{file.deletions} "
                f"(collapsed — press e to expand)"
            )
            self._diff_static.update(Text(placeholder))
        else:
            rendered = self._render_body(file.body)
            self._diff_static.update(Text.from_ansi(rendered))
        if reset_scroll:
            self._diff_scroll.scroll_to(y=0, animate=False)

    def _render_body(self, body: str) -> str:
        if not self._use_delta or not body:
            return body
        try:
            result = subprocess.run(
                ["delta", "--color-only"],
                input=body,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return result.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            return body

    # --- Tree interaction ---
    def on_tree_node_selected(self, event) -> None:  # Textual sends Tree.NodeSelected
        data = event.node.data
        if not data:
            return
        if data[0] == "file":
            _, worktree, path = data
            self._selected = (worktree, path)
            self._render_selected(reset_scroll=True)
        elif data[0] == "wt":
            _, worktree = data
            if event.node.is_expanded:
                self._collapsed_worktrees.discard(worktree)
            else:
                self._collapsed_worktrees.add(worktree)

    # --- Actions ---
    def action_toggle_lockfile(self) -> None:
        if self._selected is None:
            return
        worktree, file_path = self._selected
        wd = self._worktrees.get(worktree)
        if not isinstance(wd, WorktreeDiff):
            return
        file = next((f for f in wd.files if f.path == file_path), None)
        if file is None or not file.is_lockfile:
            return
        key = (worktree, file_path)
        if key in self._expanded_lockfiles:
            self._expanded_lockfiles.discard(key)
        else:
            self._expanded_lockfiles.add(key)
        self._render_selected()

    def action_toggle_worktree(self) -> None:
        assert self._tree is not None
        node = self._tree.cursor_node
        if node is None:
            return
        # Find the ancestor worktree node
        target = node
        while target is not None:
            data = getattr(target, "data", None)
            if data and data[0] == "wt":
                break
            target = target.parent
        if target is None:
            return
        if target.is_expanded:
            target.collapse()
            self._collapsed_worktrees.add(target.data[1])
        else:
            target.expand()
            self._collapsed_worktrees.discard(target.data[1])

    def action_focus_tree(self) -> None:
        if self._tree is not None:
            self._tree.focus()

    def action_focus_diff(self) -> None:
        if self._diff_scroll is not None:
            self._diff_scroll.focus()

    # --- Test helpers ---
    def tree_labels(self) -> list[str]:
        assert self._tree is not None

        def walk(node):
            out = [str(node.label)]
            for c in node.children:
                out.extend(walk(c))
            return out

        return walk(self._tree.root)

    def diff_text(self) -> str:
        assert self._diff_static is not None
        return str(self._diff_static.content)

    def is_worktree_collapsed(self, worktree: Path) -> bool:
        assert self._tree is not None
        for child in self._tree.root.children:
            data = child.data
            if data and data[0] == "wt" and data[1] == worktree:
                return not child.is_expanded
        raise AssertionError(f"worktree {worktree} not in tree")

    def select_file(self, worktree: Path, file_path: str) -> None:
        self._selected = (worktree, file_path)
        self._render_selected(reset_scroll=True)

    def scroll_diff_to(self, y: float) -> None:
        assert self._diff_scroll is not None
        self._diff_scroll.set_scroll(x=None, y=y)

    def diff_scroll_y(self) -> float:
        assert self._diff_scroll is not None
        return self._diff_scroll.scroll_y


def _collect_workspace_worktrees(container) -> list[Path]:
    state = container.state_manager().load()
    if state.current_task and state.current_task in state.tasks:
        task = state.tasks[state.current_task]
        paths = [Path(p) for p in task.worktrees.values()]
        if paths:
            return paths
    return [Path(repo.path) for repo in container.config().repos.values()]


def register(app: typer.Typer, get_container):
    @app.command()
    def diff(
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all worktrees, ignore active_repo"),
    ):
        """Live per-worktree git diff, browsable by file."""
        container = get_container()
        worktree_paths = _collect_workspace_worktrees(container)

        scope_path: Path | None = None
        if not all_:
            state = container.state_manager().load()
            if state.current_task is not None:
                task = state.tasks[state.current_task]
                if task.active_repo is not None and task.active_repo in task.worktrees:
                    scope_path = Path(task.worktrees[task.active_repo])

        view = DiffView(
            worktree_paths=worktree_paths,
            scope_to_active_path=scope_path,
            watch=watch,
            interval=interval,
        )
        view.run()
