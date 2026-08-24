"""Tests for bamboo.tools.code_query.

Covers:
- _github_raw_url / _github_browse_url helpers
- _extract_function AST extraction
- fetch_source_file with mocked HTTP (success, HTTP error, connection error,
  function extraction, truncation)
- CodeQueryTool.call() argument validation and async dispatch
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from bamboo.tools.code_query import (
    CodeQueryTool,
    _extract_function,
    _github_browse_url,
    _github_raw_url,
    fetch_source_file,
    code_query_tool,
)

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def test_github_raw_url_default() -> None:
    """_github_raw_url produces correct raw.githubusercontent.com URL."""
    url = _github_raw_url("PanDAWMS/pilot3", "master", "pilot/util/processes.py")
    assert url == (
        "https://raw.githubusercontent.com/PanDAWMS/pilot3/master/pilot/util/processes.py"
    )


def test_github_browse_url() -> None:
    """_github_browse_url produces correct github.com/blob URL."""
    url = _github_browse_url("PanDAWMS/pilot3", "master", "pilot/util/processes.py")
    assert url == (
        "https://github.com/PanDAWMS/pilot3/blob/master/pilot/util/processes.py"
    )


def test_github_raw_url_custom_repo_branch() -> None:
    """Custom repo and branch are reflected in the URL."""
    url = _github_raw_url("myorg/mypilot", "dev", "pilot/control/job.py")
    assert "myorg/mypilot" in url
    assert "/dev/" in url


# ---------------------------------------------------------------------------
# _extract_function
# ---------------------------------------------------------------------------

_SAMPLE_SOURCE: str = """\
def helper(x):
    return x + 1


class MyClass:
    def method(self, y):
        return y * 2


async def async_fn(z):
    return z


def outer():
    def inner():
        pass
    return inner
"""


def test_extract_top_level_function() -> None:
    """Extracts a top-level function by name."""
    result = _extract_function(_SAMPLE_SOURCE, "helper")
    assert result is not None
    assert "def helper" in result
    assert "return x + 1" in result


def test_extract_class_method() -> None:
    """Extracts a class method by name."""
    result = _extract_function(_SAMPLE_SOURCE, "method")
    assert result is not None
    assert "def method" in result


def test_extract_async_function() -> None:
    """Extracts an async function correctly."""
    result = _extract_function(_SAMPLE_SOURCE, "async_fn")
    assert result is not None
    assert "async def async_fn" in result


def test_extract_missing_function_returns_none() -> None:
    """Returns None when the function does not exist."""
    result = _extract_function(_SAMPLE_SOURCE, "nonexistent")
    assert result is None


def test_extract_on_syntax_error_returns_none() -> None:
    """Returns None gracefully when source has a syntax error."""
    result = _extract_function("def broken(\n", "broken")
    assert result is None


def test_extract_with_decorator() -> None:
    """Decorator lines are included in the extraction."""
    source = "@staticmethod\ndef decorated():\n    pass\n"
    result = _extract_function(source, "decorated")
    assert result is not None
    assert "@staticmethod" in result


# ---------------------------------------------------------------------------
# fetch_source_file
# ---------------------------------------------------------------------------


def _make_urlopen(status: int, text: str | None) -> Any:
    """Build a mock for urllib.request.urlopen returning the given status/text."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = status
    if text is not None:
        mock_resp.read.return_value = text.encode("utf-8")
    return MagicMock(return_value=mock_resp)


def test_fetch_source_file_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_source_file returns source text on HTTP 200."""
    source_text = "def foo():\n    return 42\n"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(200, source_text))

    result = fetch_source_file("pilot/util/foo.py")

    assert result["fetch_error"] == ""
    assert result["source"] == source_text
    assert result["truncated"] is False
    assert "pilot/util/foo.py" in result["github_url"]


def test_fetch_source_file_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_source_file records fetch_error on HTTP 404."""
    import urllib.error

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError(  # type: ignore[arg-type]
            url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    result = fetch_source_file("pilot/util/missing.py")

    assert "404" in result["fetch_error"]
    assert result["source"] is None


def test_fetch_source_file_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_source_file records fetch_error (status 0) on connection failure."""
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    result = fetch_source_file("pilot/util/foo.py")

    assert "0" in result["fetch_error"]
    assert result["source"] is None


def test_fetch_source_file_function_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_source_file extracts the named function when function_name is given."""
    source_text = "def bar():\n    return 99\n\ndef baz():\n    pass\n"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(200, source_text))

    result = fetch_source_file("pilot/util/foo.py", function_name="bar")

    assert result["fetch_error"] == ""
    assert "def bar" in (result["source"] or "")
    assert "def baz" not in (result["source"] or "")
    assert result["function_name"] == "bar"


def test_fetch_source_file_missing_function_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """When function_name is not found, returns full source with a warning in fetch_error."""
    source_text = "def something():\n    pass\n"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(200, source_text))

    result = fetch_source_file("pilot/util/foo.py", function_name="nonexistent")

    # Source falls back to full module text
    assert result["source"] == source_text
    assert "not found" in result["fetch_error"]


