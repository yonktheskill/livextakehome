"""Validate local image parts and budget transport separately from textual context."""

import base64
import binascii
from copy import deepcopy

from .base import ProviderError, ProviderLimits


def split_images(messages: list[dict], limits: ProviderLimits) -> tuple[list[dict], list[str]]:
    text_messages = deepcopy(messages)
    images = []
    total = 0
    for message in text_messages:
        content = message.get("content")
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list) or message.get("role") != "user":
            raise ProviderError("Invalid image input", code="invalid_input")
        if not content:
            raise ProviderError("Empty content parts", code="unsupported")
        text = []
        for part in content:
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if not isinstance(url, str) or len(url) > 2_800_000:
                    raise ProviderError("Invalid image input", code="invalid_input")
                header, sep, encoded = url.partition(",")
                if not sep or header not in (
                    "data:image/jpeg;base64",
                    "data:image/png;base64",
                    "data:image/webp;base64",
                ):
                    raise ProviderError("Only local static images are accepted", code="unsupported")
                try:
                    size = len(base64.b64decode(encoded, validate=True))
                except (ValueError, binascii.Error):
                    raise ProviderError("Invalid image input", code="invalid_input") from None
                total += size
                if size > limits.max_image_bytes or total > limits.max_images_bytes:
                    raise ProviderError("Provider image budget exceeded", code="image_limit")
                images.append(url)
                text.append("[native image]")
            else:
                raise ProviderError("Unsupported content part", code="unsupported")
        message["content"] = "\n".join(text)
    if len(images) > limits.max_images:
        raise ProviderError("Provider image budget exceeded", code="image_limit")
    return text_messages, images
