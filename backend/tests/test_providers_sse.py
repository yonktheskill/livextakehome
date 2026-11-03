"""Malformed streams must never expose a partially validated tool batch."""

import httpx
import pytest

from app.providers import ProviderError, ProviderLimits
from app.providers.openrouter import bounded_lines
from app.providers.sse import Completion, sse_data

TOOLS = [{"type": "function", "function": {"name": "read"}}]


@pytest.mark.parametrize("arguments", ['{"x":NaN}', '{"x":1,"x":2}', '{"x":Infinity}'])
def test_nonstandard_json_rejected(arguments):
    state = Completion(TOOLS, ProviderLimits())
    state.calls[0] = {"id": "a", "name": "read", "arguments": arguments}
    state.finish = "tool_calls"
    state.saw_done = True
    with pytest.raises(ProviderError):
        state.complete()


@pytest.mark.parametrize(
    "calls",
    [
        {
            0: {"id": "same", "name": "read", "arguments": "{}"},
            1: {"id": "same", "name": "read", "arguments": "{}"},
        },
        {0: {"id": "", "name": "read", "arguments": "{}"}},
    ],
)
def test_invalid_call_ids(calls):
    state = Completion(TOOLS, ProviderLimits())
    state.calls = calls
    state.finish = "tool_calls"
    state.saw_done = True
    with pytest.raises(ProviderError):
        state.complete()


def test_call_limit_and_late_output():
    state = Completion(TOOLS, ProviderLimits(max_tool_calls=1))
    with pytest.raises(ProviderError):
        state.feed('{"choices":[{"delta":{"tool_calls":[{"index":1}]}}]}')
    state.feed('{"choices":[{"delta":{},"finish_reason":"stop"}]}')
    with pytest.raises(ProviderError):
        state.feed('{"choices":[{"delta":{"content":"too late"}}]}')


@pytest.mark.asyncio
async def test_multiline_sse_and_metadata():
    async def lines():
        for line in [
            ": comment",
            "id: 5",
            "event: message",
            'data: {"choices":',
            "data: []}",
            "",
            "data: [DONE]",
        ]:
            yield line

    assert [data async for data in sse_data(lines(), 1000)] == ['{"choices":\n[]}', "[DONE]"]


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    async def __aiter__(self):
        for byte in self.raw:
            yield bytes([byte])


@pytest.mark.asyncio
async def test_utf8_across_chunks_and_crlf():
    response = httpx.Response(200, stream=ByteStream("data: café\r\n\r\n".encode()))
    try:
        assert [line async for line in bounded_lines(response, 100)] == ["data: café", ""]

    finally:
        await response.aclose()


@pytest.mark.asyncio
async def test_invalid_utf8():
    response = httpx.Response(200, stream=ByteStream(b"\xff\n"))
    try:
        with pytest.raises(UnicodeError):
            _ = [line async for line in bounded_lines(response, 100)]

    finally:
        await response.aclose()


@pytest.mark.asyncio
async def test_stream_cancellation_closes_response():
    import asyncio

    from app.providers.openrouter import OpenRouterProvider

    class CancelledStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"visible"}}]}\n\n'
            raise asyncio.CancelledError

        async def aclose(self):
            self.closed = True

    stream = CancelledStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        )
    ) as client:
        provider = OpenRouterProvider("vendor/test", "test-key", client=client)
        with pytest.raises(asyncio.CancelledError):
            _ = [event async for event in provider.stream([], [])]
    assert stream.closed


@pytest.mark.asyncio
async def test_input_validation_and_client_ownership():
    from unittest.mock import Mock

    from app.providers.openrouter import OpenRouterProvider

    handler = Mock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(
            "vendor/test", "test-key", client=client, limits=ProviderLimits(max_input_bytes=100)
        )
        with pytest.raises(ProviderError) as error:
            _ = [event async for event in provider.stream([], [])]
        assert error.value.code == "input_limit"
        provider = OpenRouterProvider("vendor/test", "test-key", client=client)
        with pytest.raises(ProviderError) as error:
            _ = [event async for event in provider.stream([{"role": "user", "content": []}], [])]
        assert error.value.code == "unsupported"
        handler.assert_not_called()
        await provider.aclose()
        assert not client.is_closed
        with pytest.raises(ProviderError) as error:
            _ = [event async for event in provider.stream([], [])]
        assert error.value.code == "closed"
