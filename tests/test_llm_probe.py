"""Tests for the LLM connectivity probe tool (bamboo_llm_probe)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bamboo.tools.llm_probe import (
    LLMProbeTool,
    _STATUS_AUTH_ERROR,
    _STATUS_CONFIG_ERROR,
    _STATUS_NOT_CONFIGURED,
    _STATUS_OK,
    _STATUS_PROVIDER_ERROR,
    _STATUS_RATE_LIMIT,
    _STATUS_TIMEOUT,
    _classify_error,
    _run_probe,
    bamboo_llm_probe_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_result(result: list[dict[str, Any]]) -> dict[str, str]:
    """Extract and JSON-parse the first text content block from a tool result."""
    assert result and isinstance(result, list)
    text = result[0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# _classify_error
# ---------------------------------------------------------------------------

class TestClassifyError:
    """Unit tests for the exception-to-status mapping helper."""

    def test_llm_config_error(self) -> None:
        """LLMConfigError maps to config_error status."""
        from bamboo.llm.exceptions import LLMConfigError  # noqa: PLC0415

        status, detail = _classify_error(LLMConfigError("MISTRAL_API_KEY is not set"))
        assert status == _STATUS_CONFIG_ERROR
        assert "MISTRAL_API_KEY" in detail

    def test_llm_rate_limit_error(self) -> None:
        """LLMRateLimitError maps to rate_limit status."""
        from bamboo.llm.exceptions import LLMRateLimitError  # noqa: PLC0415

        status, _ = _classify_error(LLMRateLimitError("429 too many requests"))
        assert status == _STATUS_RATE_LIMIT

    def test_llm_timeout_error(self) -> None:
        """LLMTimeoutError maps to timeout status."""
        from bamboo.llm.exceptions import LLMTimeoutError  # noqa: PLC0415

        status, _ = _classify_error(LLMTimeoutError("timed out"))
        assert status == _STATUS_TIMEOUT

    def test_llm_provider_error_auth_401(self) -> None:
        """LLMProviderError with 401 maps to auth_error."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        status, _ = _classify_error(LLMProviderError("401 unauthorized"))
        assert status == _STATUS_AUTH_ERROR

    def test_llm_provider_error_auth_403(self) -> None:
        """LLMProviderError with 403 maps to auth_error."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        status, _ = _classify_error(LLMProviderError("403 forbidden"))
        assert status == _STATUS_AUTH_ERROR

    def test_llm_provider_error_invalid_api_key(self) -> None:
        """LLMProviderError with 'invalid api key' maps to auth_error."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        status, _ = _classify_error(LLMProviderError("invalid api key supplied"))
        assert status == _STATUS_AUTH_ERROR

    def test_llm_provider_error_rate_limit(self) -> None:
        """LLMProviderError with rate-limit signal maps to rate_limit."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        status, _ = _classify_error(LLMProviderError("429 rate limit exceeded"))
        assert status == _STATUS_RATE_LIMIT

    def test_llm_provider_error_timeout(self) -> None:
        """LLMProviderError with timeout signal maps to timeout."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        status, _ = _classify_error(LLMProviderError("request timed out"))
        assert status == _STATUS_TIMEOUT

    def test_llm_provider_error_generic(self) -> None:
        """Generic LLMProviderError maps to provider_error."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        status, _ = _classify_error(LLMProviderError("unexpected server error"))
        assert status == _STATUS_PROVIDER_ERROR

    def test_unknown_exception_with_auth_signal(self) -> None:
        """Plain exception with auth keywords maps to auth_error."""
        status, _ = _classify_error(RuntimeError("401 unauthorized response"))
        assert status == _STATUS_AUTH_ERROR

    def test_unknown_exception_generic(self) -> None:
        """Plain exception without recognisable keywords maps to provider_error."""
        status, _ = _classify_error(RuntimeError("something went wrong"))
        assert status == _STATUS_PROVIDER_ERROR


# ---------------------------------------------------------------------------
# _run_probe (async)
# ---------------------------------------------------------------------------

class TestRunProbe:
    """Tests for the async _run_probe helper."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Successful generate call returns ok status with provider/model detail."""
        mock_spec = MagicMock()
        mock_spec.provider = "mistral"
        mock_spec.model = "mistral-large-latest"

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_spec

        mock_selector = MagicMock()
        mock_selector.default_profile = "default"
        mock_selector.registry = mock_registry

        mock_client = AsyncMock()
        mock_client.generate.return_value = MagicMock(text="p", usage=None)

        mock_manager = AsyncMock()
        mock_manager.get_client.return_value = mock_client

        with (
            patch("bamboo.llm.runtime.get_llm_selector", return_value=mock_selector),
            patch("bamboo.llm.runtime.get_llm_manager", return_value=mock_manager),
        ):
            result = await _run_probe()

        assert result["status"] == _STATUS_OK
        assert "mistral" in result["detail"]
        assert "mistral-large-latest" in result["detail"]

    @pytest.mark.asyncio
    async def test_runtime_not_initialized(self) -> None:
        """RuntimeError from get_llm_selector returns not_configured."""
        with patch(
            "bamboo.llm.runtime.get_llm_selector",
            side_effect=RuntimeError("LLM selector is not initialized"),
        ):
            result = await _run_probe()

        assert result["status"] == _STATUS_NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_auth_error_from_generate(self) -> None:
        """401 error from generate maps to auth_error status."""
        from bamboo.llm.exceptions import LLMProviderError  # noqa: PLC0415

        mock_spec = MagicMock()
        mock_spec.provider = "mistral"
        mock_spec.model = "mistral-large-latest"

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_spec

        mock_selector = MagicMock()
        mock_selector.default_profile = "default"
        mock_selector.registry = mock_registry

        mock_client = AsyncMock()
        mock_client.generate.side_effect = LLMProviderError(
            "Mistral error after retries: 401 Unauthorized"
        )

        mock_manager = AsyncMock()
        mock_manager.get_client.return_value = mock_client

        with (
            patch("bamboo.llm.runtime.get_llm_selector", return_value=mock_selector),
            patch("bamboo.llm.runtime.get_llm_manager", return_value=mock_manager),
        ):
            result = await _run_probe()

        assert result["status"] == _STATUS_AUTH_ERROR

    @pytest.mark.asyncio
    async def test_config_error_missing_key(self) -> None:
        """LLMConfigError raised by get_client maps to config_error."""
        from bamboo.llm.exceptions import LLMConfigError  # noqa: PLC0415

        mock_registry = MagicMock()
        mock_registry.get.return_value = MagicMock()

        mock_selector = MagicMock()
        mock_selector.default_profile = "default"
        mock_selector.registry = mock_registry

        mock_manager = AsyncMock()
        mock_manager.get_client.side_effect = LLMConfigError("MISTRAL_API_KEY is not set")

        with (
            patch("bamboo.llm.runtime.get_llm_selector", return_value=mock_selector),
            patch("bamboo.llm.runtime.get_llm_manager", return_value=mock_manager),
        ):
            result = await _run_probe()

        assert result["status"] == _STATUS_CONFIG_ERROR

    @pytest.mark.asyncio
    async def test_no_registry(self) -> None:
        """Selector without registry attribute returns not_configured."""
        mock_selector = MagicMock()
        mock_selector.default_profile = "default"
        mock_selector.registry = None

        with (
            patch("bamboo.llm.runtime.get_llm_selector", return_value=mock_selector),
            patch("bamboo.llm.runtime.get_llm_manager", return_value=AsyncMock()),
        ):
            result = await _run_probe()

        assert result["status"] == _STATUS_NOT_CONFIGURED


