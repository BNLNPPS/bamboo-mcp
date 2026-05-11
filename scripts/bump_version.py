"""Bump the Bamboo version string across all relevant files.

Usage::

    python scripts/bump_version.py <old_version> <new_version>

Example::

    python scripts/bump_version.py 1.0.1 1.0.2

The script performs exact string replacement of the version literal in each
target file and reports each change.  It exits non-zero if any replacement
fails, so it is safe to run in CI or a pre-release check.

Files updated
-------------
Active (always updated):

- ``__init__.py``                              — ``__version__ = "X.Y.Z"``
- ``pyproject.toml``                           — root package
- ``core/pyproject.toml``                      — bamboo-core package
- ``packages/askpanda_atlas/pyproject.toml``   — ATLAS plugin

Inactive (commented out — update manually when those packages are released):

- ``packages/askpanda_epic/pyproject.toml``
- ``packages/askpanda_verarubin/pyproject.toml``
- ``packages/cgsim/pyproject.toml``
"""
from __future__ import annotations

import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# File registry
# ---------------------------------------------------------------------------

# Each entry is (relative_path, search_template, replace_template).
# {old} and {new} are substituted with the CLI arguments.
_ACTIVE_FILES: list[tuple[str, str, str]] = [
    (
        "__init__.py",
        '__version__ = "{old}"',
        '__version__ = "{new}"',
    ),
    (
        "pyproject.toml",
        'version = "{old}"',
        'version = "{new}"',
    ),
    (
        "core/pyproject.toml",
        'version = "{old}"',
        'version = "{new}"',
    ),
    (
        "packages/askpanda_atlas/pyproject.toml",
        'version = "{old}"',
        'version = "{new}"',
    ),
    (
        "packages/askpanda_epic/pyproject.toml",
        'version = "{old}"',
        'version = "{new}"',
    ),
    # Inactive — uncomment when these packages are versioned alongside atlas:
    # (
    #     "packages/askpanda_epic/pyproject.toml",
    #     'version = "{old}"',
    #     'version = "{new}"',
    # ),
    # (
    #     "packages/askpanda_verarubin/pyproject.toml",
    #     'version = "{old}"',
    #     'version = "{new}"',
    # ),
    # (
    #     "packages/cgsim/pyproject.toml",
    #     'version = "{old}"',
    #     'version = "{new}"',
    # ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the repository root (the directory containing this script's parent).

    Assumes the script lives at ``<repo>/scripts/bump_version.py``.

    Returns:
        Absolute Path to the repository root.
    """
    return Path(__file__).resolve().parent.parent


def _bump_file(path: Path, search: str, replace: str) -> bool:
    """Replace the first occurrence of *search* with *replace* in *path*.

    Args:
        path: Absolute path to the file to update.
        search: Exact string to search for.
        replace: String to substitute in place of *search*.

    Returns:
        True if the replacement was made, False if *search* was not found.

    Raises:
        OSError: If the file cannot be read or written.
    """
    original = path.read_text(encoding="utf-8")
    if search not in original:
        return False
    updated = original.replace(search, replace, 1)
    path.write_text(updated, encoding="utf-8")
    return True


def _validate_version(version: str, label: str) -> None:
    """Raise SystemExit if *version* does not look like a PEP 440 version.

    Accepts simple forms: ``MAJOR.MINOR.PATCH`` and ``MAJOR.MINOR.PATCH.devN``
    / ``...aN`` / ``...bN`` / ``...rcN``.  Rejects obviously malformed input
    so the script fails fast rather than writing garbage into version files.

    Args:
        version: Version string to validate.
        label: Human-readable label used in the error message (e.g. ``"old"``).

    Raises:
        SystemExit: If *version* is invalid.
    """
    import re  # noqa: PLC0415
    pattern = r"^\d+\.\d+(\.\d+)?(\.?(a|b|rc|dev)\d+)?$"
    if not re.match(pattern, version):
        print(f"error: {label} version {version!r} does not look like a valid version.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments, validate, and apply version bump to all active files.

    Raises:
        SystemExit: On argument errors or if any file replacement fails.
    """
    if len(sys.argv) != 3:
        print("usage: python scripts/bump_version.py <old_version> <new_version>", file=sys.stderr)
        print("example: python scripts/bump_version.py 1.0.1 1.0.2", file=sys.stderr)
        sys.exit(1)

    old_version, new_version = sys.argv[1], sys.argv[2]

    _validate_version(old_version, "old")
    _validate_version(new_version, "new")

    if old_version == new_version:
        print(f"error: old and new versions are identical ({old_version}).", file=sys.stderr)
        sys.exit(1)

    root = _repo_root()
    errors: list[str] = []

    print(f"Bumping version: {old_version} → {new_version}\n")

    for rel_path, search_tpl, replace_tpl in _ACTIVE_FILES:
        search = search_tpl.format(old=old_version, new=new_version)
        replace = replace_tpl.format(old=old_version, new=new_version)
        path = root / rel_path

        if not path.exists():
            errors.append(f"  MISSING  {rel_path}")
            continue

        try:
            found = _bump_file(path, search, replace)
        except OSError as exc:
            errors.append(f"  ERROR    {rel_path}: {exc}")
            continue

        if found:
            print(f"  updated  {rel_path}")
        else:
            errors.append(f"  NOT FOUND  {rel_path}  (searched for: {search!r})")

    if errors:
        print("\nFailed:")
        for msg in errors:
            print(msg)
        sys.exit(1)

    print(f"\nDone. All files updated to {new_version}.")


if __name__ == "__main__":
    main()
