from __future__ import annotations

from dataclasses import dataclass, field


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
