import io
import re
import sys
import threading

import pytest

from mship.util.stream_printer import StreamPrinter, _assign_colors


def _drain_capsys(capsys):
    """Read the combined captured text. Works under capsys and capfd."""
    return capsys.readouterr().out


def test_write_pads_repo_to_longest_width(capsys):
    p = StreamPrinter(repos=["api", "worker"], use_color=False)
    p.write("api", "hello\n")
    p.write("worker", "world\n")
    out = _drain_capsys(capsys)
    assert "api     | hello\n" in out     # 3 chars + 3 pad + "  | "
    assert "worker  | world\n" in out     # 6 chars + 0 pad + "  | "


def test_write_with_single_repo(capsys):
    p = StreamPrinter(repos=["only"], use_color=False)
    p.write("only", "line\n")
    out = _drain_capsys(capsys)
    assert "only  | line\n" in out


def test_write_empty_repo_list(capsys):
    """Edge case: width=0 still produces a parseable prefix."""
    p = StreamPrinter(repos=[], use_color=False)
    p.write("x", "z\n")
    out = _drain_capsys(capsys)
    assert "x  | z\n" in out


def test_write_strips_trailing_newlines_but_keeps_inner(capsys):
    p = StreamPrinter(repos=["api"], use_color=False)
    p.write("api", "line1\n")
    p.write("api", "line2")          # no trailing newline
    p.write("api", "line3\r\n")      # CRLF
    out = _drain_capsys(capsys)
    assert "api  | line1\n" in out
    assert "api  | line2\n" in out
    assert "api  | line3\n" in out


def test_use_color_true_adds_ansi(capsys):
    p = StreamPrinter(repos=["api"], use_color=True)
    p.write("api", "hello\n")
    out = _drain_capsys(capsys)
    assert "\x1b[" in out             # ANSI CSI present
    assert "hello" in out


def test_use_color_false_no_ansi(capsys):
    p = StreamPrinter(repos=["api"], use_color=False)
    p.write("api", "hello\n")
    out = _drain_capsys(capsys)
    assert "\x1b[" not in out


def test_use_color_auto_detects_isatty(capsys, monkeypatch):
    """When use_color is unset, default to sys.stdout.isatty()."""
    # capsys makes stdout non-tty; auto-detection should disable color.
    p = StreamPrinter(repos=["api"])
    p.write("api", "hello\n")
    out = _drain_capsys(capsys)
    assert "\x1b[" not in out


def test_assign_colors_deterministic():
    """Same repo list in any order produces the same repo->color mapping."""
    c1 = _assign_colors(["b", "a", "c"])
    c2 = _assign_colors(["a", "c", "b"])
    assert c1 == c2
    # Three repos → three distinct colors
    assert len({c1["a"], c1["b"], c1["c"]}) == 3


def test_assign_colors_cycles_palette_beyond_six_repos():
    """Palette is 6 colors; 8 repos should cycle without crashing."""
    repos = [f"r{i}" for i in range(8)]
    colors = _assign_colors(repos)
    assert len(colors) == 8
    assert all(isinstance(c, str) for c in colors.values())


def test_thread_safety_no_line_tearing(capsys):
    """10 threads × 100 writes each = 1000 lines. Every captured line
    must match the valid prefix pattern; no mid-line interleaving."""
    p = StreamPrinter(repos=["api", "worker"], use_color=False)

    def _writer(repo, n):
        for i in range(n):
            p.write(repo, f"line-{i}-{repo}\n")

    threads = [
        threading.Thread(target=_writer, args=("api", 500)),
        threading.Thread(target=_writer, args=("worker", 500)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = _drain_capsys(capsys)
    pattern = re.compile(r"^(api|worker)\s*\| line-\d+-(api|worker)$")
    non_empty = [ln for ln in out.splitlines() if ln]
    assert len(non_empty) == 1000
    for ln in non_empty:
        m = pattern.match(ln)
        assert m is not None, f"line did not match: {ln!r}"
        # repo name in prefix must match repo name in content
        assert m.group(1) == m.group(2), f"prefix/content mismatch: {ln!r}"
