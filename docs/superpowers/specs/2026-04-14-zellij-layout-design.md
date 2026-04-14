# `mship layout` — zellij layout scaffolding

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-14

## Purpose

Starting work in a mothership workspace today means manually spawning `mship view status --watch`, `mship view diff --watch`, an editor, a shell, etc. into panes. That's friction every session. A `mship layout` command generates a zellij layout with four tabs (one per phase) wired to the right mship commands out of the box.

## Command Surface

Two subcommands under a new `mship layout` verb:

- `mship layout init [--force]` — write `~/.config/zellij/layouts/mothership.kdl`. Refuse if the file exists unless `--force`. `~/.config/zellij/layouts/` is created if missing.
- `mship layout launch` — exec `zellij --layout mothership` with the current cwd inherited so mship commands find the workspace's `mothership.yaml`.

## Layout

Four tabs, Dev is focused on launch:

### Plan
```
┌──────────────────┬──────────────────┐
│                  │ view spec --watch│
│ agent / shell    ├──────────────────┤
│ (60%)            │ view status      │
└──────────────────┴──────────────────┘
```

### Dev (focused)
```
┌──────────────────┬──────────────────┐
│                  │ view logs --watch│
│ $EDITOR .        ├──────────────────┤
│ (60%)            │ view status      │
│                  │ --watch          │
└──────────────────┴──────────────────┘
```

### Review
```
┌──────────────────────────┬──────────┐
│ view diff --watch        │ zsh      │
│ (70%)                    │ (30%)    │
└──────────────────────────┴──────────┘
```

### Run
```
┌──────────────────┬──────────────────┐
│                  │ view logs --watch│
│ zsh shell        ├──────────────────┤
│ (60%)            │ view status      │
│                  │ --watch          │
└──────────────────┴──────────────────┘
```

## Editor Resolution

Dev tab main pane runs:

```sh
${EDITOR:-$(command -v nvim || command -v vim || command -v vi)} .
```

Invoked via `bash -lc` so login env vars apply.

## File Template

```kdl
layout {
    tab name="Plan" {
        pane split_direction="vertical" {
            pane size="60%"
            pane split_direction="horizontal" size="40%" {
                pane command="mship" { args "view" "spec" "--watch"; }
                pane command="mship" { args "view" "status"; }
            }
        }
    }

    tab name="Dev" focus=true {
        pane split_direction="vertical" {
            pane size="60%" command="bash" {
                args "-lc" "${EDITOR:-$(command -v nvim || command -v vim || command -v vi)} ."
            }
            pane split_direction="horizontal" size="40%" {
                pane command="mship" { args "view" "logs" "--watch"; }
                pane command="mship" { args "view" "status" "--watch"; }
            }
        }
    }

    tab name="Review" {
        pane split_direction="vertical" {
            pane size="70%" command="mship" { args "view" "diff" "--watch"; }
            pane size="30%"
        }
    }

    tab name="Run" {
        pane split_direction="vertical" {
            pane size="60%"
            pane split_direction="horizontal" size="40%" {
                pane command="mship" { args "view" "logs" "--watch"; }
                pane command="mship" { args "view" "status" "--watch"; }
            }
        }
    }
}
```

## Non-Goals

- tmux equivalent. Future work if demand surfaces.
- Per-workspace layout customization. One global layout in `~/.config/zellij/layouts/`.
- Detection of `zellij` binary before `launch`. If it's missing, the exec error is clear enough.
- Embedding a specific agent (Claude Code, etc.) in the Plan tab. User drops their tool of choice.

## Testing

- `mship layout init` writes the file to the expected path; second invocation without `--force` exits 1; with `--force` overwrites.
- File content matches the template (round-trip).
- `mship layout launch` execs `zellij --layout mothership`. Test via a mock that captures the args; don't actually run zellij in CI.

## Implementation Notes

- Use `pathlib.Path.home() / ".config" / "zellij" / "layouts" / "mothership.kdl"` for the target.
- Template lives as a module-level constant in `src/mship/cli/layout.py`.
- `mship layout launch` uses `os.execvp` (not subprocess) so zellij fully replaces the mship process — agent shell stays clean.
