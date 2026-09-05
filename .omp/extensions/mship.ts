// mship-extension-version: 1
type HookDecision = {
  kind: "context" | "allow" | "deny" | "continue" | "stop" | "error";
  message?: string;
};

const EDIT_TOOLS = new Set(["edit", "write"]);

async function invokeMship(eventName: string, event: unknown, cwd: string): Promise<HookDecision> {
  const process = Bun.spawn({
    cmd: ["mship", "_omp-hook", eventName],
    cwd,
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  });
  process.stdin.write(JSON.stringify(event));
  process.stdin.end();
  const [stdout, _stderr, exitCode] = await Promise.all([
    new Response(process.stdout).text(),
    new Response(process.stderr).text(),
    process.exited,
  ]);
  if (exitCode !== 0) throw new Error("mship adapter exited unsuccessfully");
  const decision = JSON.parse(stdout) as HookDecision;
  if (!decision || typeof decision.kind !== "string") {
    throw new Error("mship adapter returned an invalid decision");
  }
  return decision;
}

export default function mshipExtension(pi: any) {
  pi.on("session_start", async (event: unknown, ctx: { cwd: string }) => {
    try {
      const decision = await invokeMship("session_start", event, ctx.cwd);
      if (decision.kind === "context" && decision.message) {
        pi.sendMessage(
          { customType: "mship-session-context", content: decision.message, display: false },
          { deliverAs: "nextTurn" },
        );
      } else if (decision.kind === "error") {
        pi.logger.warn("Mothership session_start hook failed open");
      }
    } catch {
      pi.logger.warn("Mothership session_start hook failed open");
    }
  });

  pi.on("tool_call", async (event: any, ctx: { cwd: string }) => {
    if (!EDIT_TOOLS.has(event.toolName)) return;
    try {
      const decision = await invokeMship("tool_call", event, ctx.cwd);
      if (decision.kind === "deny") {
        return { block: true, reason: decision.message || "Mothership policy denied this edit" };
      }
      if (decision.kind === "error") {
        pi.logger.warn("Mothership tool_call hook failed open");
      }
    } catch {
      pi.logger.warn("Mothership tool_call hook failed open");
    }
  });

  pi.on("session_stop", async (event: unknown, ctx: { cwd: string }) => {
    try {
      const decision = await invokeMship("session_stop", event, ctx.cwd);
      if (decision.kind === "continue") {
        return { continue: true, additionalContext: decision.message || "Handle pending Mothership inbox work" };
      }
      if (decision.kind === "error") {
        pi.logger.warn("Mothership session_stop hook failed open");
      }
    } catch {
      pi.logger.warn("Mothership session_stop hook failed open");
    }
  });
}
