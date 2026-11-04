import json
import re
from datetime import datetime
from functools import partial

from pydantic import ValidationError

from app.calcom.client import VERSIONS, CalClient, CalError
from app.core.guardrails import WITHHELD, ContentRejected, require_content
from app.core.security import digest
from app.db.database import query
from app.db.dispatch import StalePreparation, prepare_action, settle
from app.db.models import Action, Connection, Message, Run, User, db
from app.db.repository import checkpoint, claim
from app.db.requests import RequestConflict, execute_request_tool, task_write_plan
from app.tools.authorization import authorization_evidence, authorize_action
from app.tools.durations import duration
from app.tools.messages import confirmation_message
from app.tools.planning import get_booking, get_type, plan_booking_duration, prepare, slots
from app.tools.schemas import SCHEMAS, SavePreferredName, WriteArguments


class Waiting(Exception):
    pass


class Interrupted(Exception):
    pass


class UnknownOutcome(Exception):
    pass


class Executor:
    def __init__(self, vault, client_factory=None, *, settings=None):
        self.vault = vault
        self.client_factory = (
            client_factory if client_factory is not None else partial(CalClient, settings=settings)
        )

    async def execute(self, run_id, name, arguments):
        if name == "authorize_action":
            try:
                return await query(authorize_action, run_id, arguments)
            except RequestConflict as exc:
                return {"error": str(exc), "code": "request_conflict"}
        if name in {"request_context", "resolve_requests"}:
            try:
                return await query(execute_request_tool, run_id, name, arguments)
            except RequestConflict as exc:
                return {"error": str(exc), "code": "request_conflict"}
        if name not in SCHEMAS:
            return {"error": "Unknown tool"}
        try:
            args = SCHEMAS[name].model_validate(arguments)
        except ValidationError as exc:
            # Only schema-owned path components and error codes may leave validation.
            # Pydantic messages/inputs and arbitrary dictionary keys can contain secrets.
            known = {"attendee", "name", "email", "time_zone"} | set(SCHEMAS[name].model_fields)
            issues = [
                {
                    "field": ".".join(
                        str(part) if isinstance(part, int) or part in known else "[field]"
                        for part in error["loc"]
                    )
                    or "arguments",
                    "issue": error["type"],
                }
                for error in exc.errors(include_input=False, include_context=False)[:20]
            ]
            return {"error": "Invalid arguments; correct the listed fields.", "details": issues}
        # Include nested booking fields and names; reject rather than rewrite
        # user identities or text that would be sent to Cal.com.
        require_content(args.model_dump(mode="json"))
        if WITHHELD in json.dumps(args.model_dump(mode="json")):
            raise ContentRejected("withheld_details")
        if isinstance(args, SavePreferredName):
            return await query(
                self.save_preferred_name, run_id, args.preferred_name, args.source_message_id
            )
        request_id = args.request_id if isinstance(args, WriteArguments) else None
        request_revision = args.request_revision if isinstance(args, WriteArguments) else None
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
                "plan_booking_duration",
                "get_available_slots",
            }:
                return await self.read(client, name, args, conn)
            if name in {"create_booking", "reschedule_booking"} and request_id:
                plan = await query(task_write_plan, run_id, request_id, request_revision)
                if name != plan["kind"]:
                    return {"error": f"Use {plan['kind']} for this request.", "request": plan}
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
            require_content(payload)
            require_content(details)
            fingerprint = digest(json.dumps(details, sort_keys=True))
            evidence = await query(authorization_evidence, run_id, name, arguments)
            explicit = bool(evidence) or (
                await query(self.explicit_creation, run_id, args)
                if name == "create_event_type"
                else False
            )
            explicit_cancel = bool(evidence) or (
                await query(self.explicit_cancellation, run_id, args, details)
                if name == "cancel_booking"
                else False
            )
            confirmation_required = (name == "cancel_booking" and not explicit_cancel) or (
                name == "create_event_type" and not explicit
            )
            action_id, waiting = await query(
                self.prepare_action,
                run,
                conn,
                name,
                encoded,
                fingerprint,
                confirmation_required,
                {
                    "method": "POST",
                    "path": path,
                    "payload": payload,
                    "version": VERSIONS[path.split("/")[2]],
                    "validation": details,
                    "request_revision": run.revision,
                    "connection_generation": conn.generation,
                    "user_authorization": evidence,
                },
                request_id if name in {"create_booking", "reschedule_booking"} else None,
                request_revision if name in {"create_booking", "reschedule_booking"} else None,
            )
            if waiting:
                time_zone = await query(lambda: run.conversation.user.time_zone)
                raise Waiting(confirmation_message(name, details, time_zone))
            frozen_action = await query(Action.get_by_id, action_id)
            if frozen_action.state in {"succeeded", "failed"}:
                return json.loads(frozen_action.result)
            if not await query(claim, action_id):
                raise Interrupted()
            try:
                frozen = json.loads(frozen_action.request)
                result = await client.request(
                    frozen["method"],
                    frozen["path"],
                    payload=frozen["payload"],
                    version=frozen["version"],
                )
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
                    {"error": str(exc), "code": exc.code, "diagnostics": exc.diagnostics},
                )
                if exc.uncertain:
                    raise UnknownOutcome() from None
                return {"error": str(exc), "code": exc.code, "diagnostics": exc.diagnostics}
            outcome = {"response": result, "validated": frozen["validation"]}
            await query(self.record, action_id, "succeeded", outcome)
            return outcome
        except StalePreparation:
            raise Interrupted() from None
        except RequestConflict as exc:
            return {"error": str(exc), "code": "request_conflict"}
        except CalError as exc:
            return {"error": str(exc), "code": exc.code, "diagnostics": exc.diagnostics}
        finally:
            await client.close()

    @staticmethod
    def user_source(run, source_message_id=None):
        """Evidence must be an owned original user message, never a tool excerpt."""
        source = Message.get_or_none(Message.id == (source_message_id or run.message_id))
        if (
            source is None
            or source.role != "user"
            or source.conversation_id != run.conversation_id
            or source.created_at > run.message.created_at
        ):
            return None
        return source

    @staticmethod
    def identities(content):
        # Bound the asserted name at a sentence, comma, or a following task clause.
        # Quoted/reported text and negation cannot provide deterministic evidence.
        if re.search(r"\b(?:not|never|don't|do not|or)\b", content, re.I) or any(
            c in content for c in ('"', "“", "”", "?", ">")
        ):
            return []
        return [
            " ".join(m.group(1).split())
            for m in re.finditer(
                r"(?:^|[;.!?,]\s*|\band\s+)(?:(?:hello|hi|actually)[, ]+)?(?:please )?"
                r"(?:call me|my name is|i am|i’m|i'm)\s+(.+?)"
                r"(?=[;.!?,]|\s+and\s+(?:book|schedule|remember|save)\b|$)",
                content,
                re.I,
            )
        ]

    @staticmethod
    def save_preferred_name(run_id, preferred_name, source_message_id=None):
        with db.atomic("IMMEDIATE"):
            if not checkpoint(run_id):
                raise Interrupted()
            run = Run.get_by_id(run_id)
            source = Executor.user_source(run, source_message_id)
            identities = Executor.identities(source.content) if source else []
            if len(identities) != 1 or identities[0].casefold() != preferred_name.casefold():
                return {
                    "error": "Supply owned original user self-identification; clarify ambiguity "
                    "or negation. Never infer the user's name from another attendee."
                }
            # An older name must not override a newer explicit self-identification.
            later = Message.select().where(
                (Message.conversation == run.conversation_id)
                & (Message.role == "user")
                & (Message.created_at > source.created_at)
                & (Message.created_at <= run.message.created_at)
            )
            for message in later:
                names = Executor.identities(message.content)
                if names and names != identities:
                    return {"error": "A later self-identification conflicts; use the latest name."}
            User.update(preferred_name=preferred_name).where(
                User.id == run.conversation.user_id
            ).execute()
            return {"saved": True, "preferred_name": preferred_name}

    @staticmethod
    def authorization_clauses(run_id):
        run = Run.get_by_id(run_id)
        source = Executor.user_source(run)
        if not source:
            return []
        # Conservative local recognition is a fallback until durable request evidence
        # is wired by the parent. Unrecognized wording asks focused confirmation.
        text = source.content
        if any(c in text for c in ('"', "“", "”", "?", ">")) or re.search(
            r"\b(?:not|never|don't|do not|if|unless|maybe|perhaps|pretend|example)\b", text, re.I
        ):
            return []
        return [c.strip() for c in re.split(r"[;.!] |[;!]|\band then\b|\band\b", text, flags=re.I)]

    @staticmethod
    def explicit_creation(run_id, args):
        def normalize(value):
            return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

        for clause in Executor.authorization_clauses(run_id):
            match = re.fullmatch(
                r"(?:please )?(?:create|set up|make) (?:a |an )?(\d+)[ -]?(?:minute|min)[ -]"
                r"(.+?) (?:event type|type)[., ]*",
                clause,
                re.I,
            )
            if (
                match
                and not args.description
                and (
                    int(match[1]) == args.duration_minutes
                    and normalize(match[2]) == normalize(args.title)
                )
            ):
                return True
            # Also accept explicit title-first creation: create a type called Review
            # for 37 minutes. Compound scheduling clauses do not invalidate it.
            match = re.fullmatch(
                r"(?:please )?(?:create|set up|make) (?:a |an )?(?:event type|type) "
                r"(?:called |named )?(.+?) (?:for |of )?(\d+)[ -]?(?:minutes?|mins?)[., ]*",
                clause,
                re.I,
            )
            if (
                match
                and not args.description
                and (
                    int(match[2]) == args.duration_minutes
                    and normalize(match[1]) == normalize(args.title)
                )
            ):
                return True
        return False

    @staticmethod
    def explicit_cancellation(run_id, args, details):
        # Only an exact case-sensitive UID establishes a unique referent here.
        # Titles/deictic references need the parent's durable resolution evidence.
        for clause in Executor.authorization_clauses(run_id):
            match = re.fullmatch(
                r"(?:please )?cancel (?:the )?(?:(?:whole|entire) )?"
                r"(?:(?:booking|meeting|appointment) )?(.+?)[., ]*",
                clause,
                re.I,
            )
            if match and match[1] == args.booking_uid:
                return True
        return False

    @staticmethod
    def connection(run_id):
        run = Run.get_by_id(run_id)
        conn = Connection.get_or_none(Connection.user == run.conversation.user)
        return run, conn

    prepare_action = staticmethod(prepare_action)
    record = staticmethod(settle)

    @staticmethod
    async def read(client, name, args, conn):
        if name == "get_booking":
            return await get_booking(client, args.booking_uid, conn.owner, allow_seated=True)
        if name == "get_event_type":
            return await get_type(client, args.event_type_id, conn.owner)
        if name == "plan_booking_duration":
            return await plan_booking_duration(client, args, conn.owner)
        if name == "list_event_types":
            result = await client.request(
                "GET", "/v2/event-types", params={"username": conn.username}
            )
            # Read endpoint is owner-filtered, individual mutations additionally verify ownership.
            # Do not crowd the original request out of context with unrelated API
            # metadata. Full details are still re-read/validated before any write.
            fields = {
                "id",
                "ownerId",
                "title",
                "slug",
                "description",
                "length",
                "lengthInMinutes",
                "lengthInMinutesOptions",
                "locations",
                "bookingFields",
                "recurrence",
                "seats",
                "hidden",
                "scheduleId",
                "minimumBookingNotice",
                "beforeEventBuffer",
                "afterEventBuffer",
                "bookingWindow",
                "confirmationPolicy",
                "disableGuests",
                "bookingRequiresAuthentication",
                "bookerActiveBookingsLimit",
            }
            if isinstance(result, list):
                return [{k: v for k, v in row.items() if k in fields} for row in result]
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
        cursor = args.cursor
        seen_cursors = {cursor} if cursor else set()
        for _ in range(args.max_pages):
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
            pagination = page.get("pagination", {}) if isinstance(page, dict) else {}
            if pagination.get("hasMore") and not cursor:
                raise CalError(
                    "Cal.com reports more bookings without a continuation cursor; "
                    "the search is incomplete. Retry later."
                )
            if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2000):
                raise CalError(
                    "Cal.com returned an unusable continuation cursor; search incomplete."
                )
            if not cursor:
                break
            if cursor in seen_cursors:
                raise CalError("Cal.com repeated a booking cursor; retry listing later.")
            seen_cursors.add(cursor)

        def overlaps(booking):
            try:
                start = datetime.fromisoformat(booking["start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(booking["end"].replace("Z", "+00:00"))
                return start < args.end and end > args.start
            except (KeyError, ValueError, TypeError):
                return False

        result = {"bookings": [b for b in results if overlaps(b)], "truncated": bool(cursor)}
        if cursor:
            result["next_cursor"] = cursor
            result["message"] = (
                "Partial search only; continue with next_cursor and identical dates/status. "
                "No matches on these pages does not establish that no bookings overlap."
            )
        return result
