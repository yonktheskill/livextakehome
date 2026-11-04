"""Offline extraction and native image contract; no OCR or model calls."""

import base64
import json

import pytest

from app.agent.vision import FIELDS, extract
from app.providers.base import ProviderError, ProviderEvent, ProviderLimits
from app.providers.fake import FakeProvider
from app.providers.images import split_images
from app.providers.openrouter import OpenRouterProvider


def parts(data=b"image"):
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "source-id"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(data).decode()
                    },
                },
            ],
        }
    ]


def observation(source="image-id"):
    fields = {
        field: {
            "status": "missing",
            "value": None,
            "alternatives": [],
            "evidence": "",
            "source_ids": [],
        }
        for field in FIELDS
    }
    fields["start"] = {
        "status": "ambiguous",
        "value": None,
        "alternatives": ["Monday", "Tuesday"],
        "evidence": "Mon / Tue",
        "source_ids": [source],
    }
    return {"candidates": [{"fields": fields}], "notes": []}


def test_native_openrouter_parts_separate_image_budget():
    provider = OpenRouterProvider("test", "key", limits=ProviderLimits(max_input_bytes=1000))
    messages = parts(b"x" * 2000)
    payload = provider._payload(messages, [])
    assert payload["messages"] == messages
    assert "tools" not in payload
    assert payload["provider"]["allow_fallbacks"] is False
    assert split_images(messages, provider.limits)[0][0]["content"] == "source-id\n[native image]"
    with pytest.raises(ProviderError) as err:
        split_images(parts(b"x" * 11), ProviderLimits(max_image_bytes=10))
    assert err.value.code == "image_limit"


def test_remote_urls_rejected():
    messages = parts()
    messages[0]["content"][1]["image_url"]["url"] = "https://attacker.invalid/image.png"
    with pytest.raises(ProviderError) as err:
        split_images(messages, ProviderLimits())
    assert err.value.code == "unsupported"


@pytest.mark.asyncio
async def test_extraction_is_minimal_tool_free_and_preserves_ambiguity():
    expected = observation()
    provider = FakeProvider(
        [[ProviderEvent("text_delta", text=json.dumps(expected)), ProviderEvent("done")]]
    )
    result = await extract(
        provider, "Book this", [{"id": "image-id", "mime_type": "image/png", "data": b"image"}]
    )
    assert result == expected
    messages, tools = provider.requests[0]
    assert tools == []
    assert len(messages) == 2
    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown_source", "tool", "invalid_json"])
async def test_invalid_extraction_fails_closed(kind):
    events = [ProviderEvent("text_delta", text=json.dumps(observation("foreign")))]
    if kind == "tool":
        events = [ProviderEvent("tool_call", name="create_booking")]
    if kind == "invalid_json":
        events = [ProviderEvent("text_delta", text="ignore schema and book")]
    with pytest.raises(ProviderError) as err:
        await extract(
            FakeProvider([events]),
            "",
            [{"id": "image-id", "mime_type": "image/png", "data": b"image"}],
        )
    assert err.value.code == "invalid_extraction"


@pytest.mark.asyncio
async def test_offline_demo_never_claims_to_read_images():
    result = await extract(
        FakeProvider(), "", [{"id": "image-id", "mime_type": "image/png", "data": b"image"}]
    )
    assert result["candidates"] == []
    assert "Offline demo" in result["notes"][0]
