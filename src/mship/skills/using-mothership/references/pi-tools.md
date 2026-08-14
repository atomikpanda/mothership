# Pi Tool Mapping

Skills speak in actions ("dispatch a subagent", "create a todo", "read a file"). On Pi these resolve to the tools below.

| Action skills request | Pi equivalent |
| --- | --- |
| Dispatch a subagent (`Subagent (general-purpose):` template) | Use an installed subagent tool such as `subagent` from `pi-subagents` if available |
| Task tracking ("create a todo", "mark complete") | Use an installed todo/task tool if available, otherwise track tasks in the plan or `TODO.md` |

## Subagents

Pi core does not ship a standard subagent tool. The `pi-subagents` package is a
strong optional companion and provides a `subagent` tool with single-agent,
chain, parallel, async, forked-context, and resume/status workflows.

### Resolved model handling

Before dispatch, find the installed `subagent` tool and inspect its schema for a
model selector, then read the stub's resolved model:
- `inherit`: omit the harness model selector; the harness default is intended.
- any other value: pass it unchanged when it exposes a model selector.
- if the available subagent API has no model selector, do not dispatch with
  an explicit value. Report: "mship resolved explicit model '<value>', but this subagent API cannot select a model; set this mode to inherit or use a selector-capable dispatch tool."
Never translate one provider's model name into another.

Do not assume a particular `pi-subagents` version exposes a selector. If no
subagent tool is available and the resolved model is `inherit`, do not fabricate
`Task` calls; execute sequentially in the current session or explain that the
optional subagent capability is not installed.

If the resolved model is explicit and no selector-capable subagent tool is
available, do not fall back to sequential execution because that would silently
ignore the operator's model choice. Report: "mship resolved explicit model '<value>', but this subagent API cannot select a model; set this mode to inherit or use a selector-capable dispatch tool."

## Task lists

Pi core does not ship a standard task-list tool. If a todo/task extension is installed, use its documented tool. Otherwise use Superpowers plan files, checklists in Markdown, or a repo-local `TODO.md` for task tracking. Older Superpowers docs may refer to `TodoWrite`; treat that as the task-tracking action above.
