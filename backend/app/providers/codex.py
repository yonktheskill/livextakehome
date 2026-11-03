"""Fail-closed development boundary for the official, optional Python Codex SDK.

No SDK client is constructed: construction/initialization can start a runtime that
reads account/config files. SDK availability is not evidence of safe isolation.
See docs/provider-notes.md for the inspected API and the missing security proof.
"""

from collections.abc import AsyncIterator
from importlib.metadata import PackageNotFoundError, version

from .base import ProviderCapabilities, ProviderError, ProviderEvent


class CodexProvider:
    capabilities = ProviderCapabilities(tools=False, streaming=False)

    def __init__(self, model: str | None = None):
        self.model = model

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[ProviderEvent]:
        try:
            version("openai-codex")
        except PackageNotFoundError:
            raise ProviderError(
                "Optional openai-codex SDK is not installed", code="sdk_unavailable"
            ) from None
        raise ProviderError(
            "Codex development provider is disabled: bounded inference and denial of all "
            "model filesystem/shell tools have not been verified for the installed runtime. "
            "Select OpenRouter explicitly or use the offline fake provider.",
            code="unsafe_runtime",
        )
        yield  # Makes this a protocol-compatible async iterator; deliberately unreachable.

    async def aclose(self) -> None:
        return None
