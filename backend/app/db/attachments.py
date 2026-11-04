"""Small private image storage boundary. TODO: replace SQLite BLOBs with object storage."""

import hashlib
import io
import time
import warnings

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from app.db.models import Attachment

MAX_BYTES = 2 * 1024 * 1024
MAX_EDGE = 4096
MAX_PIXELS = 8_000_000
MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def metadata(attachment):
    return {
        "id": attachment.id,
        "mime_type": attachment.mime_type,
        "width": attachment.width,
        "height": attachment.height,
        "size_bytes": attachment.size_bytes,
    }


def normalize(data: bytes, declared_mime: str):
    """Inspect dimensions before pixel allocation, then discard all original metadata."""
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "Each processed image must be at most 2 MiB.")
    if declared_mime not in MIMES.values():
        raise HTTPException(415, "Use a static JPEG, PNG or WebP image.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                actual_mime = MIMES.get(source.format or "")
                if actual_mime != declared_mime:
                    raise HTTPException(415, "Image contents do not match the declared image type.")
                width, height = source.size
                if (
                    min(width, height) < 1
                    or max(width, height) > MAX_EDGE
                    or width * height > MAX_PIXELS
                ):
                    raise HTTPException(
                        422,
                        "Image exceeds 4096 pixels per edge or 8 megapixels. Crop or split it.",
                    )
                if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) != 1:
                    raise HTTPException(415, "Animated images are not supported.")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                # Building a new pixel image prevents EXIF, comments, ICC and XMP copying.
                mode = "RGB" if actual_mime == "image/jpeg" else "RGBA"
                clean = Image.new(mode, oriented.size)
                clean.paste(oriented.convert(mode))
                output = io.BytesIO()
                format_name = {value: key for key, value in MIMES.items()}[actual_mime]
                options = {"quality": 95} if format_name in {"JPEG", "WEBP"} else {}
                clean.save(output, format=format_name, **options)
                normalized = output.getvalue()
                if len(normalized) > MAX_BYTES:
                    raise HTTPException(
                        413,
                        "Normalized image exceeds 2 MiB. Crop or split it to retain readability.",
                    )
                return normalized, actual_mime, clean.width, clean.height
    except HTTPException:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HTTPException(
            422, "Image is invalid, damaged or too large to decode safely."
        ) from None


def cleanup_abandoned(now=None):
    return (
        Attachment.delete()
        .where(
            Attachment.message.is_null()
            & (Attachment.created_at < (time.time() if now is None else now) - 86400)
        )
        .execute()
    )


def store(owner_id, data, declared_mime):
    normalized, mime, width, height = normalize(data, declared_mime)
    cleanup_abandoned()
    return metadata(
        Attachment.create(
            owner=owner_id,
            data=normalized,
            mime_type=mime,
            width=width,
            height=height,
            size_bytes=len(normalized),
            sha256=hashlib.sha256(normalized).hexdigest(),
        )
    )


def load_owned(attachment_id, owner_id):
    row = Attachment.get_or_none((Attachment.id == attachment_id) & (Attachment.owner == owner_id))
    if not row or (row.message_id is None and row.created_at < time.time() - 86400):
        raise HTTPException(404, "Image not found.")
    return row
