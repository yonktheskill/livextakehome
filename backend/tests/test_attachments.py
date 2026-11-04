import io
import struct
import time
import zlib

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from starlette.requests import Request

from app.api.attachments import upload
from app.core.config import Settings
from app.db.attachments import MAX_BYTES, cleanup_abandoned, load_owned, normalize, store
from app.db.models import Attachment, Conversation, Message
from app.main import create_app


def picture(kind="PNG", size=(40, 60), **options):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=kind, **options)
    return output.getvalue()


def test_normalization_removes_metadata_preserves_orientation():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private original metadata"
    normalized, mime, width, height = normalize(picture("JPEG", exif=exif), "image/jpeg")
    assert (mime, width, height) == ("image/jpeg", 60, 40)
    assert b"private original metadata" not in normalized
    with Image.open(io.BytesIO(normalized)) as decoded:
        assert not decoded.getexif()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "private comment")
    normalized, *_ = normalize(picture(pnginfo=info), "image/png")
    assert b"private comment" not in normalized


@pytest.mark.parametrize(
    ("data", "mime", "status"),
    [
        (b"<svg></svg>", "image/svg+xml", 415),
        (b"not an image", "image/png", 422),
        (picture(), "image/jpeg", 415),
        (picture("GIF"), "image/png", 415),
        (b"x" * (MAX_BYTES + 1), "image/png", 413),
        (picture(size=(4097, 1)), "image/png", 422),
        (picture(size=(3000, 3000)), "image/png", 422),
    ],
)
def test_rejects_invalid_inputs(data, mime, status):
    with pytest.raises(HTTPException) as error:
        normalize(data, mime)
    assert error.value.status_code == status


def test_rejects_animation():
    output = io.BytesIO()
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    frames[0].save(output, format="PNG", save_all=True, append_images=frames[1:], duration=50)
    with pytest.raises(HTTPException, match="Animated"):
        normalize(output.getvalue(), "image/png")


def test_dimension_rejection_precedes_pixel_decode(monkeypatch):
    # A valid PNG header advertises a huge image without supplying pixel data.
    data = bytearray(picture())
    data[16:24] = struct.pack(">II", 65535, 65535)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    monkeypatch.setattr(Image.Image, "load", lambda *_: pytest.fail("must not decode pixels"))
    with pytest.raises(HTTPException):
        normalize(bytes(data), "image/png")


def test_expiry_ownership_and_sent_lifetime(account):
    owner, _ = account
    first = store(owner.id, picture(), "image/png")
    attachment = Attachment.get_by_id(first["id"])
    attachment.created_at = time.time() - 86401
    attachment.save()
    with pytest.raises(HTTPException):
        load_owned(attachment.id, owner.id)
    assert cleanup_abandoned() == 1
    sent = store(owner.id, picture(), "image/png")
    message = Message.create(
        conversation=Conversation.get(Conversation.user == owner), role="user", content=""
    )
    Attachment.update(message=message, created_at=1).where(Attachment.id == sent["id"]).execute()
    assert cleanup_abandoned() == 0
    assert load_owned(sent["id"], owner.id).id == sent["id"]
    with pytest.raises(HTTPException):
        load_owned(sent["id"], owner.id + 1)
    message.delete_instance()
    assert not Attachment.select().exists()


def test_authenticated_upload_private_download(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "images.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/chat/attachments", content=picture()).status_code == 401
        client.post(
            "/api/auth/register", json={"email": "one@example.com", "password": "long-password-123"}
        )
        response = client.post(
            "/api/chat/attachments", content=picture(), headers={"Content-Type": "image/png"}
        )
        assert response.status_code == 200
        image = response.json()
        assert set(image) == {"id", "mime_type", "width", "height", "size_bytes"}
        downloaded = client.get(f"/api/chat/attachments/{image['id']}")
        assert downloaded.status_code == 200
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        with Image.open(io.BytesIO(downloaded.content)) as decoded:
            assert decoded.size == (40, 60)
        assert (
            client.post(
                "/api/chat/attachments",
                content=b"x",
                headers={"Content-Type": "image/png", "Content-Length": str(MAX_BYTES + 1)},
            ).status_code
            == 413
        )
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/register", json={"email": "two@example.com", "password": "long-password-123"}
        )
        assert client.get(f"/api/chat/attachments/{image['id']}").status_code == 404


@pytest.mark.asyncio
async def test_stream_bound_ignores_misleading_content_length(monkeypatch):
    import app.api.attachments as endpoint

    class User:
        id = 1

    async def current(_):
        return User()

    monkeypatch.setattr(endpoint, "current", current)
    consumed = 0

    async def receive():
        nonlocal consumed
        consumed += 1
        return {"type": "http.request", "body": b"x" * 600_000, "more_body": True}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat/attachments",
            "headers": [(b"content-type", b"image/png"), (b"content-length", b"1")],
        },
        receive,
    )
    with pytest.raises(HTTPException) as error:
        await upload(request)
    assert error.value.status_code == 413
    assert consumed == 4
