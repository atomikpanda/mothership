from pathlib import Path


class SpecNotFoundError(Exception):
    pass


SPEC_SUBDIR = Path("docs") / "superpowers" / "specs"


def find_spec(workspace_root: Path, name_or_path: str | None) -> Path:
    """Resolve a spec file. None = newest by mtime in default spec dir."""
    if name_or_path is None:
        return _newest(workspace_root / SPEC_SUBDIR)

    candidate = Path(name_or_path)
    if candidate.is_absolute() and candidate.is_file():
        return candidate

    specs_dir = workspace_root / SPEC_SUBDIR
    for name in (name_or_path, f"{name_or_path}.md"):
        p = specs_dir / name
        if p.is_file():
            return p

    raise SpecNotFoundError(
        f"Spec not found: {name_or_path!r} (checked {specs_dir})"
    )


def _newest(specs_dir: Path) -> Path:
    if not specs_dir.is_dir():
        raise SpecNotFoundError(f"Spec directory does not exist: {specs_dir}")
    candidates = [p for p in specs_dir.iterdir() if p.is_file() and p.suffix == ".md"]
    if not candidates:
        raise SpecNotFoundError(f"No specs found in {specs_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)
