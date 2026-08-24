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

    def test_valid_json_query_forwarded_unchanged(self) -> None:
        """A valid JSON query string is not sent through DSL generation."""
        captured_queries: list[str] = []

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_queries.append(args.get("query", ""))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        dsl = '{"query":{"match_all":{}}}'
        with patch(
            "bamboo.tools.opensearch_query.opensearch_query_tool.call",
            side_effect=_fake_call,
        ):
            self._call({"query": dsl})

        # The query forwarded to opensearch_query_tool must be exactly the
        # JSON the caller supplied — _generate_dsl must NOT have been invoked.
        assert captured_queries[0] == dsl

    def test_natural_language_query_generates_dsl(self) -> None:
        """When query is not JSON, _generate_dsl is called and its result forwarded."""
        generated_dsl = '{"query":{"range":{"@timestamp":{"gte":"now/d"}}}}'
        captured_queries: list[str] = []

        async def _fake_generate(question: str) -> str:
            return generated_dsl

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_queries.append(args.get("query", ""))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        with patch(
            "bamboo.tools.opensearch_promptlog_query._generate_dsl",
            side_effect=_fake_generate,
        ):
            with patch(
                "bamboo.tools.opensearch_query.opensearch_query_tool.call",
                side_effect=_fake_call,
            ):
                self._call({"query": "Show me all ratings from today"})

        assert captured_queries[0] == generated_dsl

    def test_dsl_generation_failure_returns_error_payload(self) -> None:
        """When _generate_dsl returns empty string, an error dict is returned."""
        async def _fake_generate(question: str) -> str:
            return ""

        with patch(
            "bamboo.tools.opensearch_promptlog_query._generate_dsl",
            side_effect=_fake_generate,
        ):
            result = self._call({"query": "some natural language question"})

        assert "error" in result
        assert "natural language question" in result["error"]

    def test_natural_language_still_injects_index_pattern(self) -> None:
        """Even when DSL is generated from NL, the index pattern is injected."""
        captured_args: list[dict[str, Any]] = []

        async def _fake_generate(question: str) -> str:
            return '{"query":{"match_all":{}}}'

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_args.append(dict(args))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        from bamboo.tools.opensearch_promptlog_query import PROMPTLOG_INDEX_PATTERN

        with patch(
            "bamboo.tools.opensearch_promptlog_query._generate_dsl",
            side_effect=_fake_generate,
        ):
            with patch(
                "bamboo.tools.opensearch_query.opensearch_query_tool.call",
                side_effect=_fake_call,
            ):
                self._call({"query": "How many turns today?"})

        assert captured_args[0]["index_pattern"] == PROMPTLOG_INDEX_PATTERN

    def test_natural_language_applies_default_source_fields(self) -> None:
        """Default source fields are applied for NL queries that omit source_fields."""
        captured_args: list[dict[str, Any]] = []

        async def _fake_generate(question: str) -> str:
            return '{"query":{"match_all":{}}}'

        async def _fake_call(args: dict[str, Any]) -> Any:
            captured_args.append(dict(args))
            from bamboo.tools.base import text_content
            return text_content(json.dumps(_make_mock_result()))

        from bamboo.tools.opensearch_promptlog_query import DEFAULT_SOURCE_FIELDS

        with patch(
            "bamboo.tools.opensearch_promptlog_query._generate_dsl",
            side_effect=_fake_generate,
        ):
            with patch(
                "bamboo.tools.opensearch_query.opensearch_query_tool.call",
                side_effect=_fake_call,
            ):
                self._call({"query": "show me recent turns"})

        assert captured_args[0]["source_fields"] == DEFAULT_SOURCE_FIELDS


# ---------------------------------------------------------------------------
# _generate_dsl unit tests
# ---------------------------------------------------------------------------


async def _async_return(value: Any) -> Any:
    """Return *value* from an async context immediately."""
    return value


async def _async_raise(exc: Exception) -> Any:
    """Raise *exc* from an async context immediately."""
    raise exc


