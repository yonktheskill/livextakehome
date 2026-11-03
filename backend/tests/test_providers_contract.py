"""Provider selection, offline fixtures, and optional SDK inspection only."""

import inspect
from dataclasses import FrozenInstanceError

import pytest

from app.providers import ProviderError, ProviderEvent, ProviderLimits, create_provider
from app.providers.fake import FakeProvider


async def collect(provider):
    return [event async for event in provider.stream([], [])]


@pytest.mark.asyncio
async def test_fake_script_and_closed_state():
    steps = [
        [
            ProviderEvent("tool_call", call_id="test", name="list_bookings", arguments={}),
            ProviderEvent("done"),
        ],
        [ProviderEvent("text_delta", text="No meetings"), ProviderEvent("done")],
    ]
    provider = FakeProvider(steps)
    assert await collect(provider) == steps[0]
    assert await collect(provider) == steps[1]
    with pytest.raises(ProviderError, match="exhausted"):
        await collect(provider)
    await provider.aclose()
    with pytest.raises(ProviderError, match="closed"):
        await collect(provider)
    assert (await collect(create_provider("fake")))[-1].kind == "done"


def test_contract_and_factory():
    event = ProviderEvent("done")
    with pytest.raises(FrozenInstanceError):
        event.kind = "text_delta"
    assert event.arguments is not ProviderEvent("done").arguments
    for name in ("unknown", "openrouter"):
        with pytest.raises(ProviderError):
            create_provider(name)
    with pytest.raises(ProviderError):
        create_provider("codex", api_key="must-not-use")
    with pytest.raises(ValueError):
        ProviderLimits(max_retries=99)


@pytest.mark.asyncio
async def test_codex_fail_closed_without_import_or_process(monkeypatch):
    import app.providers.codex as adapter

    monkeypatch.setattr(adapter, "version", lambda _: "0.163.0")
    with pytest.raises(ProviderError) as error:
        await collect(create_provider("codex"))
    assert error.value.code == "unsafe_runtime"

    def missing(_):
        raise adapter.PackageNotFoundError

    monkeypatch.setattr(adapter, "version", missing)
    with pytest.raises(ProviderError) as error:
        await collect(create_provider("codex"))
    assert error.value.code == "sdk_unavailable"


def test_optional_official_sdk_api_inspection_only():
    sdk = pytest.importorskip("openai_codex")
    # Inspect only; never construct AsyncCodex or access authentication/config.
    assert "env" in inspect.signature(sdk.CodexConfig).parameters
    assert "ephemeral" in inspect.signature(sdk.AsyncCodex.thread_start).parameters
    run = inspect.signature(sdk.AsyncThread.run).parameters
    assert "output_schema" in run
    assert "max_output_tokens" not in run
    assert "max_steps" not in run
    assert "tools" not in run
