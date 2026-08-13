"""OMP event normalization and project-local Mothership extension installation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mship.core.codex_hooks import _atomic_write


OMP_EXTENSION_PATH = Path(".omp/extensions/mship.ts")
OMP_EXTENSION_VERSION = 1
# Minimum OMP version verified against the generated extension contract.
OMP_MIN_VERSION = (17, 2, 0)
OMP_EXTENSION_MARKER = f"// mship-extension-version: {OMP_EXTENSION_VERSION}"

OMP_EXTENSION_SOURCE = f'''{OMP_EXTENSION_MARKER}
type HookDecision = {{
  kind: "context" | "allow" | "deny" | "continue" | "stop" | "error";
  message?: string;
}};

const EDIT_TOOLS = new Set(["edit", "write"]);

async function invokeMship(eventName: string, event: unknown, cwd: string): Promise<HookDecision> {{
  const process = Bun.spawn({{
    cmd: ["mship", "_omp-hook", eventName],
    cwd,
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  }});
  process.stdin.write(JSON.stringify(event));
  process.stdin.end();
  const [stdout, _stderr, exitCode] = await Promise.all([
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
    process.exited,
  ]);
  if (exitCode !== 0) throw new Error("mship adapter exited unsuccessfully");
  const decision = JSON.parse(stdout) as HookDecision;
  if (!decision || typeof decision.kind !== "string") {{
    throw new Error("mship adapter returned an invalid decision");
  }}
  return decision;
}}

export default function mshipExtension(pi: any) {{
  pi.on("session_start", async (event: unknown, ctx: {{ cwd: string }}) => {{
    try {{
      const decision = await invokeMship("session_start", event, ctx.cwd);
      if (decision.kind === "context" && decision.message) {{
        pi.sendMessage(
          {{ customType: "mship-session-context", content: decision.message, display: false }},
          {{ deliverAs: "nextTurn" }},
        );
      }} else if (decision.kind === "error") {{
        pi.logger.warn("Mothership session_start hook failed open");
      }}
    }} catch {{
      pi.logger.warn("Mothership session_start hook failed open");
    }}
  }});

  pi.on("tool_call", async (event: any, ctx: {{ cwd: string }}) => {{
    if (!EDIT_TOOLS.has(event.toolName)) return;
    try {{
      const decision = await invokeMship("tool_call", event, ctx.cwd);
      if (decision.kind === "deny") {{
        return {{ block: true, reason: decision.message || "Mothership policy denied this edit" }};
      }}
      if (decision.kind === "error") {{
        pi.logger.warn("Mothership tool_call hook failed open");
      }}
    }} catch {{
      pi.logger.warn("Mothership tool_call hook failed open");
    }}
  }});

  pi.on("session_stop", async (event: unknown, ctx: {{ cwd: string }}) => {{
    try {{
      const decision = await invokeMship("session_stop", event, ctx.cwd);
      if (decision.kind === "continue") {{
        return {{ continue: true, additionalContext: decision.message || "Handle pending Mothership inbox work" }};
      }}
      if (decision.kind === "error") {{
        pi.logger.warn("Mothership session_stop hook failed open");
      }}
    }} catch {{
      pi.logger.warn("Mothership session_stop hook failed open");
    }}
  }});
}}
'''


@dataclass(frozen=True)
class OmpInstallResult:
    status: str
    path: Path


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def extract_omp_edit_targets(event: dict[str, Any]) -> tuple[str, ...]:
    """Extract OMP's derived path/path-list view for edit and write tools."""
    tool_name = event.get("toolName") or event.get("tool_name")
    if tool_name not in {"edit", "write"}:
        return ()
    tool_input = event.get("input") or event.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("guarded edit did not contain a target path")

    targets: list[str] = []
    for key in ("path", "file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            targets.append(value)
    paths = tool_input.get("paths")
    if isinstance(paths, list):
        targets.extend(value for value in paths if isinstance(value, str) and value)
    if not targets:
        raise ValueError("guarded edit did not contain a target path")
    return _deduplicate(targets)


def install_omp_extension(workspace_root: Path) -> OmpInstallResult:
    """Create or deterministically update only `.omp/extensions/mship.ts`."""
    path = Path(workspace_root) / OMP_EXTENSION_PATH
    if path.is_file():
        if path.read_text() == OMP_EXTENSION_SOURCE:
            return OmpInstallResult("up to date", path)
        status = "updated"
    else:
        status = "installed"
    _atomic_write(path, OMP_EXTENSION_SOURCE)
    return OmpInstallResult(status, path)