class TestGenerateDsl:
    """Unit tests for opensearch_promptlog_query._generate_dsl."""

    def _run(self, question: str, llm_reply: str) -> str:
        """Run _generate_dsl with a mocked LLM that replies with *llm_reply*.

        Args:
            question: Natural-language question to pass.
            llm_reply: Text the mocked LLM returns.

        Returns:
            Whatever _generate_dsl returns.
        """
        from bamboo.tools.opensearch_promptlog_query import _generate_dsl

        mock_resp = MagicMock()
        mock_resp.text = llm_reply

        mock_client = MagicMock()
        mock_client.generate = MagicMock(
            side_effect=lambda **kw: _async_return(mock_resp)
        )

        mock_selector = MagicMock()
        mock_selector.registry = {"default": MagicMock()}
        mock_selector.default_profile = "default"

        mock_manager = MagicMock()
        mock_manager.get_client = MagicMock(
            side_effect=lambda spec: _async_return(mock_client)
        )

        with patch(
            "bamboo.llm.runtime.get_llm_selector",
            return_value=mock_selector,
        ):
            with patch(
                "bamboo.llm.runtime.get_llm_manager",
                return_value=mock_manager,
            ):
                return asyncio.run(_generate_dsl(question))

    def test_valid_json_reply_returned_verbatim(self) -> None:
        """A clean JSON object response from the LLM is returned as-is."""
        dsl = '{"query":{"match_all":{}}}'
        result = self._run("show all turns", dsl)
        assert result == dsl

    def test_strips_json_fenced_markdown(self) -> None:
        """JSON wrapped in ```json…``` fences is unwrapped correctly."""
        dsl_obj = '{"query":{"match_all":{}}}'
        fenced = f"```json\n{dsl_obj}\n```"
        result = self._run("show all turns", fenced)
        parsed = json.loads(result)
        assert "query" in parsed

    def test_strips_plain_fenced_markdown(self) -> None:
        """JSON wrapped in plain ```…``` fences is unwrapped correctly."""
        dsl_obj = '{"size":0}'
        fenced = f"```\n{dsl_obj}\n```"
        result = self._run("count turns", fenced)
        assert json.loads(result) == {"size": 0}

    def test_non_json_llm_reply_returns_empty_string(self) -> None:
        """If the LLM returns plain text (not JSON), _generate_dsl returns ''."""
        result = self._run("foo", "I cannot answer that.")
        assert result == ""

    def test_json_array_reply_returns_empty_string(self) -> None:
        """A JSON array instead of an object returns ''."""
        result = self._run("foo", "[1, 2, 3]")
        assert result == ""

    def test_llm_exception_returns_empty_string(self) -> None:
        """If the LLM call raises any exception, _generate_dsl returns ''."""
        from bamboo.tools.opensearch_promptlog_query import _generate_dsl

        mock_selector = MagicMock()
        mock_selector.registry = {"default": MagicMock()}
        mock_selector.default_profile = "default"

        mock_manager = MagicMock()
        mock_manager.get_client = MagicMock(
            side_effect=lambda spec: _async_raise(RuntimeError("network error"))
        )

        with patch(
            "bamboo.llm.runtime.get_llm_selector",
            return_value=mock_selector,
        ):
            with patch(
                "bamboo.llm.runtime.get_llm_manager",
                return_value=mock_manager,
            ):
                result = asyncio.run(_generate_dsl("show turns"))

        assert result == ""

    def test_dsl_system_prompt_documents_schema_fields(self) -> None:
        """The DSL generation system prompt covers all key schema fields."""
        from bamboo.tools.opensearch_promptlog_query import _DSL_GENERATION_SYSTEM_PROMPT

        for field in ("@timestamp", "session_id", "tools_used", "rating", "raw_question"):
            assert field in _DSL_GENERATION_SYSTEM_PROMPT, (
                f"Expected field {field!r} in _DSL_GENERATION_SYSTEM_PROMPT"
            )

    def test_dsl_system_prompt_contains_ratings_example(self) -> None:
        """The DSL generation system prompt includes a ratings example."""
        from bamboo.tools.opensearch_promptlog_query import _DSL_GENERATION_SYSTEM_PROMPT

        assert "rating" in _DSL_GENERATION_SYSTEM_PROMPT
        # Date-math anchors for today must appear.
        assert "now/d" in _DSL_GENERATION_SYSTEM_PROMPT

    def test_dsl_system_prompt_has_mandatory_rating_filter_rule(self) -> None:
        """The rating filter rule must be imperative (MUST/CRITICAL), not merely advisory."""
        from bamboo.tools.opensearch_promptlog_query import _DSL_GENERATION_SYSTEM_PROMPT

        # The rule should use strong language so the LLM cannot skip the filter.
        upper = _DSL_GENERATION_SYSTEM_PROMPT.upper()
        assert "RATING" in upper
        assert "MUST" in upper or "CRITICAL" in upper or "MANDATORY" in upper

    def test_dsl_system_prompt_rating_filter_includes_gte_1(self) -> None:
        """The ratings example DSL in the prompt must include gte:1 on the rating field."""
        from bamboo.tools.opensearch_promptlog_query import _DSL_GENERATION_SYSTEM_PROMPT

        # The example output for ratings must contain the numeric filter.
        assert '"rating":{"gte":1}' in _DSL_GENERATION_SYSTEM_PROMPT or \
               '"rating": {"gte": 1}' in _DSL_GENERATION_SYSTEM_PROMPT


