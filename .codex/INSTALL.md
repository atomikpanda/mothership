# Installing Mothership for Codex

Enable the mothership skills bundle in Codex via native skill discovery.
Just clone and symlink.

## Prerequisites

- Git
- [uv](https://docs.astral.sh/uv/) (for the `mship` CLI itself)

## Installation

1. **Install the `mship` CLI:**
   ```bash
   uv tool install git+https://github.com/atomikpanda/mothership.git
   ```

2. **Clone the mothership repository (for skills):**
   ```bash
   git clone https://github.com/atomikpanda/mothership.git ~/.codex/mothership
   ```

3. **Create the skills symlink:**
   ```bash
   mkdir -p ~/.agents/skills
   ln -s ~/.codex/mothership/skills ~/.agents/skills/mothership
   ```

   **Windows (PowerShell):**
   ```powershell
   New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.agents\skills"
   cmd /c mklink /J "$env:USERPROFILE\.agents\skills\mothership" "$env:USERPROFILE\.codex\mothership\skills"
   ```

4. **Restart Codex** (quit and relaunch the CLI) to discover the skills.

## Verify

```bash
ls -la ~/.agents/skills/mothership
```

You should see a symlink (or junction on Windows) pointing to the bundled
skills directory, which contains `working-with-mothership/`, `brainstorming/`,
`writing-plans/`, etc.

## Updating

Refresh the clone and the installed binary:

```bash
cd ~/.codex/mothership && git pull
uv tool upgrade mothership
```

Skills update instantly through the symlink; the `mship` CLI updates via uv.

## What's inside

- **working-with-mothership** — the mship session-start protocol, phase workflow, and command reference.
- **Vendored superpowers skills** (brainstorming, writing-plans, subagent-driven-development, executing-plans, systematic-debugging, test-driven-development, …) — mship-aware: they require an active `mship` task and point subagents at task worktrees, not `main`.

See the repo's [README.md](https://github.com/atomikpanda/mothership#for-ai-agents) for the full bundle.
