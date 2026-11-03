"""Bounded SSE framing and Chat Completions assembly, independent of HTTP."""

import json
from collections.abc import AsyncIterator

from .base import ProviderError, ProviderEvent, ProviderLimits


def invalid() -> ProviderError:
    return ProviderError("Provider returned an invalid response", code="invalid_response")


def strict_json(data: str):
    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_object)


async def sse_data(lines: AsyncIterator[str], limit: int) -> AsyncIterator[str]:
    data: list[str] = []
    size = 0
    async for line in lines:
        size += len(line.encode("utf-8"))
        if size > limit:
            raise ProviderError("Provider response exceeded its limit", code="response_limit")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        # SSE comments, event, retry, and id fields carry no completion content.
    if data:
        yield "\n".join(data)


class Completion:
    """Delay every tool proposal until the entire response succeeds."""

    def __init__(self, tools: list[dict], limits: ProviderLimits):
        self.limits = limits
        self.names = {tool["function"]["name"] for tool in tools}
        self.calls: dict[int, dict] = {}
        self.finish: str | None = None
        self.usage: dict = {}
        self.saw_done = False

    def feed(self, data: str) -> list[ProviderEvent]:
        if data == "[DONE]":
            self.saw_done = True
            return []
        try:
            chunk = strict_json(data)
            if not isinstance(chunk, dict):
                raise invalid()
            if "error" in chunk:
                raise ProviderError("Provider generation failed", code="upstream_error")
            usage = chunk.get("usage")
            if usage is not None:
                if not isinstance(usage, dict):
                    raise invalid()
                # Do not forward arbitrary upstream metadata or error strings.
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if value is not None:
                        if type(value) is not int or value < 0:
                            raise invalid()
                        self.usage[key] = value
            events = []
            for choice in chunk.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise invalid()
                delta = choice.get("delta") or {}
                text = delta.get("content")
                fragments = delta.get("tool_calls") or []
                if self.finish and (text or fragments):
                    raise invalid()
                if text is not None:
                    if not isinstance(text, str):
                        raise invalid()
                    if text:
                        events.append(ProviderEvent("text_delta", text=text))
                for part in fragments:
                    self._fragment(part)
                reason = choice.get("finish_reason")
                if reason:
                    if reason not in ("stop", "tool_calls"):
                        raise ProviderError(
                            "Provider did not finish successfully", code="incomplete"
                        )
                    if self.finish and reason != self.finish:
                        raise invalid()
                    self.finish = reason
            return events
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise invalid() from None

    def _fragment(self, part: dict) -> None:
        index = part["index"]
        if type(index) is not int or not 0 <= index < self.limits.max_tool_calls:
            raise invalid()
        call = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if part.get("type", "function") != "function":
            raise invalid()
        function = part.get("function") or {}
        for field, value in (
            ("id", part.get("id")),
            ("name", function.get("name")),
            ("arguments", function.get("arguments")),
        ):
            if value is not None:
                if not isinstance(value, str):
                    raise invalid()
                call[field] += value

    def complete(self) -> list[ProviderEvent]:
        if not self.saw_done or not self.finish:
            raise ProviderError("Provider stream ended prematurely", code="incomplete")
        if bool(self.calls) != (self.finish == "tool_calls"):
            raise invalid()
        events = []
        seen = set()
        # Validate ALL proposals before yielding any; the executor validates schemas.
        for index in sorted(self.calls):
            call = self.calls[index]
            try:
                arguments = strict_json(call["arguments"])
            except (ValueError, TypeError, RecursionError):
                raise invalid() from None
            if (
                not isinstance(arguments, dict)
                or not call["id"]
                or call["id"] in seen
                or call["name"] not in self.names
            ):
                raise invalid()
            seen.add(call["id"])
            events.append(
                ProviderEvent(
                    "tool_call", call_id=call["id"], name=call["name"], arguments=arguments
                )
            )
        if self.usage:
            events.append(ProviderEvent("usage", usage=self.usage))
        events.append(ProviderEvent("done"))
        return events
