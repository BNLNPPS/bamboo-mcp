"""Mistral LLM client implementation."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from collections.abc import Sequence

from bamboo.llm.base import LLMClient
from bamboo.llm.exceptions import LLMConfigError, LLMProviderError, LLMTimeoutError
from bamboo.llm.types import GenerateParams, LLMResponse, Message, ModelSpec, TokenUsage


# Keep this small and configurable; mirrors the original AskPanDA approach.
_MISTRAL_CONCURRENCY = asyncio.Semaphore(int(os.getenv("ASKPANDA_MISTRAL_CONCURRENCY", "4")))


class MistralLLMClient(LLMClient):
    """Mistral provider client.

    Uses the official `mistralai` SDK with an async client and `chat.complete_async`.
    """

    def __init__(self, model_spec: ModelSpec) -> None:
        """Initialize the client with a model spec.

        Args:
            model_spec: Model specification for this client.
        """
        super().__init__(model_spec)
        self._client: Any | None = None  # mistralai.Mistral (kept Any to avoid import at module import time)
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Close any underlying client resources."""
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def _get_client(self) -> Any:
        """Lazily initializes and returns the Mistral client.

        Returns:
            An instance of `mistralai.Mistral` (async context entered).

        Raises:
            LLMConfigError: If the API key is missing or the ``mistralai``
                package is not installed.
        """
        if self._client is not None:
            return self._client

        env_name = getattr(self.model_spec, "api_key_env", None) or "MISTRAL_API_KEY"
        api_key = os.getenv(env_name)
        if not api_key:
            raise LLMConfigError("MISTRAL_API_KEY is not set in the environment.")

        async with self._lock:
            if self._client is None:
                # Import lazily so installing mistralai is only required if this provider is used.
                # pylint: disable=import-outside-toplevel, unnecessary-dunder-call
                try:
                    from mistralai import Mistral  # type: ignore
                except ImportError as exc:
                    raise LLMConfigError(
                        "The 'mistralai' package is not installed.  "
                        "Run: pip install -r requirements-mistral.txt"
                    ) from exc

                client = Mistral(api_key=api_key)

                # The SDK supports async context manager usage; enter the async context
                # to initialize the client instance (we keep the instance for reuse).
                await client.__aenter__()
                self._client = client

        return self._client

    def _normalize_messages(self, messages: Sequence[Message]) -> list[dict[str, str]]:
        """Convert normalized messages into Mistral SDK chat message dicts.

        Maps internal ``role`` values to Mistral-accepted roles
        (system/user/assistant). Tool messages are folded into assistant content
        with a ``[tool:<name>]`` prefix.

        Args:
            messages: Sequence of normalized Message dicts.

        Returns:
            List of dicts with 'role' and 'content' keys accepted by the
            Mistral chat completion API.
        """
        out: list[dict[str, str]] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            # Mistral chat API expects roles like system/user/assistant.
            if role == "tool":
                # Minimal safe mapping: tool output becomes assistant-visible content.
                role = "assistant"
                name_val = m.get("name")
                if name_val:
                    content = f"[tool:{name_val}]\n{content}"

            if role not in ("system", "user", "assistant"):
                role = "user"

            out.append({"role": role, "content": content})
        return out

    async def generate(self, messages: Sequence[Message], params: GenerateParams) -> LLMResponse:
        """Generate a completion using Mistral.

        Args:
            messages: Normalized messages.
            params: Generation parameters.

        Returns:
            Normalized LLMResponse.

        Raises:
            LLMConfigError: If the model spec is invalid.
            LLMProviderError: For provider errors.
            LLMTimeoutError: For request timeouts (if we can detect them).
        """
        model = self.model_spec.model
        if not model:
            raise LLMConfigError("ModelSpec.model is empty for Mistral provider.")

        mistral_messages = self._normalize_messages(messages)

        async with _MISTRAL_CONCURRENCY:
            tries = int(os.getenv("ASKPANDA_MISTRAL_RETRIES", "3"))
            backoff = float(os.getenv("ASKPANDA_MISTRAL_BACKOFF_SECONDS", "1.0"))
            last_err: Exception | None = None
            for _ in range(tries):
                try:
                    client = await self._get_client()

                    # Match the original AskPanDA usage style:
                    # res = await client.chat.complete_async(model=..., messages=[...], stream=False)
                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": mistral_messages,
                        "stream": False,
                    }

                    # These args exist in many SDK versions; pass only if present.
                    # (If the SDK rejects them, we'll catch and rethrow with details.)
                    if params.max_tokens is not None:
                        kwargs["max_tokens"] = params.max_tokens
                    kwargs["temperature"] = params.temperature

                    res = await client.chat.complete_async(**kwargs)

                    text = (res.choices[0].message.content or "").strip()

                    # Usage varies by SDK version; best-effort extraction.
                    usage = None
                    try:
                        u = getattr(res, "usage", None)
                        if u is not None:
                            input_tokens = getattr(u, "prompt_tokens", None) or getattr(u, "input_tokens", None)
                            output_tokens = (
                                getattr(u, "completion_tokens", None) or getattr(u, "output_tokens", None)
                            )
                            total_tokens = getattr(u, "total_tokens", None)
                            usage = TokenUsage(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                total_tokens=total_tokens,
                            )
                    except Exception:  # pylint: disable=broad-exception-caught  # pragma: no cover - best-effort parsing
                        usage = None

                    return LLMResponse(text=text, usage=usage, raw=res)

                except asyncio.TimeoutError as exc:
                    raise LLMTimeoutError(str(exc)) from exc
                except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                    # Provider-level errors: capture and retry with backoff.
                    last_err = exc
                    await asyncio.sleep(backoff)
                    backoff *= 2

            raise LLMProviderError(f"Mistral error after retries: {last_err!s}") from last_err
