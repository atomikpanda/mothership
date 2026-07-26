import json
import re
import shlex
from pathlib import Path
from typing import NamedTuple

from mship.core.evidence_store import IMAGE_EXTS, is_stored_ref, resolve_evidence_mode
from mship.core.gh_auth import git_cred_args, create_pr_via_httpx, get_default_branch_via_httpx
from mship.util.shell import ShellRunner


class PrStateResult(NamedTuple):
    """Result of `PRManager.check_pr_state`.

    `state` is one of `merged` / `closed` / `open` / `unknown`. `reason` is
    empty for known states; for `unknown` it's a classified label
    (`rate limited`, `gh not authenticated`, `network error`, `not found`,
    `unmapped state: <raw>`, `gh not installed`, or `other: <excerpt>`).
    Callers include `reason` in log messages so users can act on the cause.
    """
    state: str
    reason: str


def _classify_pr_state_reason(returncode: int, stderr: str, raw_state: str) -> str:
    """Classify why `gh pr view` produced an unknown state. See #73."""
    if returncode == 127:
        return "gh not installed"
    if returncode == 0 and raw_state:
        return f"unmapped state: {raw_state.strip()}"
    s = stderr.lower()
    if "rate limit" in s:
        return "rate limited"
    if (
        "authentication" in s
        or "not logged in" in s
        or "gh auth login" in s
    ):
        return "gh not authenticated"
    if (
        "could not resolve host" in s
        or "network is unreachable" in s
        or "connection timed out" in s
    ):
        return "network error"
    if (
        "not found" in s
        or "could not find pull request" in s
        or "could not resolve to a pullrequest" in s
        or "http 404" in s
    ):
        return "not found"
    excerpt = stderr.strip()[:80]
    return f"other: {excerpt}" if excerpt else "other: (no stderr)"


_REMOTE_URL_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)


def _parse_github_slug(remote_url: str) -> tuple[str, str] | None:
    m = _REMOTE_URL_RE.search(remote_url.strip())
    if not m:
        return None
    return m.group("owner"), m.group("repo")


def _is_graphql_rate_limit(stderr: str) -> bool:
    s = stderr.lower()
    return (
        ("graphql" in s and "rate limit" in s)
        or "secondary rate limit" in s
    )