class TestDefaultSourceFieldsContainsRating:
    """rating must be in DEFAULT_SOURCE_FIELDS so hits always carry it."""

    def test_rating_in_default_source_fields(self) -> None:
        """rating field must appear in DEFAULT_SOURCE_FIELDS."""
        from bamboo.tools.opensearch_promptlog_query import DEFAULT_SOURCE_FIELDS

        assert "rating" in DEFAULT_SOURCE_FIELDS

    def test_timestamp_in_default_source_fields(self) -> None:
        """@timestamp must remain in DEFAULT_SOURCE_FIELDS."""
        from bamboo.tools.opensearch_promptlog_query import DEFAULT_SOURCE_FIELDS

        assert "@timestamp" in DEFAULT_SOURCE_FIELDS

    def test_raw_question_in_default_source_fields(self) -> None:
        """raw_question must remain in DEFAULT_SOURCE_FIELDS."""
        from bamboo.tools.opensearch_promptlog_query import DEFAULT_SOURCE_FIELDS

        assert "raw_question" in DEFAULT_SOURCE_FIELDS


# ---------------------------------------------------------------------------
# jobs_query_schema.build_sql_prompt — date injection tests
# ---------------------------------------------------------------------------


class TestBuildSqlPromptDateInjection:
    """Tests that build_sql_prompt injects the current UTC date and time."""

    def test_returns_two_messages(self) -> None:
        """build_sql_prompt returns a system + user message pair."""
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        msgs = build_sql_prompt("How many failed jobs at BNL?")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_user_message_contains_question(self) -> None:
        """The user message must echo the original question verbatim."""
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        q = "How many failed jobs at BNL?"
        msgs = build_sql_prompt(q)
        assert msgs[1]["content"] == q

    def test_system_message_contains_today_anchor(self) -> None:
        """The system prompt must contain a TODAY= anchor."""
        import datetime
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        msgs = build_sql_prompt("whatever")
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        assert f"TODAY={today}" in msgs[0]["content"]

    def test_system_message_contains_now_anchor(self) -> None:
        """The system prompt must contain a NOW= anchor in ISO-Z format."""
        import re
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        msgs = build_sql_prompt("whatever")
        # Expect NOW=2026-06-29T15:00:00Z (or similar)
        assert re.search(r"NOW=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", msgs[0]["content"])

    def test_system_message_contains_date_rule(self) -> None:
        """The DATE RULE block must be present to warn about statechangetime scope."""
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        msgs = build_sql_prompt("histogram of failures")
        assert "DATE RULE" in msgs[0]["content"]
        assert "statechangetime" in msgs[0]["content"]

    def test_system_message_relaxes_queue_filter_rule(self) -> None:
        """The _queue rule must no longer require filtering on ALL queries."""
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        msgs = build_sql_prompt("global failure histogram")
        system = msgs[0]["content"]
        # Old unconditional rule should be gone.
        assert "Always filter by _queue" not in system
        # New conditional rule should be present.
        assert "ONLY when the question mentions a specific site" in system

    def test_system_message_contains_global_histogram_example(self) -> None:
        """A global histogram example (no _queue filter) must be in the prompt."""
        from askpanda_atlas.jobs_query_schema import build_sql_prompt

        msgs = build_sql_prompt("histogram")
        system = msgs[0]["content"]
        assert "histogram" in system.lower()

# ---------------------------------------------------------------------------
# _build_promptlog_plan max_hits tests
# ---------------------------------------------------------------------------


class TestBuildPromptlogPlanMaxHits:
    """Tests that _build_promptlog_plan passes appropriate max_hits."""

    def _plan_args(self, question: str) -> dict[str, Any]:
        """Return the tool-call arguments from _build_promptlog_plan."""
        from bamboo.tools.bamboo_answer import _build_promptlog_plan, ReusePolicy
        plan = _build_promptlog_plan(question, ReusePolicy())
        return plan.tool_calls[0].arguments

    def test_all_ratings_gets_max_hits_100(self) -> None:
        """'show me all ratings' must request max_hits=100."""
        args = self._plan_args("Show me all ratings from today")
        assert args["max_hits"] == 100

    def test_all_questions_gets_max_hits_100(self) -> None:
        """'all questions' must request max_hits=100."""
        args = self._plan_args("What are all questions asked today?")
        assert args["max_hits"] == 100

    def test_show_all_turns_gets_max_hits_100(self) -> None:
        """'show all turns' must request max_hits=100."""
        args = self._plan_args("show all turns from this session")
        assert args["max_hits"] == 100

    def test_scoped_question_gets_max_hits_50(self) -> None:
        """A question without 'all' intent gets max_hits=50, not 10."""
        args = self._plan_args("Show me the most recent ratings")
        assert args["max_hits"] == 50

    def test_which_tools_gets_max_hits_50(self) -> None:
        """'which tools were used today' has no all-intent; gets max_hits=50."""
        args = self._plan_args("which tools were used most today?")
        assert args["max_hits"] == 50

    def test_query_arg_is_original_question(self) -> None:
        """The raw question is always passed as the query argument."""
        q = "Show me all ratings from today"
        args = self._plan_args(q)
        assert args["query"] == q


