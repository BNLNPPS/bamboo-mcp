"""Google Gemini LLM client implementation.

Uses the official ``google-generativeai`` SDK.
Install the dependency with::

    pip install -r requirements-gemini.txt
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Any

from bamboo.llm.base import LLMClient
from bamboo.llm.exceptions import LLMConfigError, LLMProviderError, LLMRateLimitError, LLMTimeoutError
from bamboo.llm.types import GenerateParams, LLMResponse, Message, ModelSpec, TokenUsage


_GEMINI_CONCURRENCY = asyncio.Semaphore(int(os.getenv("ASKPANDA_GEMINI_CONCURRENCY", "4")))


class GeminiLLMClient(LLMClient):
    """Google Gemini provider client.

    Uses the ``google-generativeai`` SDK (``google-generativeai>=0.7``).
    The Gemini API separates a ``system_instruction`` from the chat history,
    similar to Anthropic.  This client extracts the first system-role message
    and passes it as ``system_instruction``; the rest become the chat history.

    Retry behaviour mirrors the other clients: exponential backoff with
    ``ASKPANDA_GEMINI_RETRIES`` attempts (default 3) and
    ``ASKPANDA_GEMINI_BACKOFF_SECONDS`` initial wait (default 1.0 s).
    """

    def __init__(self, model_spec: ModelSpec) -> None:
        """Initialise the client with a model spec.

        Args:
            model_spec: Model specification for this client.
        """
        super().__init__(model_spec)
        self._genai: Any | None = None  # google.generativeai module
        self._lock = asyncio.Lock()

    async def _get_genai(self) -> Any:
        """Lazily import and configure the google.generativeai module.

        Returns:
            The configured ``google.generativeai`` module.

        Raises:
            LLMConfigError: If the API key is not set or the SDK is not installed.
        """
        if self._genai is not None:
            return self._genai

        env_name = getattr(self._model_spec, "api_key_env", None) or "GEMINI_API_KEY"
        api_key = os.getenv(env_name)
        if not api_key:
            raise LLMConfigError(
                f"Gemini API key not found.  Set {env_name} in the environment."
            )

        async with self._lock:
            if self._genai is None:
                try:
                    import google.generativeai as genai  # type: ignore  # pylint: disable=import-outside-toplevel
                except ImportError as exc:
                    raise LLMConfigError(
                        "The 'google-generativeai' package is not installed.  "
                        "Run: pip install -r requirements-gemini.txt"
                    ) from exc

                genai.configure(api_key=api_key)  # type: ignore[attr-defined]
                self._genai = genai

        return self._genai

    def _build_history(
        self, messages: Sequence[Message]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convert normalised messages into Gemini history format.

        The Gemini SDK expects ``contents`` as a list of
        ``{"role": "user"|"model", "parts": [{"text": "..."}]}`` dicts, with
        an optional ``system_instruction`` string passed to the model
        constructor.

        Args:
            messages: Sequence of normalised Message dicts.

        Returns:
            A ``(system_instruction, contents)`` tuple.
        """
        system_instruction = ""
        contents: list[dict[str, Any]] = []

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "system" and not system_instruction:
                system_instruction = content
                continue

            # Gemini uses "model" instead of "assistant".
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": content}]})

        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Please respond."}]}]

        return system_instruction, contents

    @staticmethod
    def _parse_response(res: Any) -> LLMResponse:
        """Extract text and token usage from a Gemini API response.

        Args:
            res: Raw response object from ``generate_content_async``.

        Returns:
            Normalised :class:`~bamboo.llm.types.LLMResponse`.
        """
        text = ""
        try:
            text = res.text.strip()
        except Exception:  # pylint: disable=broad-exception-caught
            for part in getattr(res, "parts", []):
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str):
                    text = part_text.strip()
                    break

        usage = None
        try:
            meta = getattr(res, "usage_metadata", None)
            if meta is not None:
                usage = TokenUsage(
                    input_tokens=getattr(meta, "prompt_token_count", None),
                    output_tokens=getattr(meta, "candidates_token_count", None),
                    total_tokens=getattr(meta, "total_token_count", None),
                )
        except Exception:  # pylint: disable=broad-exception-caught
            usage = None

        return LLMResponse(text=text, usage=usage, raw=res)

    async def generate(self, messages: Sequence[Message], params: GenerateParams) -> LLMResponse:
        """Generate a completion using Gemini.

        Args:
            messages: Normalised messages.
            params: Generation parameters.

        Returns:
            Normalised LLMResponse.

        Raises:
            LLMConfigError: If the model spec is invalid or the API key is missing.
            LLMRateLimitError: On HTTP 429 rate-limit responses.
            LLMTimeoutError: On request timeouts.
            LLMProviderError: For all other provider errors after retries are exhausted.
        """
        model_name = self._model_spec.model
        if not model_name:
            raise LLMConfigError("ModelSpec.model is empty for Gemini provider.")

        system_instruction, contents = self._build_history(messages)

        async with _GEMINI_CONCURRENCY:
            tries = int(os.getenv("ASKPANDA_GEMINI_RETRIES", "3"))
            backoff = float(os.getenv("ASKPANDA_GEMINI_BACKOFF_SECONDS", "1.0"))
            last_err: Exception | None = None

            for _ in range(tries):
                try:
                    genai = await self._get_genai()

                    model_kwargs: dict[str, Any] = {"model_name": model_name}
                    if system_instruction:
                        model_kwargs["system_instruction"] = system_instruction

                    generation_config: dict[str, Any] = {"temperature": params.temperature}
                    if params.max_tokens is not None:
                        generation_config["max_output_tokens"] = params.max_tokens

                    model = genai.GenerativeModel(
                        **model_kwargs,
                        generation_config=generation_config,
                    )
                    res = await model.generate_content_async(contents)
                    return self._parse_response(res)

                except asyncio.TimeoutError as exc:
                    raise LLMTimeoutError(str(exc)) from exc
                except (LLMConfigError, LLMRateLimitError):
                    raise  # permanent failures — do not retry
                except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                    exc_str = str(exc).lower()
                    if "429" in exc_str or "rate limit" in exc_str or "quota" in exc_str:
                        raise LLMRateLimitError(str(exc)) from exc
                    last_err = exc
                    await asyncio.sleep(backoff)
                    backoff *= 2

            raise LLMProviderError(
                f"Gemini error after retries: {last_err!s}"
            ) from last_err
