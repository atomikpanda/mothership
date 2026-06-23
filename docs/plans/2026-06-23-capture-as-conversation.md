# Capture-as-conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Spec:** `capture-as-conversation` (approved) — workspace `specs/2026-06-23-capture-as-conversation.md`.

**Goal:** Turn a chat thread into a structured spec. The phone signals intent with a plain message; the host agent drafts via the existing `mship spec draft/apply`; mship links the spec to the thread; the phone offers a "View spec" jump.

**Architecture:** mothership adds `Thread.spec_id` + `MessageStore.link_spec` + a `mship spec from-thread` convenience (seeds the draft prompt from the transcript + links). ground-control adds the DTO field + a "Make this a spec" action (posts a canonical message) + a "View spec ->" affordance. Reuses everything else as-is. JVM/pytest unit tests; screens build-verified.

**Two repos:**
- mothership worktree: `.worktrees/capture-as-conversation/mothership` (Tasks 1, 2)
- ground-control worktree: `.worktrees/capture-as-conversation/ground-control` (Task 3)

`uv run pytest` for mothership; `source ~/toolchains/android-env.sh` before gradle.

---

<!-- mship:task id=1 -->
### Task 1 (mothership): Thread.spec_id + MessageStore.link_spec

**Files:** `src/mship/core/message.py`, `src/mship/core/message_store.py`; tests `tests/core/test_message_store.py`, `tests/core/test_serve.py`.

- [ ] **Step 1: Failing tests** — append to `tests/core/test_message_store.py`:
```python
def test_link_spec_sets_spec_id(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=now)
    s.link_spec(t.id, "my-spec")
    assert s.get(t.id).spec_id == "my-spec"


def test_link_spec_unknown_thread_raises(tmp_path):
    with pytest.raises(KeyError):
        _store(tmp_path).link_spec("nope", "s")
```
and to `tests/core/test_serve.py` (a thread JSON exposes `spec_id`):
```python
def test_thread_exposes_spec_id(tmp_path):
    from mship.core.message_store import MessageStore
    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]
    MessageStore(tmp_path / ".mothership" / "messages").link_spec(tid, "spec-1")
    assert client.get(f"/threads/{tid}").json()["spec_id"] == "spec-1"
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** — in `core/message.py`, add to `Thread` (after `task_slug`):
```python
    spec_id: str | None = None
```
In `core/message_store.py`, add:
```python
    def link_spec(self, thread_id: str, spec_id: str) -> None:
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        thread.spec_id = spec_id
        self.save(thread)
```
(`GET /threads/{id}` already returns `model_dump(mode="json")`, so `spec_id` is exposed automatically — no serve change needed.)

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(core): Thread.spec_id + MessageStore.link_spec`), journal.
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2 (mothership): `mship spec from-thread`

**Files:** `src/mship/cli/spec.py`; test `tests/cli/test_spec.py`.

