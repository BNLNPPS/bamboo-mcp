"""Guard that the duplicated askpanda_atlas / askpanda_epic modules stay in sync.

Several plugin modules are deliberately duplicated rather than shared, because
plugin packages must stay independently installable and must not import each
other or bamboo core at module scope.  The cost of that decision is drift, and
drift in duplicated code is unusually nasty: the two files look the same, so a
reader assumes the behaviour matches, while the stale copy quietly keeps an old
bug or lacks a new symbol.

This has already happened twice in one session:

- ``_strip_directory_listing`` was added to the ATLAS ``log_analysis_impl``
  after the ePIC copy had been mirrored, so the ePIC excerpt builder kept
  pulling `ls -l` directory listings into the LLM context.
- The chained-traceback fix in ``_traceback_parse.find_traceback_blocks`` landed
  in ATLAS only, so the ePIC copy went on splitting chained exceptions into two
  blocks and reporting the wrong one as the failure.

Neither was caught by the plugins' own test suites, because both suites pass
against their own copy. Only a cross-package comparison catches it, which is
what this module does.

When this test fails, regenerate the ePIC copy rather than hand-editing it::

    python -c "
    import pathlib, sys; sys.path.insert(0, 'tests')
    from plugin_mirror_spec import MIRRORS, render_epic_copy
    for atlas_rel, epic_rel, subs in MIRRORS:
        src = pathlib.Path(atlas_rel).read_text()
        pathlib.Path(epic_rel).write_text(render_epic_copy(src, subs))
    "

If the divergence is intentional, add the difference to the substitution table in
``tests/plugin_mirror_spec.py`` so the intent is recorded as data.
"""
from __future__ import annotations

import ast
import difflib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from plugin_mirror_spec import MIRRORS, render_epic_copy  # noqa: E402

_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("atlas_rel", "epic_rel", "subs"),
    MIRRORS,
    ids=[epic_rel for _atlas_rel, epic_rel, _subs in MIRRORS],
)
def test_epic_copy_matches_atlas_source(
    atlas_rel: str,
    epic_rel: str,
    subs: tuple[tuple[str, str], ...],
) -> None:
    """The ePIC copy must equal the ATLAS source with naming substitutions applied.

    Args:
        atlas_rel: Repo-relative path of the ATLAS source module.
        epic_rel: Repo-relative path of the ePIC copy.
        subs: Ordered substitutions that turn the former into the latter.
    """
    atlas_path = _REPO_ROOT / atlas_rel
    epic_path = _REPO_ROOT / epic_rel

    if not atlas_path.exists() or not epic_path.exists():
        pytest.skip(f"Mirror pair not present in this checkout: {atlas_rel}")

    expected = render_epic_copy(atlas_path.read_text(), subs)
    actual = epic_path.read_text()

    if expected == actual:
        return

    diff = "\n".join(difflib.unified_diff(
        actual.splitlines(),
        expected.splitlines(),
        fromfile=f"{epic_rel} (current)",
        tofile=f"{epic_rel} (expected from {atlas_rel})",
        lineterm="",
    ))
    pytest.fail(
        f"{epic_rel} has drifted from {atlas_rel}. Regenerate it (see this "
        f"module's docstring) or record the intended difference in "
        f"tests/plugin_mirror_spec.py.\n\n{diff}"
    )


@pytest.mark.parametrize(
    ("atlas_rel", "epic_rel", "subs"),
    MIRRORS,
    ids=[epic_rel for _atlas_rel, epic_rel, _subs in MIRRORS],
)
def test_no_atlas_imports_in_epic_copy(
    atlas_rel: str,
    epic_rel: str,
    subs: tuple[tuple[str, str], ...],
) -> None:
    """The ePIC copy must not import from the ATLAS package.

    A leaked ``askpanda_atlas`` import would make askpanda_epic depend on a
    package it is supposed to be installable without, which is the whole reason
    these files are duplicated instead of shared.

    Checked via ``ast`` rather than by searching the text, because the module
    docstrings legitimately mention ``askpanda_atlas`` in prose — for instance
    to point at ``pilot_source_analysis_impl``, which genuinely lives only in
    the ATLAS package. A substring search flags those and trains the reader to
    ignore the test.

    Args:
        atlas_rel: Repo-relative path of the ATLAS source module.
        epic_rel: Repo-relative path of the ePIC copy.
        subs: Unused; present so the parametrisation matches the other tests.
    """
    epic_path = _REPO_ROOT / epic_rel
    if not epic_path.exists():
        pytest.skip(f"Mirror target not present in this checkout: {epic_rel}")

    tree = ast.parse(epic_path.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("askpanda_atlas"):
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("askpanda_atlas"):
                    offenders.append(f"line {node.lineno}: import {alias.name}")

    assert not offenders, (
        f"{epic_rel} imports from askpanda_atlas, which breaks independent "
        f"installability of askpanda_epic:\n" + "\n".join(offenders)
    )


def test_mirror_substitutions_are_all_used() -> None:
    """Every substitution must actually match, so the table cannot rot silently.

    A substitution whose search string no longer appears in the ATLAS source is
    dead weight that hides the fact the real difference is unrecorded.
    """
    unused: list[str] = []
    for atlas_rel, _epic_rel, subs in MIRRORS:
        atlas_path = _REPO_ROOT / atlas_rel
        if not atlas_path.exists():
            continue
        source = atlas_path.read_text()
        for find, _replace in subs:
            if find not in source:
                unused.append(f"{atlas_rel}: {find!r}")
    assert not unused, (
        "Substitutions in tests/plugin_mirror_spec.py no longer match the ATLAS "
        "source and should be removed or corrected:\n" + "\n".join(unused)
    )
