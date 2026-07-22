from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Enroll ids are secrets.token_hex(16) (hex); [0-9a-z] also admits the
# lowercase-alpha stand-ins used in tests while still rejecting every
# path-traversal metacharacter (., /, \) — this is the traversal guard, not an
# id-format contract.
_ID_RE = re.compile(r"\A[0-9a-z]{1,64}\Z")

# owner/repo: exactly one slash, non-empty halves, no whitespace. This is what
# GitHubAppProvider later splits on `owner, name = r.split("/", 1)`, so validating
# it here (at grant/issue time) turns a malformed `--repos api` into a clear up-front
# error instead of a later IndexError / 500 at mint time.
_REPO_RE = re.compile(r"\A[^/\s]+/[^/\s]+\Z")


class RepoSpecError(ValueError):
    """A `--repos` value is empty, malformed, or spans multiple owners."""


def parse_repos(csv: str) -> tuple[str, ...]:
    """Parse a comma-separated `owner/repo,owner/repo` list into a validated tuple.

    Raises RepoSpecError when the list is empty, any entry is not `owner/repo`, or
    the entries span more than one owner (mship mints one installation token per
    single account — matching `mship serve`'s single-owner /gh-token contract)."""
    repos = tuple(r.strip() for r in csv.split(",") if r.strip())
    if not repos:
        raise RepoSpecError("must list at least one owner/repo")
    bad = [r for r in repos if not _REPO_RE.match(r)]
    if bad:
        raise RepoSpecError(f"malformed repo(s), expected owner/repo: {bad}")
    owners = {r.split("/", 1)[0] for r in repos}
    if len(owners) > 1:
        raise RepoSpecError(f"repos span multiple owners {sorted(owners)}; "
                            "a run is single-account")
    return repos


@dataclass(frozen=True)
class Scope:
    """A repo set, optionally narrowed to one push branch.

    Used twice: as an enrollment's CEILING (push_branch=None — which repos the
    enrollment may ever touch) and as a PER-RUN scope (repos ⊆ ceiling +
    push_branch = the run's branch). `repos` are full `owner/repo` names.
    """
    repos: tuple[str, ...] = field(default_factory=tuple)
    push_branch: str | None = None

    def covers(self, other: "Scope") -> bool:
        """True when `other`'s repos are a subset of this scope's repos.
        Branch is not part of the ceiling check (the ceiling has no branch)."""
        return set(other.repos) <= set(self.repos)


@dataclass(frozen=True)
class Grant:
    """A typed authorization: one provider + its scope. v1: provider='github-app'."""
    provider: str
    scope: Scope


class GrantStore:
    """Filesystem-backed typed grants, one file per enrollment: grants/<id>.json.
    Atomic writes (tmp + replace), like RequestStore."""

    def __init__(self, base_dir):
        self._dir = Path(base_dir) / "grants"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, enrollment_id: str) -> Path:
        if not _ID_RE.match(enrollment_id):
            raise ValueError(f"invalid enrollment id {enrollment_id!r}")
        return self._dir / f"{enrollment_id}.json"

    def _write_atomic(self, path: Path, rec: dict) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(path)

    def set_grant(self, enrollment_id: str, grant: Grant) -> None:
        """Set/replace the grant for `grant.provider` on this enrollment."""
        path = self._path(enrollment_id)
        grants = {g.provider: g for g in self.get_grants(enrollment_id)}
        grants[grant.provider] = grant
        self._write_atomic(
            path,
            {
                "enrollment_id": enrollment_id,
                "grants": [
                    {
                        "provider": g.provider,
                        "scope": {"repos": list(g.scope.repos),
                                  "push_branch": g.scope.push_branch},
                    }
                    for g in grants.values()
                ],
            },
        )

    def get_grants(self, enrollment_id: str) -> list[Grant]:
        path = self._path(enrollment_id)
        if not path.exists():
            return []
        try:
            rec = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        out: list[Grant] = []
        for g in rec.get("grants", []):
            sc = g.get("scope", {})
            out.append(
                Grant(
                    provider=g["provider"],
                    scope=Scope(repos=tuple(sc.get("repos", [])),
                                push_branch=sc.get("push_branch")),
                )
            )
        return out
