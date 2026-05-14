"""Tests for the in-process TTL cache (askpanda_atlas._cache).

All tests are synchronous and require no network access.  The cache is
cleared before each test via the ``autouse`` fixture so tests are isolated.
"""
from __future__ import annotations

import math
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from askpanda_atlas._cache import (  # type: ignore[import]
    METADATA_TTL,
    LOG_TTL,
    _MISS,
    _get,
    _set,
    cached_fetch_jsonish,
    cached_fetch_log,
    clear,
    invalidate,
    stats,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_cache() -> None:
    """Clear the cache before every test to ensure isolation."""
    clear()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_metadata_ttl_is_sixty_seconds() -> None:
    """METADATA_TTL is 60 seconds."""
    assert METADATA_TTL == 60.0


def test_log_ttl_is_infinite() -> None:
    """LOG_TTL is math.inf — logs never expire."""
    assert LOG_TTL == math.inf


# ---------------------------------------------------------------------------
# Internal primitives: _get / _set
# ---------------------------------------------------------------------------


def test_get_returns_miss_for_unknown_key() -> None:
    """_get returns the _MISS sentinel for a key that has never been set."""
    assert _get("https://example.com/never-set") is _MISS


def test_set_and_get_roundtrip() -> None:
    """A value stored with _set is retrievable via _get before expiry."""
    _set("key1", {"data": 42}, ttl=60.0)
    result = _get("key1")
    assert result == {"data": 42}


def test_expired_entry_returns_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry whose TTL has elapsed is evicted and returns _MISS."""
    _set("key2", "value", ttl=1.0)
    # Advance monotonic time past the TTL.
    original = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original() + 2.0)
    assert _get("key2") is _MISS


def test_infinite_ttl_never_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry stored with ttl=math.inf is still returned far in the future."""
    _set("log_key", "log text", ttl=math.inf)
    original = time.monotonic
    # 10 years in the future.
    monkeypatch.setattr(time, "monotonic", lambda: original() + 3.15e8)
    assert _get("log_key") == "log text"


def test_none_value_is_cached() -> None:
    """Storing None is valid — distinguishable from _MISS."""
    _set("key_none", None, ttl=60.0)
    result = _get("key_none")
    assert result is None
    assert result is not _MISS


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


def test_invalidate_removes_entry() -> None:
    """invalidate() evicts a specific key."""
    _set("to_remove", "data", ttl=60.0)
    invalidate("to_remove")
    assert _get("to_remove") is _MISS


def test_invalidate_nonexistent_key_is_safe() -> None:
    """invalidate() on a key that doesn't exist raises no error."""
    invalidate("https://example.com/ghost")  # must not raise


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_removes_all_entries() -> None:
    """clear() evicts every cached entry."""
    _set("a", 1, ttl=60.0)
    _set("b", 2, ttl=60.0)
    _set("c", 3, ttl=math.inf)
    clear()
    assert _get("a") is _MISS
    assert _get("b") is _MISS
    assert _get("c") is _MISS


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_reflects_current_entries() -> None:
    """stats() returns the correct entry count and URL list."""
    _set("https://bigpanda.cern.ch/jobs/?jeditaskid=1&json", "x", ttl=60.0)
    _set("https://bigpanda.cern.ch/job?pandaid=2&json", "y", ttl=60.0)
    s = stats()
    assert s["entries"] == 2
    assert "https://bigpanda.cern.ch/jobs/?jeditaskid=1&json" in s["urls"]


def test_stats_counts_expired_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """stats() correctly counts entries that have exceeded their TTL."""
    _set("exp1", "v", ttl=1.0)
    _set("exp2", "v", ttl=1.0)
    _set("live", "v", ttl=math.inf)
    original = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original() + 2.0)
    s = stats()
    assert s["expired"] == 2
    assert s["entries"] == 3  # not yet evicted, just counted as expired


def test_stats_empty_cache() -> None:
    """stats() on an empty cache returns zeros."""
    s = stats()
    assert s["entries"] == 0
    assert s["expired"] == 0
    assert s["urls"] == []


# ---------------------------------------------------------------------------
# cached_fetch_jsonish
# ---------------------------------------------------------------------------

_SAMPLE_RESPONSE: tuple[int, str, str, dict[str, Any]] = (
    200, "application/json", '{"jobs": []}', {"jobs": []}
)


def test_cached_fetch_jsonish_calls_real_on_miss() -> None:
    """On a cache miss, fetch_jsonish is called once and the result is stored."""
    url = "https://bigpanda.cern.ch/jobs/?jeditaskid=99&json"
    mock_fetch = MagicMock(return_value=_SAMPLE_RESPONSE)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        result = cached_fetch_jsonish(url)

    assert result == _SAMPLE_RESPONSE
    mock_fetch.assert_called_once_with(url, 30)


def test_cached_fetch_jsonish_returns_cached_on_hit() -> None:
    """On a cache hit, fetch_jsonish is NOT called again."""
    url = "https://bigpanda.cern.ch/jobs/?jeditaskid=100&json"
    mock_fetch = MagicMock(return_value=_SAMPLE_RESPONSE)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        first = cached_fetch_jsonish(url)
        second = cached_fetch_jsonish(url)

    assert first == second == _SAMPLE_RESPONSE
    mock_fetch.assert_called_once()   # only one real HTTP call


