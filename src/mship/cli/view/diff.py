from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Optional

import typer
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static, Tree

from mship.cli.view._base import ViewApp
from mship.core.view.diff_sources import WorktreeDiff, collect_worktree_diff


_LARGE_WORKTREE_THRESHOLD = 20


_STATUS_STYLES: dict[str, str] = {
    "N": "green",
    "M": "yellow",
    "D": "red",
    "R": "blue",
}


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

    @staticmethod
    def _apply_scope(all_paths: list[Path], scope_path: Path | None) -> list[Path]:
        """Return all_paths filtered to scope_path, or all_paths if scope is None/not found."""
        if scope_path is not None:
            resolved = Path(scope_path).resolve()
            filtered = [p for p in all_paths if Path(p).resolve() == resolved]
            return filtered if filtered else all_paths
        return all_paths

    def __init__(
        self,
        worktree_paths: Iterable[Path] = (),
        use_delta: bool | None = None,
        scope_to_active_path: Path | None = None,
        resolve_paths: Callable[[], tuple[list[Path], Path | None]] | None = None,
        base_branch_by_path: dict[Path, str | None] | None = None,
        **kw,
    ):
        super().__init__(**kw)
        self._resolve_paths = resolve_paths
        self._base_branch_by_path: dict[Path, str | None] = dict(base_branch_by_path or {})
        if resolve_paths is None:
            # Static mode: capture paths at construction time.
            all_paths = list(worktree_paths)
            self._paths = self._apply_scope(all_paths, scope_to_active_path)
            self._scope_to_active_path = scope_to_active_path
        else:
            # Deferred mode: paths will be resolved on each refresh.
            self._paths: list[Path] = []
            self._scope_to_active_path: Path | None = None
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
            # Honour self._paths so resolver-based filtering is respected in tests.
            return {p: v for p, v in self._test_override.items() if p in self._paths}
        out: dict[Path, WorktreeDiff | Exception] = {}
        for p in self._paths:
            try:
                base = self._base_branch_by_path.get(p)
                out[p] = collect_worktree_diff(p, base_branch=base)
            except Exception as e:  # noqa: BLE001 — view must stay alive
                out[p] = e
        return out

    # --- Refresh ---
    def _refresh_content(self) -> None:
        if self._resolve_paths is not None:
            all_paths, scope_path = self._resolve_paths()
            self._paths = self._apply_scope(all_paths, scope_path)
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
                display_path = (
                    f"{f.path} ← {f.old_path}"
                    if f.status == "R" and f.old_path
                    else f.path
                )
                label = Text.assemble(
                    (f.status, _STATUS_STYLES.get(f.status, "")),
                    "  ",
                    display_path,
                    "  ",
                    suffix,
                )
                node.add_leaf(label, data=("file", p, f.path))

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
        task: Optional[str] = typer.Option(None, "--task", help="Task slug (default: picker / current)"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all worktrees, ignore active_repo"),
    ):
        """Live per-worktree git diff (picker when no task specified)."""
        from pathlib import Path as _P
        container = get_container()
        state = container.state_manager().load()

        target_task = task if task is not None else state.current_task
        if task is not None and task not in state.tasks:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            typer.echo(f"Unknown task '{task}'. Known: {known}.", err=True)
            raise typer.Exit(code=1)

        def _resolver() -> tuple[list[Path], Path | None]:
            if target_task is not None and target_task in state.tasks:
                t = state.tasks[target_task]
                all_paths = [Path(p) for p in t.worktrees.values()]
                scope: Path | None = None
                if not all_ and t.active_repo is not None and t.active_repo in t.worktrees:
                    scope = Path(t.worktrees[t.active_repo])
                return all_paths, scope
            return [Path(repo.path) for repo in container.config().repos.values()], None

        if target_task is not None:
            base_by_path: dict[Path, str | None] = {}
            if target_task in state.tasks:
                t = state.tasks[target_task]
                for p in t.worktrees.values():
                    base_by_path[Path(p)] = t.base_branch
            view = DiffView(
                resolve_paths=_resolver,
                base_branch_by_path=base_by_path,
                watch=watch,
                interval=interval,
            )
            view.run()
            return

        # Picker flow.
        from mship.cli.view._picker import TaskPicker, picker_rows
        from mship.core.view.task_index import build_task_index

        workspace_root = _P(container.config_path()).parent
        index = build_task_index(state, workspace_root)
        selected: dict[str, str] = {}
        def _on_select(slug: str) -> None:
            selected["slug"] = slug
        picker = TaskPicker(rows=picker_rows(index), on_select=_on_select, watch=False, interval=interval)
        picker.run()
        chosen = selected.get("slug")
        if chosen is None:
            return

        def _resolver_for(slug: str):
            def inner() -> tuple[list[Path], Path | None]:
                t = state.tasks[slug]
                return [Path(p) for p in t.worktrees.values()], None
            return inner

        chosen_task = state.tasks[chosen]
        base_by_path = {Path(p): chosen_task.base_branch for p in chosen_task.worktrees.values()}
        view = DiffView(
            resolve_paths=_resolver_for(chosen),
            base_branch_by_path=base_by_path,
            watch=watch,
            interval=interval,
        )
        view.run()
