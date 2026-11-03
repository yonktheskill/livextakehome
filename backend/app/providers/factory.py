"""Explicit provider selection; no environment reads or automatic fallbacks."""

from .base import Provider, ProviderError


def create_provider(name: str, model: str | None = None, api_key: str | None = None) -> Provider:
    if name == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    if name == "openrouter":
        from .openrouter import OpenRouterProvider

        return OpenRouterProvider(model=model or "", api_key=api_key or "")
    if name == "codex":
        from .codex import CodexProvider

        if api_key is not None:
            raise ProviderError(
                "Codex development provider does not accept API keys", code="configuration"
            )
        return CodexProvider(model=model)
    raise ProviderError("Unknown provider name", code="configuration")
