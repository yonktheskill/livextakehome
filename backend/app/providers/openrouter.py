"""HTTPX Chat Completions adapter. Only the application executes tool proposals."""

import asyncio
import codecs
import json
from collections.abc import AsyncIterator

import httpx

from .base import ProviderCapabilities, ProviderError, ProviderEvent, ProviderLimits
from .images import split_images
from .sse import Completion, invalid, sse_data


async def bounded_lines(response: httpx.Response, limit: int) -> AsyncIterator[str]:
    """Bound bytes before line buffering, including a malicious unterminated line."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    pending = ""
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ProviderError("Provider response exceeded its limit", code="response_limit")
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line.removesuffix("\r")
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending.removesuffix("\r")


class OpenRouterProvider:
    capabilities = ProviderCapabilities(images=True)

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        limits: ProviderLimits | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if not model or not model.strip() or not api_key or not api_key.strip():
            raise ProviderError(
                "OpenRouter requires a model and project API key", code="configuration"
            )
        self.model = model
        self._api_key = api_key
        self.limits = limits or ProviderLimits()
        self._owns_client = client is None
        # Ignore ambient proxy credentials and never follow credential-bearing redirects.
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self._closed = False

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[ProviderEvent]:
        if self._closed:
            raise ProviderError("Provider is closed", code="closed")
        payload = self._payload(messages, tools)
        emitted = False
        attempt = 0
        phase = "connect"
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                for attempt in range(self.limits.max_retries + 1):
                    phase = "connect"
                    try:
                        completion = Completion(tools, self.limits)
                        async with self._client.stream(
                            "POST",
                            "https://openrouter.ai/api/v1/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self._api_key}",
                                "Accept": "text/event-stream",
                            },
                            json=payload,
                            timeout=self.limits.timeout_seconds,
                            follow_redirects=False,
                        ) as response:
                            if response.status_code != 200:
                                retryable = response.status_code in (429, 500, 502, 503, 504)
                                raise ProviderError(
                                    "OpenRouter request was rejected",
                                    code="http_error",
                                    retryable=retryable,
                                    diagnostics={
                                        "phase": "http_response",
                                        "http_status": response.status_code,
                                        "attempt": attempt + 1,
                                    },
                                )
                            if not response.headers.get("content-type", "").startswith(
                                "text/event-stream"
                            ):
                                raise invalid()
                            phase = "stream"
                            lines = bounded_lines(response, self.limits.max_response_bytes)
                            async for data in sse_data(lines, self.limits.max_response_bytes):
                                for event in completion.feed(data):
                                    emitted = True
                                    yield event
                                if completion.saw_done:
                                    break
                            for event in completion.complete():
                                emitted = True
                                yield event
                        return
                    except (httpx.TransportError, ProviderError) as error:
                        retryable = isinstance(error, httpx.TransportError) or error.retryable
                        if emitted or not retryable or attempt == self.limits.max_retries:
                            if isinstance(error, ProviderError):
                                raise ProviderError(
                                    str(error),
                                    code=error.code,
                                    retryable=retryable and not emitted,
                                    diagnostics={
                                        "phase": phase,
                                        "attempt": attempt + 1,
                                        **error.diagnostics,
                                    },
                                ) from None
                            raise ProviderError(
                                "OpenRouter connection failed",
                                code="transport",
                                retryable=not emitted,
                                diagnostics={
                                    "phase": phase,
                                    "attempt": attempt + 1,
                                    "cause_class": type(error).__name__,
                                },
                            ) from None
                        await asyncio.sleep(self.limits.retry_delay_seconds * 2**attempt)
        except TimeoutError:
            raise ProviderError(
                "Provider request timed out",
                code="timeout",
                retryable=not emitted,
                diagnostics={"phase": phase, "attempt": attempt + 1},
            ) from None
        except (UnicodeError, ValueError, TypeError, KeyError, AttributeError):
            raise invalid() from None

    def _payload(self, messages: list[dict], tools: list[dict]) -> dict:
        try:
            text_messages, images = split_images(messages, self.limits)
            for tool in tools:
                if tool.get("type") != "function" or not isinstance(tool["function"]["name"], str):
                    raise invalid()
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "max_tokens": self.limits.max_output_tokens,
                "stream_options": {"include_usage": True},
                "provider": {
                    "require_parameters": True,
                    "allow_fallbacks": False,
                    "data_collection": "deny",
                },
            }
            if tools:
                payload.update(tools=tools, tool_choice="auto")
            budget_payload = {**payload, "messages": text_messages}
            if (
                len(json.dumps(budget_payload, allow_nan=False).encode())
                > self.limits.max_input_bytes
            ):
                raise ProviderError("Provider input exceeded its limit", code="input_limit")
            return payload
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError("Invalid provider input", code="invalid_input") from None

    async def aclose(self) -> None:
        self._closed = True
        if self._owns_client:
            await self._client.aclose()