# ---------------------------------------------------------------------------
# _pick_synthesis_prompt opensearch routing tests
# ---------------------------------------------------------------------------


class TestPickSynthesisPromptOpenSearch:
    """Tests that opensearch tools route to _SYSTEM_PROMPTLOG_QUERY."""

    def test_opensearch_promptlog_query_uses_promptlog_prompt(self) -> None:
        """opensearch_promptlog_query must route to _SYSTEM_PROMPTLOG_QUERY."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_PROMPTLOG_QUERY,
        )
        result = _pick_synthesis_prompt(["opensearch_promptlog_query"])
        assert result is _SYSTEM_PROMPTLOG_QUERY

    def test_opensearch_query_uses_promptlog_prompt(self) -> None:
        """opensearch_query must also route to _SYSTEM_PROMPTLOG_QUERY."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_PROMPTLOG_QUERY,
        )
        result = _pick_synthesis_prompt(["opensearch_query"])
        assert result is _SYSTEM_PROMPTLOG_QUERY

    def test_panda_jobs_query_not_affected(self) -> None:
        """panda_jobs_query must still route to _SYSTEM_JOBS_QUERY."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_JOBS_QUERY,
        )
        result = _pick_synthesis_prompt(["panda_jobs_query"])
        assert result is _SYSTEM_JOBS_QUERY

    def test_site_health_compound_check_unaffected(self) -> None:
        """panda_harvester_workers + panda_jobs_query must still give _SYSTEM_SITE_HEALTH."""
        from bamboo.tools.bamboo_executor import (
            _pick_synthesis_prompt,
            _SYSTEM_SITE_HEALTH,
        )
        result = _pick_synthesis_prompt(["panda_harvester_workers", "panda_jobs_query"])
        assert result is _SYSTEM_SITE_HEALTH


# ---------------------------------------------------------------------------
# _SYSTEM_PROMPTLOG_QUERY content tests
# ---------------------------------------------------------------------------


class TestSystemPromptlogQueryContent:
    """Tests for the content of the _SYSTEM_PROMPTLOG_QUERY synthesis prompt."""

    def test_no_mermaid_instruction(self) -> None:
        """The prompt must explicitly forbid Mermaid diagrams."""
        from bamboo.tools.bamboo_executor import _SYSTEM_PROMPTLOG_QUERY

        assert "Mermaid" in _SYSTEM_PROMPTLOG_QUERY or "mermaid" in _SYSTEM_PROMPTLOG_QUERY
        assert "NOT" in _SYSTEM_PROMPTLOG_QUERY or "Do NOT" in _SYSTEM_PROMPTLOG_QUERY

    def test_truncation_rule_present(self) -> None:
        """The prompt must tell the synthesiser to report total vs retrieved."""
        from bamboo.tools.bamboo_executor import _SYSTEM_PROMPTLOG_QUERY

        assert "total" in _SYSTEM_PROMPTLOG_QUERY.lower()
        assert "truncat" in _SYSTEM_PROMPTLOG_QUERY.lower() or "cap" in _SYSTEM_PROMPTLOG_QUERY.lower()

    def test_ratings_table_format_specified(self) -> None:
        """The prompt must specify a table format for ratings display queries."""
        from bamboo.tools.bamboo_executor import _SYSTEM_PROMPTLOG_QUERY

        assert "table" in _SYSTEM_PROMPTLOG_QUERY.lower()
        assert "rating" in _SYSTEM_PROMPTLOG_QUERY.lower()

    def test_no_mermaid_guidance_appended(self) -> None:
        """_MERMAID_GUIDANCE must NOT be appended to _SYSTEM_PROMPTLOG_QUERY."""
        from bamboo.tools.bamboo_executor import _SYSTEM_PROMPTLOG_QUERY

        # _MERMAID_GUIDANCE starts with this distinctive phrase.
        assert "Diagram rule:" not in _SYSTEM_PROMPTLOG_QUERY
