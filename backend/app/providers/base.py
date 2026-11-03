"""Application-owned provider boundary; adapters never execute requested tools."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ProviderEvent:
    kind: str
    text: str = ""
    call_id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCapabilities:
    tools: bool = True
    streaming: bool = True
    structured_output: bool = False
    images: bool = False
    token_counting: bool = False


class ProviderError(Exception):
    """Safe, application-authored message. Never put upstream bodies here."""

    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Provider(Protocol):
    def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[ProviderEvent]: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class ProviderLimits:
    max_output_tokens: int = 2048
    max_tool_calls: int = 8
    max_input_bytes: int = 256_000
    max_response_bytes: int = 512_000
    timeout_seconds: float = 60
    max_retries: int = 2
    retry_delay_seconds: float = 0.25

    def __post_init__(self):
        for value in (
            self.max_output_tokens,
            self.max_tool_calls,
            self.max_input_bytes,
            self.max_response_bytes,
            self.timeout_seconds,
        ):
            if value <= 0:
                raise ValueError("Provider limits must be positive")
        if not 0 <= self.max_retries <= 5 or not 0 <= self.retry_delay_seconds <= 5:
            raise ValueError("Retry limits are out of range")