- [ ] **Step 1: Failing test** — append to `tests/cli/test_spec.py` (mirror its existing `_configured`/CliRunner setup; seed a thread via `MessageStore`):
```python
def test_spec_from_thread_creates_links_and_prompts(_configured):
    from datetime import datetime, timezone
    from mship.core.message_store import MessageStore
    from mship.core.spec_store import SpecStore, SPECS_DIRNAME
    ws = _configured
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    mstore = MessageStore(ws / ".mothership" / "messages")
    t = mstore.create_thread(subject="Add dark mode", text="we should add dark mode", now=now)
    mstore.append(t.id, "agent", "which screens?", now)

    result = runner.invoke(app, ["spec", "from-thread", t.id])
    assert result.exit_code == 0, result.output
    # a spec was created, titled from the subject, and linked to the thread
    spec = SpecStore(ws / SPECS_DIRNAME).find_by_id(mstore.get(t.id).spec_id)
    assert spec is not None and spec.title == "Add dark mode"
    # the printed drafting prompt embeds the transcript
    assert "we should add dark mode" in result.output
    assert "which screens?" in result.output


def test_spec_from_thread_unknown_thread_errors(_configured):
    assert runner.invoke(app, ["spec", "from-thread", "nope"]).exit_code != 0
```
> If `tests/cli/test_spec.py` lacks a `_configured`/`runner`, copy the minimal versions from `tests/cli/test_serve.py`.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** — add a `from-thread` command to the `spec_app` in `src/mship/cli/spec.py` (mirror the `new` command's container/store access):
```python
    @spec_app.command("from-thread")
    def from_thread(
        thread_id: str = typer.Argument(..., help="Thread id to draft a spec from."),
        title: Optional[str] = typer.Option(None, "--title", help="Spec title (default: the thread subject)."),
    ):
        """Create a spec seeded from a chat thread's transcript, linked to the thread."""
        from datetime import datetime, timezone
        from pathlib import Path
        from mship.core.message_store import MessageStore
        from mship.core.spec_draft import build_draft_prompt, new_spec
        from mship.core.spec_store import SpecStore, SPECS_DIRNAME

        container = get_container()
        output = Output()
        workspace_root = Path(container.config_path()).parent
        messages = MessageStore(Path(container.state_dir()) / "messages")
        thread = messages.get(thread_id)
        if thread is None:
            output.error(f"no thread {thread_id!r}")
            raise typer.Exit(1)

        now = datetime.now(timezone.utc)
        spec = new_spec(title or thread.subject.strip() or "captured note", now=now)
        SpecStore(workspace_root / SPECS_DIRNAME).save(spec)
        messages.link_spec(thread_id, spec.id)

        transcript = "\n".join(f"{m.role}: {m.text}" for m in thread.messages)
        output.print(f"created spec {spec.id!r} linked to thread {thread_id!r}. "
                     f"Run the prompt below, then: mship spec apply {spec.id} --from-json <file>, "
                     f"then mship reply {thread_id} \"drafted {spec.id}\".")
        typer.echo(build_draft_prompt(spec.id, transcript))
```
(Confirm `Output` is already imported at the top of `spec.py` — the `new` command uses it; `Optional` likewise.)

- [ ] **Step 4: Run → pass** (`uv run pytest tests/cli/test_spec.py -q`).
- [ ] **Step 5: Commit** (`feat(spec): mship spec from-thread — draft a spec from a chat thread`), journal.
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3 (ground-control): specId DTO + Make-this-a-spec + View-spec

**Files:** `data/dto/ThreadDtos.kt`, `ui/messages/ConversationViewModel.kt`, `ui/messages/ConversationScreen.kt`, `GroundControlApp.kt`; tests `ThreadDtosTest.kt`, `ConversationViewModelTest.kt`.

Work in the **ground-control** worktree.

- [ ] **Step 1: Failing tests** —
  - `ThreadDtosTest.kt`: a `parses_thread_with_spec_id` case (Thread JSON with `"spec_id":"s1"` → `specId == "s1"`; and absent → null).
  - `ConversationViewModelTest.kt`: `request_spec_posts_canonical_message` — load a thread, call `vm.requestSpec()?.join()`, assert the mock saw a POST to `/threads/{id}/messages` whose body contains the canonical text (or that the returned Content includes the posted message). Mirror the existing send test.

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement**
  - `data/dto/ThreadDtos.kt`: in `Thread`, after `taskSlug`, add `@SerialName("spec_id") val specId: String? = null`.
  - `ui/messages/ConversationViewModel.kt`: add
    ```kotlin
    /** Ask the host agent (via the mailbox) to turn this thread into a spec. */
    fun requestSpec(): Job? = send("Please turn this thread into a spec.")
    ```
  - `ui/messages/ConversationScreen.kt`:
    - Add a param `onViewSpec: (specId: String) -> Unit` to `ConversationScreen(...)`.
    - In the `TopAppBar` `actions = { ... }` (add the slot if absent), an IconButton ("Make this a spec", e.g. `Icons.Filled.NoteAdd`/`PostAdd`) calling `vm.requestSpec()`.
    - In Content, when `thread.specId != null`, render a "View spec →" row/button (e.g. above the timeline or in the top bar) calling `onViewSpec(thread.specId!!)`.
  - `GroundControlApp.kt`: in the `thread/{connectionId}/{threadId}` route, pass `onViewSpec = { specId -> nav.navigate("specDetail/$connectionId/$specId") }` to `ConversationScreen` (`connectionId` is already in scope there).

- [ ] **Step 4: Run → pass** (`./gradlew testDebugUnitTest --tests "com.atomikpanda.groundcontrol.ThreadDtosTest" --tests "com.atomikpanda.groundcontrol.ConversationViewModelTest"`), then **build** (`./gradlew assembleDebug`).
- [ ] **Step 5: Commit** (`feat(gc): make-this-a-spec + view-spec link in the conversation`), journal `--repo ground-control`.
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: full verification + phase transition

- [ ] **Step 1:** mothership `uv run pytest -q` (worktree) → green; ground-control `mship test` (or gradle assembleDebug + testDebugUnitTest) → green.
- [ ] **Step 2:** Confirm acceptance criteria against `specs/2026-06-23-capture-as-conversation.md` (ac1 spec_id+link_spec → T1; ac2 from-thread → T2; ac3 DTO → T3; ac4 affordances+nav → T3; ac5 requestSpec → T3; ac6 absent-when-unlinked → T3; ac7 tests → all). Note any gap.
- [ ] **Step 3:** `mship journal "capture-as-conversation implemented; suites green" --action completed --test-state pass` then `mship phase review`.

> Then `mship finish --body-file <path>` to open the PR(s).
<!-- /mship:task -->

---

## Non-goals (from the spec)

Auto-dispatch · agent auto-drafting without the request message · task-steering (slice 4) · notifications · editing the drafted spec from chat · changing the draft/apply lifecycle.
