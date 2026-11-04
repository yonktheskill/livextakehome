from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import current
from app.calcom.client import CalClient, CalError
from app.core.security import digest
from app.db.database import query
from app.db.models import Action, Connection, Conversation, Run, User, db

router = APIRouter(prefix="/api/connection")


class KeyInput(BaseModel):
    api_key: str = Field(min_length=8, max_length=1024)


def status(user_id):
    conn = Connection.get_or_none(Connection.user == user_id)
    return {
        "connected": bool(conn and conn.ciphertext),
        "label": conn.label if conn and conn.ciphertext else None,
    }


def change(user_id, identity=None, ciphertext="", label=""):
    with db.atomic("IMMEDIATE"):
        conv = Conversation.get(Conversation.user == user_id)
        unresolved = (
            Action.select()
            .join(Run)
            .where((Run.conversation == conv) & (Action.state.in_(["dispatching", "unknown"])))
            .exists()
        )
        if unresolved:
            raise HTTPException(409, "A committed action needs reconciliation before key changes.")
        conn, _ = Connection.get_or_create(user=user_id)
        if identity and conn.owner and conn.owner != identity["id"]:
            raise HTTPException(
                409, "Different Cal.com owner. Disconnect before explicitly relinking."
            )
        # A committed request retains evidence; disconnect does not recall it.
        conv.revision += 1
        conv.save()
        Run.update(interrupted=True).where(
            (Run.conversation == conv) & (Run.status.in_(["queued", "running"]))
        ).execute()
        Action.update(state="superseded").where(
            (Action.run.in_(Run.select(Run.id).where(Run.conversation == conv)))
            & (Action.state.in_(["prepared", "authorized", "awaiting_confirmation"]))
        ).execute()
        conn.generation += 1
        conn.ciphertext, conn.label = ciphertext, label
        if identity:
            conn.owner, conn.username = identity["id"], identity["username"]
            User.update(time_zone=identity.get("timeZone") or "UTC").where(
                (User.id == user_id) & (User.time_zone_manual == False)  # noqa: E712
            ).execute()
        else:
            conn.owner, conn.username = 0, ""
        conn.save()
        return status(user_id)


@router.get("")
async def get_connection(request: Request):
    user = await current(request)
    return await query(status, user.id)


@router.put("")
async def connect(data: KeyInput, request: Request):
    user = await current(request)
    client = CalClient(data.api_key)
    try:
        identity = await client.request("GET", "/v2/me")
        if not isinstance(identity.get("id"), int) or not identity.get("username"):
            raise CalError("Cal.com did not return a valid owner identity.")
    except CalError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        await client.close()
    encrypted = request.app.state.vault.seal(data.api_key, user.id, identity["id"])
    return await query(
        change, user.id, identity, encrypted, "Cal.com • " + digest(data.api_key)[:8]
    )


@router.delete("", status_code=204)
async def disconnect(request: Request):
    user = await current(request)
    await query(change, user.id)
    # DELETE then PUT is the explicit account-relink flow.
