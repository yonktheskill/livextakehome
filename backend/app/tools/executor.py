import json
import re
import time
from datetime import datetime

from pydantic import ValidationError

from app.calcom.client import CalClient, CalError
from app.core.security import digest
from app.db.database import query
from app.db.models import Action, Confirmation, Connection, Run, db
from app.db.repository import claim, emit
from app.tools.planning import duration, get_booking, get_type, prepare, slots
from app.tools.schemas import SCHEMAS


class Waiting(Exception):
    pass


class Interrupted(Exception):
    pass


class UnknownOutcome(Exception):
    pass


class Executor:
    def __init__(self, vault, client_factory=CalClient):
        self.vault = vault
        self.client_factory = client_factory

    async def execute(self, run_id, name, arguments):
        if name not in SCHEMAS:
            return {"error": "Unknown tool"}
        try:
            args = SCHEMAS[name].model_validate(arguments)
        except ValidationError:
            return {
                "error": "Invalid arguments; use the exact schema and complete required fields."
            }
        run, conn = await query(self.connection, run_id)
        if not conn or not conn.ciphertext:
            return {"error": "Connect your Cal.com account in settings first."}
        client = self.client_factory(self.vault.open(conn.ciphertext, conn.user_id, conn.owner))
        try:
            if name in {
                "list_bookings",
                "get_booking",
                "list_event_types",
                "get_event_type",
                "get_available_slots",
            }:
                return await self.read(client, name, args, conn)
            encoded = json.dumps(args.model_dump(mode="json"), sort_keys=True)
            cached = await query(
                lambda: Action.get_or_none(
                    (Action.run == run_id)
                    & (Action.kind == name)
                    & (Action.inputs == encoded)
                    & (Action.state == "succeeded")
                )
            )
            if cached:
                return json.loads(cached.result)
            path, payload, details = await prepare(client, name, args, conn.owner)
            fingerprint = digest(json.dumps(details, sort_keys=True))
            explicit = (
                await query(self.explicit_creation, run_id, args)
                if name == "create_event_type"
                else False
            )
            confirmation_required = name == "cancel_booking" or (
                name == "create_event_type" and not explicit
            )
            action_id, waiting = await query(
                self.prepare_action, run, conn, name, encoded, fingerprint, confirmation_required
            )
            if waiting:
                prompt = (
                    "Confirm cancellation of this exact booking"
                    if name == "cancel_booking"
                    else "Confirm creating this reusable event type"
                )
                text = (
                    f"{prompt}: {json.dumps(details)}. Reply yes to confirm, or describe a change."
                )
                await query(emit, run_id, "tool.status", {"text": text})
                raise Waiting(text)
            if not await query(claim, action_id):
                raise Interrupted()
            try:
                result = await client.request("POST", path, payload=payload)
                identity_field = "id" if name == "create_event_type" else "uid"
                if not isinstance(result, dict) or not result.get(identity_field):
                    raise CalError(
                        "Cal.com accepted a request without a verifiable resource ID.",
                        uncertain=True,
                    )
            except CalError as exc:
                await query(
                    self.record,
                    action_id,
                    "unknown" if exc.uncertain else "failed",
                    {"error": str(exc)},
                )
                if exc.uncertain:
                    raise UnknownOutcome() from None
                return {"error": str(exc)}
            outcome = {"response": result, "validated": details}
            await query(self.record, action_id, "succeeded", outcome)
            await query(
                emit,
                run_id,
                "action.result",
                {
                    "action_id": action_id,
                    "kind": name,
                    "summary": "Cal.com accepted the request; see returned booking status.",
                    "details": outcome,
                },
            )
            return outcome
        except CalError as exc:
            return {"error": str(exc)}
        finally:
            await client.close()

    @staticmethod
    def explicit_creation(run_id, args):
        # A narrow deterministic subset of unambiguous direct creation requests.
        # Any qualifiers/negation/compound requests fall back to exact confirmation.
        text = Run.get_by_id(run_id).message.content.strip().lower().rstrip(".!")
        match = re.fullmatch(
            r"(?:please )?(?:create|set up) (?:a |an )?(\d+)[ -]?(?:minute|min)[ -]"
            r"(.+?) (?:event type|type)",
            text,
        )
        if not match or args.description:
            return False

        def normalize(value):
            return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

        return int(match[1]) == args.duration_minutes and normalize(match[2]) == normalize(
            args.title
        )

    @staticmethod
    def connection(run_id):
        run = Run.get_by_id(run_id)
        conn = Connection.get_or_none(Connection.user == run.conversation.user)
        return run, conn

    @staticmethod
    def prepare_action(run, conn, name, encoded, fingerprint, confirmation_required):
        with db.atomic("IMMEDIATE"):
            existing = Action.get_or_none(
                (Action.run == run.id)
                & (Action.kind == name)
                & (Action.inputs == encoded)
                & (Action.state == "authorized")
            )
            if existing:
                if existing.fingerprint != fingerprint:
                    existing.state = "superseded"
                    existing.save()
                else:
                    return existing.id, False
            action = Action.create(
                run=run.id,
                revision=run.revision,
                generation=conn.generation,
                kind=name,
                inputs=encoded,
                fingerprint=fingerprint,
                state="awaiting_confirmation" if confirmation_required else "prepared",
            )
            if confirmation_required:
                Confirmation.create(
                    conversation=run.conversation_id, action=action, expires=time.time() + 600
                )
            return action.id, confirmation_required

    @staticmethod
    def record(action_id, state, result):
        Action.update(state=state, result=json.dumps(result)).where(
            Action.id == action_id
        ).execute()

    @staticmethod
    async def read(client, name, args, conn):
        if name == "get_booking":
            return await get_booking(client, args.booking_uid, conn.owner)
        if name == "get_event_type":
            return await get_type(client, args.event_type_id, conn.owner)
        if name == "list_event_types":
            result = await client.request(
                "GET", "/v2/event-types", params={"username": conn.username}
            )
            # Read endpoint is owner-filtered, individual mutations additionally verify ownership.
            return result
        if name == "get_available_slots":
            event_type = await get_type(client, args.event_type_id, conn.owner)
            minutes = duration(event_type, args.duration_minutes)
            if args.booking_uid:
                await get_booking(client, args.booking_uid, conn.owner)
            result = await slots(
                client, args.event_type_id, args.start, args.end, minutes, args.booking_uid
            )
            return {"slots": result, "effective_duration_minutes": minutes}
        results: list[dict] = []
        cursor = None
        for _ in range(5):
            # API's afterStart/beforeEnd exclude crossing appointments. Walk bounded
            # owner/status pages and filter interval overlap locally instead.
            params = {"limit": 100}
            if args.status:
                params["status"] = args.status
            if cursor:
                params["cursor"] = cursor
            page = await client.request("GET", "/v2/bookings", params=params)
            rows = page if isinstance(page, list) else page.get("bookings", [])
            results.extend(
                b
                for b in rows
                if any(str(h.get("id")) == str(conn.owner) for h in b.get("hosts", []))
            )
            cursor = (
                page.get("pagination", {}).get("nextCursor") if isinstance(page, dict) else None
            )
            if not cursor:
                break

        def overlaps(booking):
            try:
                start = datetime.fromisoformat(booking["start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(booking["end"].replace("Z", "+00:00"))
                return start < args.end and end > args.start
            except (KeyError, ValueError, TypeError):
                return False

        return {"bookings": [b for b in results if overlaps(b)], "truncated": bool(cursor)}
