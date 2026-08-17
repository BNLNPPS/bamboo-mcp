"""Canonical specification of the askpanda_atlas -> askpanda_epic file mirrors.

Some plugin modules are deliberately duplicated between ``askpanda_atlas`` and
``askpanda_epic`` rather than shared, because plugin packages must stay
independently installable and must not import each other or bamboo core at
module scope.  The duplicates differ only in experiment naming.

Duplication invites drift: a change applied to the ATLAS copy and forgotten in
the ePIC copy leaves two files that look the same and behave differently.  That
has already happened once — ``_strip_directory_listing`` was added to the ATLAS
``log_analysis_impl`` after the ePIC copy had been mirrored, and the divergence
surfaced only as a downstream type-check error against a stale symbol.

This module holds the substitutions as data so that:

- the mirror can be regenerated mechanically, and
- ``tests/test_plugin_mirror_parity.py`` can assert the copies are still in
  sync, turning silent drift into a test failure.

To add a mirrored file, add an entry to :data:`MIRRORS`.
"""
from __future__ import annotations

# Substitutions applied to the ATLAS source to produce the ePIC copy, in order.
# Longest/most specific first: plain "askpanda_atlas" -> "askpanda_epic" must run
# last so it cannot clobber a more specific phrase containing that token.
_LOG_ANALYSIS_SUBS: tuple[tuple[str, str], ...] = (
    ('"""ATLAS PanDA job log analysis tool', '"""ePIC PanDA job log analysis tool'),
    (
        "Fetches job metadata and pilot log from BigPanDA, extracts a relevant",
        "Fetches job metadata and pilot log from the PanDA monitor, extracts a relevant",
    ),
    (
        '"pilot log and error metadata from BigPanDA, extracts the "',
        '"pilot log and error metadata from the PanDA monitor, extracts the "',
    ),
    (
        'f"Job {job_id} was not found in BigPanDA."',
        'f"Job {job_id} was not found in the PanDA monitor."',
    ),
    (
        '"Failed to fetch job metadata from BigPanDA"',
        '"Failed to fetch job metadata from the PanDA monitor"',
    ),
    (
        "base_url: BigPanDA base URL (from environment or default).",
        "base_url: PanDA monitor base URL (from environment or default).",
    ),
    ("base_url: BigPanDA base URL.", "base_url: PanDA monitor base URL."),
    (
        '"""Fetch job metadata JSON from BigPanDA, using the in-process TTL cache.',
        '"""Fetch job metadata JSON from the PanDA monitor, using the in-process TTL cache.',
    ),
    (
        "BigPanDA's filebrowser JSON listing uses the following structure::",
        "The PanDA monitor's filebrowser JSON listing uses the following structure::",
    ),
    (
        "Fetches job metadata and pilot/payload logs directly from BigPanDA,",
        "Fetches job metadata and pilot/payload logs directly from the PanDA monitor,",
    ),
    (
        '[f"- [BigPanDA Monitor]({monitor_url})"]',
        '[f"- [PanDA Monitor]({monitor_url})"]',
    ),
    (
        '"tags": ["atlas", "panda", "bigpanda", "job", "log", "failure", "diagnosis"],',
        '"tags": ["epic", "eic", "panda", "job", "log", "failure", "diagnosis"],',
    ),
    (
        "# Some BigPanDA versions wrap the list under a",
        "# Some PanDA monitor versions wrap the list under a",
    ),
    (
        "The ``job`` dict from the BigPanDA metadata response.",
        "The ``job`` dict from the PanDA monitor metadata response.",
    ),
    ("askpanda_atlas", "askpanda_epic"),
)

_TRACEBACK_PARSE_SUBS: tuple[tuple[str, str], ...] = (
    ("traceback parsing — ATLAS plugin copy", "traceback parsing — ePIC plugin copy"),
    (
        ":mod:`askpanda_atlas.log_analysis_impl` to locate",
        ":mod:`askpanda_epic.log_analysis_impl` to locate",
    ),
    (
        "This file is intentionally duplicated in ``askpanda_epic`` (kept byte-identical",
        "This file is intentionally duplicated in ``askpanda_atlas`` (kept byte-identical",
    ),
    (
        "be mirrored in ``askpanda_epic/_traceback_parse.py``.",
        "be mirrored in ``askpanda_atlas/_traceback_parse.py``.",
    ),
)

#: Mirrored files as (atlas_relative_path, epic_relative_path, substitutions).
#: Paths are relative to the repository root.
MIRRORS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "packages/askpanda_atlas/askpanda_atlas/log_analysis_impl.py",
        "packages/askpanda_epic/askpanda_epic/log_analysis_impl.py",
        _LOG_ANALYSIS_SUBS,
    ),
    (
        "packages/askpanda_atlas/askpanda_atlas/_traceback_parse.py",
        "packages/askpanda_epic/askpanda_epic/_traceback_parse.py",
        _TRACEBACK_PARSE_SUBS,
    ),
)


def render_epic_copy(atlas_source: str, subs: tuple[tuple[str, str], ...]) -> str:
    """Apply mirror substitutions to ATLAS source to produce the ePIC copy.

    Args:
        atlas_source: Full text of the ATLAS module.
        subs: Ordered ``(find, replace)`` pairs for this mirror.

    Returns:
        The expected text of the corresponding ePIC module.
    """
    result = atlas_source
    for find, replace in subs:
        result = result.replace(find, replace)
    return result


__all__ = ["MIRRORS", "render_epic_copy"]
