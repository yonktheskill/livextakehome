"""Authenticated, bounded image transport. No filenames or original image copies."""

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.auth import current
from app.db.attachments import MAX_BYTES, MIMES, load_owned, store
from app.db.database import query

router = APIRouter(prefix="/api/chat/attachments")


@router.post("")
async def upload(request: Request):
    user = await current(request)
    mime = request.headers.get("content-type", "").split(";", 1)[0].lower().strip()
    if mime not in MIMES.values():
        raise HTTPException(415, "Use a static JPEG, PNG or WebP image.")
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) < 0 or int(length) > MAX_BYTES:
                raise HTTPException(413, "Each processed image must be at most 2 MiB.")
        except ValueError:
            raise HTTPException(400, "Invalid content length.") from None
    # Never trust Content-Length; count every streamed chunk before retaining it.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "Each processed image must be at most 2 MiB.")
        body.extend(chunk)
    return await query(store, user.id, bytes(body), mime)


@router.get("/{attachment_id}")
async def image(attachment_id: str, request: Request):
    user = await current(request)
    row = await query(load_owned, attachment_id, user.id)
    return Response(
        bytes(row.data),
        media_type=row.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
