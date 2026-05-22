"""Tests for OpenSearch read-query tools.

Covers:
- opensearch_client.create_os_client: env-var wiring, TLS flags, ValueError on
  empty password, ImportError propagation.
- opensearch_query._is_index_allowed / _get_allowed_patterns: default and
  custom allow-lists, fnmatch matching.
- opensearch_query.OpenSearchQueryTool.call: allow-list rejection, bad JSON
  rejection, missing ASKPANDA_OPENSEARCH error, successful hit projection,
  max_hits clamping, aggregation passthrough, ImportError handling.
- opensearch_promptlog_query.OpenSearchPromptlogQueryTool.call: index injection,
  default source-field projection, caller-supplied source_fields respected.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# opensearch_client
# ---------------------------------------------------------------------------


class TestCreateOsClient:
    """Unit tests for bamboo.llm.opensearch_client.create_os_client."""

    def test_raises_value_error_on_empty_password(self) -> None:
        """ValueError is raised when an empty password is passed."""
        import bamboo.llm.opensearch_client as _mod

        with pytest.raises(ValueError, match="non-empty"):
            _mod.create_os_client("")

    def test_raises_import_error_when_package_missing(self) -> None:
        """ImportError propagates when opensearch-py is not installed."""
        import bamboo.llm.opensearch_client as _mod

        with patch.dict(sys.modules, {"opensearchpy": None}):  # type: ignore[dict-item]
            with pytest.raises((ImportError, TypeError)):
                _mod.create_os_client("secret")

    def test_passes_host_from_env(self) -> None:
        """ASKPANDA_OPENSEARCH_HOST is forwarded to the OpenSearch constructor."""
        import bamboo.llm.opensearch_client as _mod

        mock_cls = MagicMock(return_value=MagicMock())
        mock_opensearchpy = MagicMock()
        mock_opensearchpy.OpenSearch = mock_cls

        with patch.dict(
            os.environ,
            {"ASKPANDA_OPENSEARCH_HOST": "https://my-host:9200"},
        ):
            with patch.dict(sys.modules, {"opensearchpy": mock_opensearchpy}):
                _mod.create_os_client("secret")

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["hosts"] == ["https://my-host:9200"]

    def test_verify_certs_disabled_by_env(self) -> None:
        """Setting ASKPANDA_OPENSEARCH_VERIFY_CERTS=false disables TLS verification."""
        import bamboo.llm.opensearch_client as _mod

        mock_cls = MagicMock(return_value=MagicMock())
        mock_opensearchpy = MagicMock()
        mock_opensearchpy.OpenSearch = mock_cls

        with patch.dict(
            os.environ, {"ASKPANDA_OPENSEARCH_VERIFY_CERTS": "false"}
        ):
            with patch.dict(sys.modules, {"opensearchpy": mock_opensearchpy}):
                _mod.create_os_client("pw")

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["verify_certs"] is False

    def test_verify_certs_enabled_by_default(self) -> None:
        """TLS verification is True when the env var is absent."""
        import bamboo.llm.opensearch_client as _mod

        mock_cls = MagicMock(return_value=MagicMock())
        mock_opensearchpy = MagicMock()
        mock_opensearchpy.OpenSearch = mock_cls

        env = {
            k: v for k, v in os.environ.items()
            if k != "ASKPANDA_OPENSEARCH_VERIFY_CERTS"
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.dict(sys.modules, {"opensearchpy": mock_opensearchpy}):
                _mod.create_os_client("pw")

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["verify_certs"] is True


# ---------------------------------------------------------------------------
# opensearch_query — allow-list helpers
# ---------------------------------------------------------------------------


class TestAllowList:
    """Unit tests for _is_index_allowed and _get_allowed_patterns."""

    def test_default_patterns_include_promptlog(self) -> None:
        """bamboomcp-promptlog-* is in the default allow-list."""
        from bamboo.tools.opensearch_query import _is_index_allowed

        env = {
            k: v for k, v in os.environ.items()
            if k != "BAMBOO_OPENSEARCH_ALLOWED_INDICES"
        }
        with patch.dict(os.environ, env, clear=True):
            assert _is_index_allowed("bamboomcp-promptlog-*") is True

    def test_default_patterns_include_harvester(self) -> None:
        """atlas_harvesterworkers-* is in the default allow-list."""
        from bamboo.tools.opensearch_query import _is_index_allowed

        env = {
            k: v for k, v in os.environ.items()
            if k != "BAMBOO_OPENSEARCH_ALLOWED_INDICES"
        }
        with patch.dict(os.environ, env, clear=True):
            assert _is_index_allowed("atlas_harvesterworkers-*") is True

    def test_unlisted_pattern_rejected(self) -> None:
        """An index pattern not in the allow-list returns False."""
        from bamboo.tools.opensearch_query import _is_index_allowed

        env = {
            k: v for k, v in os.environ.items()
            if k != "BAMBOO_OPENSEARCH_ALLOWED_INDICES"
        }
        with patch.dict(os.environ, env, clear=True):
            assert _is_index_allowed("secret-index-*") is False

    def test_custom_allow_list_from_env(self) -> None:
        """BAMBOO_OPENSEARCH_ALLOWED_INDICES overrides the default."""
        from bamboo.tools.opensearch_query import _is_index_allowed

        with patch.dict(
            os.environ,
            {"BAMBOO_OPENSEARCH_ALLOWED_INDICES": "custom-index-*,other-*"},
        ):
            assert _is_index_allowed("custom-index-*") is True
            assert _is_index_allowed("bamboomcp-promptlog-*") is False

    def test_concrete_index_name_matching_wildcard_allowed(self) -> None:
        """A concrete daily index name matches the wildcard pattern."""
        from bamboo.tools.opensearch_query import _is_index_allowed

        env = {
            k: v for k, v in os.environ.items()
            if k != "BAMBOO_OPENSEARCH_ALLOWED_INDICES"
        }
        with patch.dict(os.environ, env, clear=True):
            assert _is_index_allowed("bamboomcp-promptlog-2026.05.19") is True


# ---------------------------------------------------------------------------
# opensearch_query — OpenSearchQueryTool.call
# ---------------------------------------------------------------------------


def _make_mock_result(
    hits: list[dict[str, Any]] | None = None,
    total: int = 0,
    took_ms: int = 5,
) -> dict[str, Any]:
    """Build a fake _run_query return value."""
    return {
        "hits": hits or [],
        "total": total,
        "took_ms": took_ms,
        "aggregations": {},
    }


class TestOpenSearchQueryToolCall:
    """Tests for OpenSearchQueryTool.call."""

    def _call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run tool.call synchronously and parse the JSON result."""
        from bamboo.tools.opensearch_query import opensearch_query_tool

        raw = asyncio.run(opensearch_query_tool.call(arguments))
        # text_content returns list[dict], each dict has 'text' key.
        first = raw[0] if isinstance(raw, list) else raw
        text = first["text"] if isinstance(first, dict) else first.text
        return json.loads(text)

    def test_rejects_unlisted_index(self) -> None:
        """Returns an error dict when the index pattern is not allowed."""
        env = {
            k: v for k, v in os.environ.items()
            if k != "BAMBOO_OPENSEARCH_ALLOWED_INDICES"
        }
        with patch.dict(os.environ, env, clear=True):
            result = self._call({
                "index_pattern": "forbidden-index-*",
                "query": '{"query":{"match_all":{}}}',
            })
        assert "error" in result
        assert "allow-list" in result["error"]

    def test_rejects_bad_json_query(self) -> None:
        """Returns an error dict when the query string is not valid JSON."""
        result = self._call({
            "index_pattern": "bamboomcp-promptlog-*",
            "query": "not json {",
        })
        assert "error" in result

    def test_rejects_json_array_query(self) -> None:
        """Returns an error dict when the query is a JSON array, not object."""
        result = self._call({
            "index_pattern": "bamboomcp-promptlog-*",
            "query": "[1, 2, 3]",
        })
        assert "error" in result

    def test_missing_askpanda_opensearch_returns_error(self) -> None:
        """Returns an error dict when ASKPANDA_OPENSEARCH is not set."""
        env = {k: v for k, v in os.environ.items() if k != "ASKPANDA_OPENSEARCH"}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "bamboo.tools.opensearch_query._run_query",
                side_effect=RuntimeError("ASKPANDA_OPENSEARCH is not set"),
            ):
                result = self._call({
                    "index_pattern": "bamboomcp-promptlog-*",
                    "query": '{"query":{"match_all":{}}}',
                })
        assert "error" in result

    def test_successful_query_returns_hits(self) -> None:
        """Returns hits, total, took_ms, and aggregations on success."""
        fake_result = _make_mock_result(
            hits=[{"@timestamp": "2026-05-19T10:00:00Z", "provider": "gemini"}],
            total=1,
            took_ms=3,
        )
        with patch(
            "bamboo.tools.opensearch_query._run_query",
            return_value=fake_result,
        ):
            result = self._call({
                "index_pattern": "bamboomcp-promptlog-*",
                "query": '{"query":{"match_all":{}}}',
            })
        assert result["total"] == 1
        assert len(result["hits"]) == 1
        assert result["hits"][0]["provider"] == "gemini"
        assert result["took_ms"] == 3
        assert "aggregations" in result

    def test_max_hits_clamped_to_hard_cap(self) -> None:
        """max_hits values above MAX_HITS_HARD_CAP are silently clamped."""
        from bamboo.tools.opensearch_query import MAX_HITS_HARD_CAP

        captured: list[int] = []

        def _fake_run(
            index_pattern: str,
            query: dict[str, Any],
            max_hits: int,
            source_fields: list[str] | None,
        ) -> dict[str, Any]:
            captured.append(max_hits)
            return _make_mock_result()

        with patch("bamboo.tools.opensearch_query._run_query", side_effect=_fake_run):
            self._call({
                "index_pattern": "bamboomcp-promptlog-*",
                "query": '{"query":{"match_all":{}}}',
                "max_hits": MAX_HITS_HARD_CAP + 500,
            })

        assert captured[0] == MAX_HITS_HARD_CAP

    def test_aggregations_passed_through(self) -> None:
        """Aggregation results from OpenSearch are included in the response."""
        fake_result = {
            "hits": [],
            "total": 0,
            "took_ms": 2,
            "aggregations": {
                "tools": {"buckets": [{"key": "cric_query", "doc_count": 42}]}
            },
        }
        with patch(
            "bamboo.tools.opensearch_query._run_query",
            return_value=fake_result,
        ):
            result = self._call({
                "index_pattern": "bamboomcp-promptlog-*",
                "query": (
                    '{"query":{"match_all":{}},'
                    '"aggs":{"tools":{"terms":{"field":"tools_used"}}},'
                    '"size":0}'
                ),
            })
        assert result["aggregations"]["tools"]["buckets"][0]["key"] == "cric_query"

    def test_import_error_returns_error_dict(self) -> None:
        """Missing opensearch-py is surfaced as an error dict, not an exception."""
        with patch(
            "bamboo.tools.opensearch_query._run_query",
            side_effect=ImportError("No module named 'opensearchpy'"),
        ):
            result = self._call({
                "index_pattern": "bamboomcp-promptlog-*",
                "query": '{"query":{"match_all":{}}}',
            })
        assert "error" in result
        assert "opensearch-py" in result["error"]

    def test_source_fields_forwarded_to_run_query(self) -> None:
        """source_fields list is passed through to _run_query."""
        captured_fields: list[list[str] | None] = []

        def _fake_run(
            index_pattern: str,
            query: dict[str, Any],
            max_hits: int,
            source_fields: list[str] | None,
        ) -> dict[str, Any]:
            captured_fields.append(source_fields)
            return _make_mock_result()

        with patch("bamboo.tools.opensearch_query._run_query", side_effect=_fake_run):
            self._call({
                "index_pattern": "bamboomcp-promptlog-*",
                "query": '{"query":{"match_all":{}}}',
                "source_fields": ["@timestamp", "provider"],
            })

        assert captured_fields[0] == ["@timestamp", "provider"]