def test_fetch_source_file_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_source_file sets truncated=True when source exceeds _MAX_SOURCE_CHARS."""
    from bamboo.tools.code_query import _MAX_SOURCE_CHARS

    long_source = "x = 1\n" * (_MAX_SOURCE_CHARS // 6 + 100)
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(200, long_source))

    result = fetch_source_file("pilot/util/big.py")

    assert result["truncated"] is True
    # Source should be shorter than the raw limit (cut at line boundary) and
    # contain the truncation note appended by _truncate_to_line_boundary.
    assert len(result["source"] or "") <= _MAX_SOURCE_CHARS + 200  # note overhead
    assert "TRUNCATED" in (result["source"] or "")


# ---------------------------------------------------------------------------
# _truncate_to_line_boundary
# ---------------------------------------------------------------------------


def test_truncate_no_op_when_within_limit() -> None:
    """Short text is returned unchanged with truncated=False."""
    from bamboo.tools.code_query import _truncate_to_line_boundary

    text = "line1\nline2\nline3\n"
    result, was_truncated = _truncate_to_line_boundary(text, 10_000)
    assert result == text
    assert was_truncated is False


def test_truncate_cuts_at_line_boundary() -> None:
    """Truncation always lands on a complete line, not mid-statement."""
    from bamboo.tools.code_query import _truncate_to_line_boundary

    text = "line1\n" + "x" * 200 + "\nline3\n"
    result, was_truncated = _truncate_to_line_boundary(text, 20)
    assert was_truncated is True
    # Result must not contain a partial "xxx..." fragment without a preceding newline
    # i.e. the cut text before the note ends at a newline.
    before_note = result.split("# --- TRUNCATED")[0]
    assert before_note.endswith("\n")


def test_truncate_appends_line_count_note() -> None:
    """The truncation note reports line counts."""
    from bamboo.tools.code_query import _truncate_to_line_boundary

    text = "\n".join(f"line{i}" for i in range(200)) + "\n"
    result, _ = _truncate_to_line_boundary(text, 100)
    assert "TRUNCATED" in result
    assert "201" in result  # total line count (200 data lines + trailing newline = 201)


def test_truncate_exact_fit_no_truncation() -> None:
    """Text exactly at the limit is not truncated."""
    from bamboo.tools.code_query import _truncate_to_line_boundary

    text = "a" * 100
    result, was_truncated = _truncate_to_line_boundary(text, 100)
    assert not was_truncated
    assert result == text


def test_fetch_source_file_custom_repo_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BAMBOO_CODE_QUERY_REPO env var overrides the default repository."""
    source_text = "pass\n"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(200, source_text))
    monkeypatch.setenv("BAMBOO_CODE_QUERY_REPO", "myorg/mypilot")
    monkeypatch.setenv("BAMBOO_CODE_QUERY_BRANCH", "develop")

    result = fetch_source_file("pilot/util/foo.py")

    assert result["repo"] == "myorg/mypilot"
    assert result["branch"] == "develop"
    assert "myorg/mypilot" in result["github_url"]


# ---------------------------------------------------------------------------
# CodeQueryTool.call()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_missing_pilot_path() -> None:
    """call() returns an error when file_path is absent."""
    tool = CodeQueryTool()
    result = await tool.call({"question": "explain"})
    text = result[0].text if hasattr(result[0], "text") else result[0]["text"]
    parsed = json.loads(text)
    assert "error" in parsed.get("evidence", {})


@pytest.mark.asyncio
async def test_tool_call_non_dict_arguments() -> None:
    """call() handles non-dict arguments gracefully."""
    tool = CodeQueryTool()
    result = await tool.call("not a dict")  # type: ignore[arg-type]
    text = result[0].text if hasattr(result[0], "text") else result[0]["text"]
    parsed = json.loads(text)
    assert "error" in parsed.get("evidence", {})


@pytest.mark.asyncio
async def test_tool_call_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """call() returns structured evidence on a successful fetch."""
    source_text = "def foo():\n    return 1\n"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(200, source_text))

    tool = CodeQueryTool()
    result = await tool.call({
        "file_path": "pilot/util/foo.py",
        "question": "What does foo do?",
    })

    text = result[0].text if hasattr(result[0], "text") else result[0]["text"]
    parsed = json.loads(text)
    evidence = parsed.get("evidence", {})
    assert evidence.get("file_path") == "pilot/util/foo.py"
    assert evidence.get("source") == source_text
    assert evidence.get("fetch_error") == ""


@pytest.mark.asyncio
async def test_tool_call_fetch_error_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """call() surfaces fetch_error in evidence when the file is unavailable."""
    import urllib.error

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError(  # type: ignore[arg-type]
            url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    tool = CodeQueryTool()
    result = await tool.call({
        "file_path": "pilot/util/missing.py",
        "question": "What does this do?",
    })

    text = result[0].text if hasattr(result[0], "text") else result[0]["text"]
    parsed = json.loads(text)
    evidence = parsed.get("evidence", {})
    assert "404" in evidence.get("fetch_error", "")
    assert evidence.get("source") is None


def test_get_definition_shape() -> None:
    """get_definition() returns a dict with required MCP keys."""
    defn = code_query_tool.get_definition()
    assert defn["name"] == "code_query"
    assert "superuser" in defn["tags"]
    assert "file_path" in defn["inputSchema"]["properties"]
    assert "question" in defn["inputSchema"]["required"]
