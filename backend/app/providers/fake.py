"""Deterministic offline fixture, selected explicitly; no scheduling claims."""

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy

from .base import ProviderCapabilities, ProviderError, ProviderEvent


class FakeProvider:
    capabilities = ProviderCapabilities()

    def __init__(self, steps: Sequence[Sequence[ProviderEvent]] | None = None):
        self._steps = deepcopy(list(steps)) if steps is not None else None
        self.requests: list[tuple[list[dict], list[dict]]] = []
        self._closed = False

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[ProviderEvent]:
        if self._closed:
            raise ProviderError("Provider is closed", code="closed")
        index = len(self.requests)
        self.requests.append(deepcopy((messages, tools)))
        if self._steps is None:
            yield ProviderEvent(
                "text_delta", text="Offline demo: no calendar actions were executed."
            )
            yield ProviderEvent("done")
        elif index < len(self._steps):
            for event in deepcopy(self._steps[index]):
                yield event
        else:
            raise ProviderError("Fake provider script exhausted", code="fixture_exhausted")

    async def aclose(self) -> None:
        self._closed = True
