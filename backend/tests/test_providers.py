"""Offline contract tests: no database, credentials, runtime, or model calls."""

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from app.providers import ProviderError, ProviderLimits
from app.providers.openrouter import OpenRouterProvider

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_bookings",
            "parameters": {"type": "object", "properties": {"day": {"type": "string"}}},
        },
    }
]
MESSAGES = [{"role": "user", "content": "Show tomorrow's meetings"}]


def frame(delta=None, finish=None, **extra):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}


def wire(*chunks):
    return ": processing\n\n" + "".join(
        "data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n"
        for chunk in chunks
    )


async def collect(provider):
    return [event async for event in provider.stream(MESSAGES, TOOLS)]


def provider_for(handler, **limits):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterProvider(
        "vendor/test-model",
        "test-project-key",
        client=client,
        limits=ProviderLimits(retry_delay_seconds=0, **limits),
    ), client


@pytest.mark.asyncio
async def test_fragments_multiple_calls_usage_and_repeated_finish():
    def handle(request):
        payload = json.loads(request.content)
        assert payload["provider"] == {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        }
        assert payload["max_tokens"] == 111
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=wire(
                frame(
                    {
                        "content": "Checking ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_",
                                "function": {"name": "list_", "arguments": '{"day":'},
                            },
                            {
                                "index": 1,
                                "id": "call_2",
                                "function": {"name": "list_bookings", "arguments": "{}"},
                            },
                        ],
                    }
                ),
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "1",
                                "function": {"name": "bookings", "arguments": '"Friday"}'},
                            }
                        ]
                    }
                ),
                frame(finish="tool_calls"),
                frame(
                    finish="tool_calls",
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 5,
                        "total_tokens": 25,
                        "secret": "must not forward",
                    },
                ),
                "[DONE]",
            ),
        )

    provider, client = provider_for(handle, max_output_tokens=111)
    async with client:
        events = await collect(provider)
    assert [event.kind for event in events] == [
        "text_delta",
        "tool_call",
        "tool_call",
        "usage",
        "done",
    ]
    assert events[1].call_id == "call_1"
    assert events[1].arguments == {"day": "Friday"}
    assert events[2].arguments == {}
    assert events[3].usage == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        [frame(finish="stop")],  # No DONE sentinel.
        [frame(finish="length"), "[DONE]"],
        [{"error": {"message": "secret-upstream-token"}}, "[DONE]"],
        ["not JSON", "[DONE]"],
        [frame({"content": 42}), frame(finish="stop"), "[DONE]"],
        [
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "a",
                            "function": {"name": "list_bookings", "arguments": "[]"},
                        }
                    ]
                },
                "tool_calls",
            ),
            "[DONE]",
        ],
        [
            frame(
                {
                    "tool_calls": [
                        {"index": 0, "id": "a", "function": {"name": "shell", "arguments": "{}"}}
                    ]
                },
                "tool_calls",
            ),
            "[DONE]",
        ],
        [
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "a",
                            "function": {"name": "list_bookings", "arguments": "{"},
                        }
                    ]
                },
                "tool_calls",
            ),
            "[DONE]",
        ],
    ],
)
async def test_invalid_response_has_no_tool_proposals(chunks):
    provider, client = provider_for(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=wire(*chunks)
        )
    )
    events = []
    async with client:
        with pytest.raises(ProviderError) as error:
            async for event in provider.stream(MESSAGES, TOOLS):
                events.append(event)
    assert not any(event.kind in ("tool_call", "done") for event in events)
    assert "secret-upstream-token" not in str(error.value)


@pytest.mark.asyncio
async def test_validate_all_calls_before_emitting_any():
    calls = [
        {"index": i, "id": str(i), "function": {"name": "list_bookings", "arguments": args}}
        for i, args in enumerate(["{}", "incomplete"])
    ]
    provider, client = provider_for(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=wire(frame({"tool_calls": calls}, "tool_calls"), "[DONE]"),
        )
    )
    seen = []
    async with client:
        with pytest.raises(ProviderError):
            async for event in provider.stream(MESSAGES, TOOLS):
                seen.append(event)
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status, attempts", [(429, 3), (503, 3), (401, 1), (402, 1), (302, 1)])
async def test_bounded_http_retries_and_sanitization(status, attempts):
    handler = Mock(return_value=httpx.Response(status, text="secret-upstream-token"))
    provider, client = provider_for(handler)
    async with client:
        with pytest.raises(ProviderError) as error:
            await collect(provider)
    assert handler.call_count == attempts
    assert "secret-upstream-token" not in str(error.value)


@pytest.mark.asyncio
async def test_connection_retry_then_success():
    handler = Mock(
        side_effect=[
            httpx.ConnectError("sensitive URL"),
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=wire(
                    frame({"content": "Ready"}),
                    frame(finish="stop"),
                    {"choices": [], "usage": {"total_tokens": 10}},
                    "[DONE]",
                ),
            ),
        ]
    )
    provider, client = provider_for(handler)
    async with client:
        events = await collect(provider)
    assert handler.call_count == 2
    assert [event.kind for event in events] == ["text_delta", "usage", "done"]


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield wire(frame({"content": "Already visible"})).encode()
        raise httpx.ReadError("private headers")


@pytest.mark.asyncio
async def test_no_replay_after_visible_output():
    handler = Mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
        )
    )
    provider, client = provider_for(handler)
    async with client:
        with pytest.raises(ProviderError) as error:
            await collect(provider)
    assert handler.call_count == 1
    assert error.value.retryable is False
    assert "private headers" not in str(error.value)


@pytest.mark.asyncio
async def test_limits_and_timeout():
    handler = Mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text="x" * 1000
        )
    )
    provider, client = provider_for(handler, max_response_bytes=100)
    async with client:
        with pytest.raises(ProviderError, match="limit"):
            await collect(provider)
    assert handler.call_count == 1

    async def slow(_):
        await asyncio.sleep(1)

    provider, client = provider_for(slow, timeout_seconds=0.01)
    async with client:
        with pytest.raises(ProviderError) as error:
            await collect(provider)
    assert error.value.code == "timeout"


@pytest.mark.asyncio
async def test_cancel_propagates_without_retry():
    async def cancelled(_):
        raise asyncio.CancelledError

    handler = Mock(side_effect=cancelled)
    provider, client = provider_for(handler)
    async with client:
        with pytest.raises(asyncio.CancelledError):
            await collect(provider)
    assert handler.call_count == 1
