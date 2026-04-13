"""Repo drift detection — data model and audit entry point."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "info"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str


@dataclass(frozen=True)
class RepoAudit:
    name: str
    path: Path
    current_branch: str | None
    issues: tuple[Issue, ...]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


@dataclass(frozen=True)
class AuditReport:
    repos: tuple[RepoAudit, ...]

    @property
    def has_errors(self) -> bool:
        return any(r.has_errors for r in self.repos)

    def to_json(self, workspace: str) -> dict:
        return {
            "workspace": workspace,
            "has_errors": self.has_errors,
            "repos": [
                {
                    "name": r.name,
                    "path": str(r.path),
                    "current_branch": r.current_branch,
                    "issues": [
                        {"code": i.code, "severity": i.severity, "message": i.message}
                        for i in r.issues
                    ],
                }
                for r in self.repos
            ],
        }