# ---------------------------------------------------------------------------
# opensearch_promptlog_query — OpenSearchPromptlogQueryTool.call
# ---------------------------------------------------------------------------


class TestOpenSearchPromptlogQueryToolCall:
    """Tests for OpenSearchPromptlogQueryTool.call."""

    def _call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run tool.call synchronously and parse the JSON result."""
        from bamboo.tools.opensearch_promptlog_query import (
            opensearch_promptlog_query_tool,
        )

        raw = asyncio.run(opensearch_promptlog_query_tool.call(arguments))
        first = raw[0] if isinstance(raw, list) else raw
        text = first["text"] if isinstance(first, dict) else first.text
        return json.loads(text)

    def test_injects_promptlog_index_pattern(self) -> None:
        """The tool always queries bamboomcp-promptlog-* regardless of caller."""
        from bamboo.tools.opensearch_promptlog_query import PROMPTLOG_INDEX_PATTERN

        captured_pattern: list[str] = []

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_pattern.append(args.get("index_pattern", ""))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        with patch(
            "bamboo.tools.opensearch_query.opensearch_query_tool.call",
            side_effect=_fake_call,
        ):
            self._call({"query": '{"query":{"match_all":{}}}'})

        assert captured_pattern[0] == PROMPTLOG_INDEX_PATTERN

    def test_default_source_fields_applied_when_not_specified(self) -> None:
        """DEFAULT_SOURCE_FIELDS is injected when the caller omits source_fields."""
        from bamboo.tools.opensearch_promptlog_query import DEFAULT_SOURCE_FIELDS

        captured_fields: list[Any] = []

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_fields.append(args.get("source_fields"))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        with patch(
            "bamboo.tools.opensearch_query.opensearch_query_tool.call",
            side_effect=_fake_call,
        ):
            self._call({"query": '{"query":{"match_all":{}}}'})

        assert captured_fields[0] == DEFAULT_SOURCE_FIELDS

    def test_caller_supplied_source_fields_respected(self) -> None:
        """When the caller provides source_fields, DEFAULT_SOURCE_FIELDS is not injected."""
        captured_fields: list[Any] = []

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_fields.append(args.get("source_fields"))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        with patch(
            "bamboo.tools.opensearch_query.opensearch_query_tool.call",
            side_effect=_fake_call,
        ):
            self._call({
                "query": '{"query":{"match_all":{}}}',
                "source_fields": ["response"],
            })

        assert captured_fields[0] == ["response"]

    def test_empty_source_fields_list_respected(self) -> None:
        """An explicit empty list for source_fields is passed through unchanged."""
        captured_fields: list[Any] = []

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_fields.append(args.get("source_fields"))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        with patch(
            "bamboo.tools.opensearch_query.opensearch_query_tool.call",
            side_effect=_fake_call,
        ):
            self._call({
                "query": '{"query":{"match_all":{}}}',
                "source_fields": [],
            })

        assert captured_fields[0] == []

    def test_tool_definition_requires_only_query(self) -> None:
        """Only 'query' is listed as required in the input schema."""
        from bamboo.tools.opensearch_promptlog_query import (
            OpenSearchPromptlogQueryTool,
        )

        defn = OpenSearchPromptlogQueryTool.get_definition()
        assert defn["inputSchema"]["required"] == ["query"]

    def test_max_hits_forwarded(self) -> None:
        """max_hits from the caller is forwarded to opensearch_query_tool."""
        captured_max: list[Any] = []

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_max.append(args.get("max_hits"))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        with patch(
            "bamboo.tools.opensearch_query.opensearch_query_tool.call",
            side_effect=_fake_call,
        ):
            self._call({
                "query": '{"query":{"match_all":{}}}',
                "max_hits": 25,
            })

        assert captured_max[0] == 25