def test_cached_fetch_jsonish_separate_urls_are_independent() -> None:
    """Different URLs are cached independently."""
    url_a = "https://bigpanda.cern.ch/jobs/?jeditaskid=1&json"
    url_b = "https://bigpanda.cern.ch/jobs/?jeditaskid=2&json"
    resp_a = (200, "application/json", '{"jobs": [{"pandaid": 1}]}', {"jobs": [{"pandaid": 1}]})
    resp_b = (200, "application/json", '{"jobs": [{"pandaid": 2}]}', {"jobs": [{"pandaid": 2}]})

    def _side_effect(url: str, timeout: int) -> Any:
        return resp_a if "jeditaskid=1" in url else resp_b

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", side_effect=_side_effect):
        a = cached_fetch_jsonish(url_a)
        b = cached_fetch_jsonish(url_b)

    assert a[3] == {"jobs": [{"pandaid": 1}]}
    assert b[3] == {"jobs": [{"pandaid": 2}]}


def test_cached_fetch_jsonish_uses_metadata_ttl_by_default() -> None:
    """Default TTL is METADATA_TTL (60 s); the entry expires accordingly."""
    url = "https://bigpanda.cern.ch/jobs/?jeditaskid=77&json"
    mock_fetch = MagicMock(return_value=_SAMPLE_RESPONSE)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        cached_fetch_jsonish(url)   # populates cache

    # The entry should be present.
    assert _get(url) is not _MISS


def test_cached_fetch_jsonish_expired_entry_refetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired metadata entry triggers a fresh HTTP request."""
    url = "https://bigpanda.cern.ch/jobs/?jeditaskid=55&json"
    mock_fetch = MagicMock(return_value=_SAMPLE_RESPONSE)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        cached_fetch_jsonish(url)  # first call — populates cache

    # Advance time past METADATA_TTL.
    original = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original() + METADATA_TTL + 1)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        cached_fetch_jsonish(url)  # second call — cache expired → re-fetch

    assert mock_fetch.call_count == 2


def test_cached_fetch_jsonish_infinite_ttl_never_refetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry stored with ttl=math.inf is never re-fetched."""
    url = "https://bigpanda.cern.ch/filebrowser/?pandaid=1&json&filename=pilotlog.txt"
    mock_fetch = MagicMock(return_value=_SAMPLE_RESPONSE)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        cached_fetch_jsonish(url, ttl=math.inf)

    # Advance time by 10 years.
    original = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original() + 3.15e8)

    with patch("askpanda_atlas._fallback_http.fetch_jsonish", mock_fetch):
        cached_fetch_jsonish(url, ttl=math.inf)

    mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# cached_fetch_log
# ---------------------------------------------------------------------------


def test_cached_fetch_log_calls_requests_on_miss() -> None:
    """On a cache miss, requests.get is called and the text is stored."""
    url = "https://bigpanda.cern.ch/filebrowser/?pandaid=42&json&filename=pilotlog.txt"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "=== PILOT LOG ==="
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp) as mock_get:
        result = cached_fetch_log(url)

    assert result == "=== PILOT LOG ==="
    mock_get.assert_called_once()


def test_cached_fetch_log_returns_cached_on_hit() -> None:
    """On a cache hit, requests.get is NOT called again for a log."""
    url = "https://bigpanda.cern.ch/filebrowser/?pandaid=43&json&filename=pilotlog.txt"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "log line 1\nlog line 2"
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp) as mock_get:
        first = cached_fetch_log(url)
        second = cached_fetch_log(url)

    assert first == second == "log line 1\nlog line 2"
    mock_get.assert_called_once()   # only one real HTTP download


def test_cached_fetch_log_returns_none_for_404() -> None:
    """A 404 response yields None, which is itself cached to avoid re-requests."""
    url = "https://bigpanda.cern.ch/filebrowser/?pandaid=44&json&filename=missing.txt"
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("requests.get", return_value=mock_resp) as mock_get:
        first = cached_fetch_log(url)
        second = cached_fetch_log(url)

    assert first is None
    assert second is None
    mock_get.assert_called_once()  # 404 result is cached — no second request


def test_cached_fetch_log_none_persists_across_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached None (404) for a log is never re-fetched, even after a long time."""
    url = "https://bigpanda.cern.ch/filebrowser/?pandaid=45&json&filename=gone.txt"
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("requests.get", return_value=mock_resp):
        cached_fetch_log(url)

    original = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original() + 3.15e8)

    with patch("requests.get", return_value=mock_resp) as mock_get2:
        result = cached_fetch_log(url)

    assert result is None
    mock_get2.assert_not_called()   # infinite TTL — never re-fetched


def test_cached_fetch_log_after_clear_refetches() -> None:
    """After clear(), a log is re-downloaded on the next request."""
    url = "https://bigpanda.cern.ch/filebrowser/?pandaid=46&json&filename=pilotlog.txt"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "log content"
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp) as mock_get:
        cached_fetch_log(url)   # populate
        clear()                  # flush everything
        cached_fetch_log(url)   # must re-fetch

    assert mock_get.call_count == 2