class PRManager:
    """Create and manage PRs via the gh CLI."""

    def __init__(self, shell: ShellRunner) -> None:
        self._shell = shell
        self._gh_usable_cache: bool | None = None

    def check_gh_available(self) -> None:
        result = self._shell.run("gh auth status", cwd=Path("."))
        if result.returncode == 127:
            raise RuntimeError(
                "gh CLI not found. Install it: https://cli.github.com"
            )
        if result.returncode != 0:
            raise RuntimeError(
                "gh CLI not authenticated. Run `gh auth login` first."
            )
        # Seed the cache so a later gh_usable() (e.g. in create_pr) doesn't
        # re-run `gh auth status` in the no-token path.
        self._gh_usable_cache = True

    def gh_usable(self) -> bool:
        """True if gh is installed and authenticated. Cached per instance —
        gh availability does not change within a single mship invocation."""
        if self._gh_usable_cache is None:
            self._gh_usable_cache = (
                self._shell.run("gh auth status", cwd=Path(".")).returncode == 0
            )
        return self._gh_usable_cache

    def push_branch(self, repo_path: Path, branch: str, token: str | None = None) -> None:
        prefix, env = "", None
        if token:
            args, env = git_cred_args(token)
            prefix = " ".join(shlex.quote(a) for a in args) + " "
        result = self._shell.run(
            f"git {prefix}push -u origin {shlex.quote(branch)}",
            cwd=repo_path, env=env,
        )
        if result.returncode != 0:
            hint = ""
            if token is None and "could not read" in result.stderr.lower():
                hint = " — set GH_TOKEN/GITHUB_TOKEN or pass --token"
            raise RuntimeError(
                f"Failed to push branch '{branch}': {result.stderr.strip()}{hint}"
            )

    def ensure_upstream(self, repo_path: Path, branch: str) -> None:
        """Ensure `branch`'s tracking ref resolves. No-op when already set.

        `git push -u` normally sets tracking; this is belt-and-suspenders
        so `mship audit` doesn't report `no_upstream` after a finish where
        push succeeded but tracking config somehow wasn't written.
        """
        upstream_ref = f"{branch}@{{u}}"
        check = self._shell.run(
            f"git rev-parse --abbrev-ref --symbolic-full-name {shlex.quote(upstream_ref)}",
            cwd=repo_path,
        )
        if check.returncode == 0:
            return
        result = self._shell.run(
            f"git branch --set-upstream-to=origin/{shlex.quote(branch)} {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to set upstream for branch '{branch}': {result.stderr.strip()}"
            )

    def create_pr(
        self, repo_path: Path, branch: str, title: str, body: str,
        base: str | None = None, token: str | None = None,
    ) -> str:
        if not self.gh_usable():
            if not token:
                raise RuntimeError(
                    "gh CLI not available and no GH_TOKEN/GITHUB_TOKEN found. "
                    "Install gh, or set a token (repo scope) / pass --token."
                )
            remote = self._shell.run("git remote get-url origin", cwd=repo_path)
            slug = _parse_github_slug(remote.stdout) if remote.returncode == 0 else None
            if slug is None:
                raise RuntimeError(
                    "Could not determine owner/repo from origin remote for REST PR creation."
                )
            owner, repo = slug
            effective_base = base or get_default_branch_via_httpx(token, owner, repo)
            return create_pr_via_httpx(
                token, owner, repo, head=branch, base=effective_base,
                title=title, body=body,
            )
        # --- existing gh pr create path stays here, unchanged ---
        safe_title = shlex.quote(title)
        safe_body = shlex.quote(body)
        cmd = (
            f"gh pr create --title {safe_title} --body {safe_body} "
            f"--head {shlex.quote(branch)}"
        )
        if base is not None:
            cmd += f" --base {shlex.quote(base)}"
        result = self._shell.run(cmd, cwd=repo_path)
        if result.returncode != 0:
            stderr_lower = result.stderr.lower()
            if "already exists" in stderr_lower and "pull request" in stderr_lower:
                existing = self.list_pr_for_branch(repo_path, branch)
                if existing is not None:
                    return existing
            if _is_graphql_rate_limit(result.stderr):
                rest_url = self._create_pr_via_rest(
                    repo_path, branch, title, body, base,
                )
                if rest_url is not None:
                    return rest_url
                # REST also failed — surface the original graphql error
                # since that's the cause users need to see.
                raise RuntimeError(
                    f"Failed to create PR (rate-limited; REST fallback also failed): "
                    f"{result.stderr.strip()}"
                )
            raise RuntimeError(
                f"Failed to create PR: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _create_pr_via_rest(
        self, repo_path: Path, branch: str, title: str, body: str,
        base: str | None,
    ) -> str | None:
        """Call GitHub's REST endpoint for PR creation (no GraphQL quota).

        Returns the PR html_url on success, or None on any failure so the
        caller can surface the original rate-limit error.
        """
        remote = self._shell.run("git remote get-url origin", cwd=repo_path)
        if remote.returncode != 0:
            return None
        slug = _parse_github_slug(remote.stdout)
        if slug is None:
            return None
        owner, repo = slug
        cmd = (
            f"gh api repos/{owner}/{repo}/pulls -X POST "
            f"-f title={shlex.quote(title)} "
            f"-f head={shlex.quote(branch)} "
            f"-f body={shlex.quote(body)}"
        )
        if base is not None:
            cmd += f" -f base={shlex.quote(base)}"
        result = self._shell.run(cmd, cwd=repo_path)
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        url = payload.get("html_url")
        return url if isinstance(url, str) and url else None

    def count_commits_ahead(self, repo_path: Path, base: str, branch: str) -> int:
        """Return the number of commits on `branch` not on `base`.

        Uses `origin/<base>` so the comparison is against the remote (same
        reference gh will use). Returns 0 on any git failure (fail-closed: a
        caller treating 0 as "empty" will surface a clear error instead of
        attempting a doomed push).
        """
        spec = f"origin/{base}..{branch}"
        result = self._shell.run(
            f"git rev-list --count {shlex.quote(spec)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return 0
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return 0

    def check_merged_into_base(self, repo_path: Path, branch: str, base: str) -> bool:
        """True if `branch` is an ancestor of `base` (i.e. already merged).

        Uses `git merge-base --is-ancestor`: exit 0 = ancestor, 1 = not, >1 = error.
        Any error → False (conservative).
        """
        result = self._shell.run(
            f"git merge-base --is-ancestor {shlex.quote(branch)} {shlex.quote(base)}",
            cwd=repo_path,
        )
        return result.returncode == 0

    def check_pushed_to_origin(self, repo_path: Path, branch: str) -> bool:
        """True if `branch` exists on origin at the exact same SHA as local HEAD.

        Any error or mismatch → False (conservative).
        """
        local = self._shell.run(
            f"git rev-parse {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if local.returncode != 0:
            return False
        local_sha = local.stdout.strip()

        remote = self._shell.run(
            f"git ls-remote origin {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if remote.returncode != 0:
            return False
        # Output: "<sha>\trefs/heads/<branch>\n"
        for line in remote.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip() == local_sha:
                return True
        return False

    def verify_base_exists(self, repo_path: Path, base: str) -> bool:
        """Return True if `base` exists as a head on origin, else False.

        Network/auth failures are treated as False (fail-closed).
        """
        result = self._shell.run(
            f"git ls-remote --heads origin {shlex.quote(base)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())

    def get_merge_commit(self, pr_url: str) -> str | None:
        """Return the integration-side commit SHA for a merged PR, or None.

        Works for merge / squash / rebase styles — gh stores the resulting
        commit on the base branch in `mergeCommit.oid` regardless of style.
        Returns None on any failure (PR not merged, gh down, parse error).
        """
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json mergeCommit -q .mergeCommit.oid",
            cwd=Path("."),
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    def fetch_remote_branch(self, repo_path: Path, base: str) -> bool:
        """Refresh `origin/<base>` from the remote. False on network/auth failure."""
        result = self._shell.run(
            f"git fetch origin {shlex.quote(base)}",
            cwd=repo_path,
        )
        return result.returncode == 0

    def list_pr_for_branch(self, repo_path: Path, branch: str) -> str | None:
        """Return the URL of any PR (open/closed/merged) whose head is `branch`, or None.

        Used to:
        - Pre-check whether a PR already exists before calling `create_pr`
          (idempotent retry after mid-loop crash).
        - Fallback-harvest on `gh pr create`'s `already exists` error.
        """
        result = self._shell.run(
            f"gh pr list --head {shlex.quote(branch)} --state all "
            f"--json url -q '.[0].url'",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        return url or None

    def check_pr_state(self, pr_url: str) -> PrStateResult:
        """Return (state, reason) for a PR URL.

        state: 'merged' | 'closed' | 'open' | 'unknown'.
        reason: empty string for known states; classified label for unknown
        (see `_classify_pr_state_reason`).
        """
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json state -q .state",
            cwd=Path("."),
        )
        raw = result.stdout.strip().upper()
        mapping = {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open"}
        if result.returncode == 0 and raw in mapping:
            return PrStateResult(state=mapping[raw], reason="")
        reason = _classify_pr_state_reason(
            returncode=result.returncode,
            stderr=result.stderr,
            raw_state=raw if result.returncode == 0 else "",
        )
        return PrStateResult(state="unknown", reason=reason)

    def issue_state(self, slug: str, number: int) -> str:
        """'open' | 'closed' | 'unknown' for GitHub issue `slug#number` (#386)."""
        result = self._shell.run(
            f"gh issue view {number} -R {shlex.quote(slug)} --json state -q .state",
            cwd=Path("."),
        )
        raw = result.stdout.strip().upper()
        if result.returncode == 0 and raw in ("OPEN", "CLOSED"):
            return raw.lower()
        return "unknown"

    def close_issue(self, slug: str, number: int, comment: str) -> None:
        """Close GitHub issue `slug#number` with `comment`. Raises RuntimeError
        on failure (callers treat that as warn-and-continue, #386)."""
        result = self._shell.run(
            f"gh issue close {number} -R {shlex.quote(slug)} --comment {shlex.quote(comment)}",
            cwd=Path("."),
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or f"gh issue close failed for {slug}#{number}"
            )

    def get_pr_body(self, pr_url: str) -> str:
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json body -q .body",
            cwd=Path("."),
        )
        return result.stdout.strip()

    def update_pr_body(self, pr_url: str, body: str) -> None:
        safe_body = shlex.quote(body)
        self._shell.run(
            f"gh pr edit {shlex.quote(pr_url)} --body {safe_body}",
            cwd=Path("."),
        )

    def build_coordination_block(
        self,
        task_slug: str,
        prs: list[dict],
        current_repo: str,
    ) -> str:
        if len(prs) <= 1:
            return ""

        lines = [
            "",
            "---",
            "",
            "## Cross-repo coordination (mothership)",
            "",
            f"This PR is part of a coordinated change: `{task_slug}`",
            "",
            "| # | Repo | PR | Merge order |",
            "|---|------|----|-------------|",
        ]

        for pr in prs:
            members = pr.get("members", [pr["repo"]])
            repo_label = (
                pr["repo"] if len(members) == 1
                else f"{pr['repo']} (+{', '.join(m for m in members if m != pr['repo'])})"
            )
            if current_repo in members:
                order_label = "this PR"
            elif pr["order"] == 1:
                order_label = "merge first"
            else:
                order_label = f"merge #{pr['order']}"
            lines.append(
                f"| {pr['order']} | {repo_label} | {pr['url']} | {order_label} |"
            )

        deps_note = " → ".join(pr["repo"] for pr in prs)
        lines.append("")
        lines.append(f"⚠ Merge in order: {deps_note}")

        return "\n".join(lines)


def _is_embeddable_image(
    evidence, evidence_base_url: str | None, verified_refs=None
) -> bool:
    """True when GitHub's renderer can fetch these bytes AND they are an image.

    Only refs the evidence store produced can resolve under the base URL, so a
    hand-written ref (`docs/shot.png`, from before `capture --evidence`) is
    named rather than embedded as a URL that would 404. An encrypted ref falls
    out here for free: its extension is `.enc`, not an image one — and its bytes
    are ciphertext, so an embed would render broken.

    `verified_refs`, when given, additionally restricts embedding to refs proven
    to exist in the commit the base URL pins. None means "not checked" — the
    pure renderer's default, and the shape callers use to ask the weaker
    question "is this the KIND of thing we would embed?".
    """
    if not evidence_base_url or evidence.kind != "artifact":
        return False
    if not is_stored_ref(evidence.ref):
        return False
    if Path(evidence.ref).suffix.lower() not in IMAGE_EXTS:
        return False
    return verified_refs is None or evidence.ref in verified_refs


def build_acceptance_block(
    spec, evidence_base_url: str | None = None, verified_refs=None
) -> str:
    """Render an 'Acceptance criteria' PR-body section listing each AC as verified
    (with its evidence refs) or unverified. Pure analogue of
    PRManager.build_coordination_block: returns '' when there is nothing to render
    (no criteria), else a leading-separator markdown block ready to append to a
    PR body.

    `evidence_base_url`, when given, is the raw base under which this spec's
    published evidence is fetchable — a commit on the target repo's evidence
    branch; image artifacts are then embedded rather than named. It is None
    whenever the bytes are not fetchable by GitHub (local or encrypted storage, or
    a publication that did not land), in which case the artifact is named — never
    emitted as a broken image.

    `verified_refs` narrows that further to refs proven present in the pinned
    commit, so one unpublished artifact degrades to a name without dragging its
    published siblings down with it. None (the default) embeds every image ref
    the base URL covers.
    """
    acs = getattr(spec, "acceptance_criteria", None) or []
    if not acs:
        return ""
    lines = [
        "",
        "---",
        "",
        "## Acceptance criteria",
        "",
    ]
    for c in acs:
        if not c.evidence:
            lines.append(f"- [ ] `{c.id}` {c.text} — _no evidence_")
            continue
        embeds, named = [], []
        for e in c.evidence:
            embeddable = _is_embeddable_image(e, evidence_base_url, verified_refs)
            (embeds if embeddable else named).append(e)
        head = f"- [x] `{c.id}` {c.text}"
        if named:
            head += " — " + ", ".join(f"{e.kind}:{e.ref}" for e in named)
        lines.append(head)
        # Embedded on its own indented line beneath the criterion, so the
        # checklist stays scannable. The image replaces the ref text: showing
        # both a hash filename and the picture of it is noise.
        for e in embeds:
            lines.append("")
            lines.append(f"  ![{c.id}]({evidence_base_url}/{spec.id}/{e.ref})")
    return "\n".join(lines)


def acceptance_block_for_finish(
    spec, workspace_root, repo_path, shell, config
) -> tuple[str, str | None]:
    """The acceptance block plus an optional operator warning, for the PR about to
    be opened in `repo_path`.

    Split from build_acceptance_block so the pure renderer stays testable without
    a git repo or a config. Embedding requires `published` storage AND the artifact
    actually present in a pushed commit: under `local` the bytes never leave the
    machine, and under `encrypted` they are ciphertext.

    Under `published`, finish OWNS getting them there — `publish_evidence` puts the
    referenced artifacts on `repo_path`'s `mship-evidence` orphan branch, because
    the store is machine-local and nothing else would. Called PER REPO, since each
    PR embeds from its own repo's branch (ac7). It reports back only the refs it
    could prove are in the pinned commit; the rest are named.

    The mode gate is checked FIRST and short-circuits before any shell call:
    `publish_evidence` only answers the git question and would happily publish
    bytes that must never leave the machine if asked, so it must never be asked
    under `local`/`encrypted` (see evidence_url.py). The "has image evidence" scan
    likewise runs first — an operator with no screenshots gets no message and no
    git work.

    `resolve_evidence_mode` can raise `EvidenceModeError` (evidence_storage more
    exposed than spec_storage). By the time `finish` runs, config load has already
    enforced that invariant, so a raise here means the config changed underneath
    mid-session — a real inconsistency, not a normal path. Left to propagate
    uncaught rather than downgraded to a warning: swallowing it would let finish
    continue past a broken config silently, the opposite of surfacing the problem.
    """
    mode = resolve_evidence_mode(config)
    acs = getattr(spec, "acceptance_criteria", None) or []
    # Ordered-unique: one artifact attached to several criteria is one file.
    image_refs = list(dict.fromkeys(
        e.ref
        for c in acs
        for e in (c.evidence or [])
        if _is_embeddable_image(e, "placeholder")
    ))

    base_url, verified, publish_warning = None, None, None
    if mode == "committed" and image_refs:
        from mship.core.evidence_url import publish_evidence

        base_url, verified, publish_warning = publish_evidence(
            workspace_root, repo_path, spec.id, image_refs, shell,
        )

    block = build_acceptance_block(
        spec, evidence_base_url=base_url, verified_refs=verified,
    )

    if not image_refs:
        return block, None
    if mode == "committed":
        return block, publish_warning
    return block, (
        f"Image evidence exists but evidence_storage={mode!r} keeps it off "
        "GitHub in readable form, so it will be named rather than embedded in "
        "the PR body."
    )