# ---------------------------------------------------------------------------
# LLMProbeTool.call (full MCP round-trip)
# ---------------------------------------------------------------------------

class TestLLMProbeTool:
    """Tests for the full tool call method."""

    def test_get_definition(self) -> None:
        """Tool definition has the expected name and input schema."""
        defn = LLMProbeTool.get_definition()
        assert defn["name"] == "bamboo_llm_probe"
        assert "inputSchema" in defn
        assert defn["inputSchema"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_call_returns_ok_json(self) -> None:
        """call() returns a one-element list with a JSON ok payload."""
        with patch(
            "bamboo.tools.llm_probe._run_probe",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "provider=openai model=gpt-4o"},
        ):
            result = await bamboo_llm_probe_tool.call({})

        parsed = _parse_result(result)
        assert parsed["status"] == "ok"
        assert "openai" in parsed["detail"]

    @pytest.mark.asyncio
    async def test_call_returns_auth_error_json(self) -> None:
        """call() propagates auth_error status from the probe."""
        with patch(
            "bamboo.tools.llm_probe._run_probe",
            new_callable=AsyncMock,
            return_value={"status": "auth_error", "detail": "401 Unauthorized"},
        ):
            result = await bamboo_llm_probe_tool.call({})

        parsed = _parse_result(result)
        assert parsed["status"] == "auth_error"

    @pytest.mark.asyncio
    async def test_call_ignores_arguments(self) -> None:
        """call() accepts arbitrary arguments without error."""
        with patch(
            "bamboo.tools.llm_probe._run_probe",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "provider=gemini model=gemini-2.0-flash"},
        ):
            result = await bamboo_llm_probe_tool.call({"unexpected_key": "value"})

        parsed = _parse_result(result)
        assert parsed["status"] == "ok"

    def test_singleton_is_instance(self) -> None:
        """Module-level singleton is an LLMProbeTool instance."""
        assert isinstance(bamboo_llm_probe_tool, LLMProbeTool)
