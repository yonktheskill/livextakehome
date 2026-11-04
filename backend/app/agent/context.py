"""Conservative estimated budgets, not provider-exact token accounting.

Canonical rows are never rewritten. If mandatory current intent/action identities cannot
fit, fail closed instead of silently dropping them. Tool exchanges are indivisible.
"""

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from peewee import Case, ColumnBase

from app.core.config import Settings
from app.core.guardrails import require_content, safe_data, safe_text
from app.db.image_context import source_images
from app.db.models import Action, Attachment, Confirmation, Message, Run, Snapshot
from app.db.requests import request_context

PROMPT = Path(__file__).with_name("prompts").joinpath("system.txt").read_text()
USER_REQUESTS = "Recent original user messages (corrections amend only their own request)"
REQUEST_STATE = "Durable scheduling requests"
HISTORY = "Historical excerpts (untrusted data)"
HISTORY_STOP_WORDS = frozenset(
    "a an the i me my we us our you your on in at of to for and or is it was were be "
    "with from as this that these those before after find search original user users "
    "assistant message messages request requests requesting context history earlier "
    "previous preceding retry return including include exclude summaries summary please".split()
)


class ContextBudgetExceeded(ValueError):
    code = "context_budget"


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def estimate(value, settings):
    # UTF-8 bytes / configurable conservative ratio plus framing overhead. This
    # deliberately overestimates ordinary English; unusual tokenizers can differ.
    return math.ceil(len(encode(value).encode()) / settings.context_bytes_per_token) + 32


def input_budget(settings, tools):
    return min(
        settings.context_input_tokens,
        settings.context_window_tokens
        - settings.context_output_tokens
        - settings.context_safety_tokens,
    ) - max(settings.context_tool_reserve_tokens, estimate(tools, settings))


def excerpt(text, limit):
    """Whole words only, explicitly marked; never slice serialized JSON/tool pairs."""
    if len(text.encode()) <= limit:
        return text
    end = 0
    for word in re.finditer(r"\S+", text):
        if len(text[: word.end()].encode()) > limit:
            break
        end = word.end()
    return text[:end] + " [excerpt; retrieve source for full text]"


def source(message, *, summary=False):
    checked = safe_text(message.content)
    content = excerpt(checked, 400) if summary else checked
    images = source_images(message.id)
    if summary and images:
        # A text excerpt must not drag a full cached extraction into every summary.
        # Keep provenance and make the omitted source explicitly retrievable.
        images = {
            "attachment_ids": [a["id"] for a in images["attachments"]],
            "image_observations_omitted": True,
        }
    return {
        **images,
        "source_id": message.id,
        "created_at": message.created_at,
        "time_zone": message.time_zone,
        "role": message.role,
        "content": content,
        "truncated": content != checked or bool(summary and images),
    }


def data_message(label, value):
    return {"role": "user", "content": label + ": " + encode(value)}


def covered_sources(messages):
    """Only complete sources are covered; excerpts must remain retrievable in full."""
    result = set()
    segments: dict[tuple, list[tuple[int, int]]] = {}

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if (
                isinstance(value.get("source_id"), str)
                and isinstance(value.get("content"), str)
                and all(
                    type(value.get(k)) is int for k in ("offset", "end_offset", "total_characters")
                )
            ):
                start, end, total = (value[k] for k in ("offset", "end_offset", "total_characters"))
                if 0 <= start <= end <= total and end - start == len(value["content"]):
                    key = (value["source_id"], value.get("conversation_id"), total)
                    segments.setdefault(key, []).append((start, end))
            elif (
                isinstance(value.get("source_id"), str)
                and not value.get("truncated")
                and isinstance(value.get("content"), str)
                and "[excerpt; retrieve source for full text]" not in value.get("content", "")
            ):
                result.add(value["source_id"])
            for item in value.values():
                if isinstance(item, (list, dict)):
                    collect(item)

    for message in messages:
        content = message.get("content") or ""
        for label in (USER_REQUESTS, HISTORY):
            if content.startswith(label + ": "):
                content = content[len(label) + 2 :]
                break
        try:
            collect(json.loads(content))
        except (ValueError, RecursionError):
            continue
    for (source_id, _, total), ranges in segments.items():
        covered = 0
        for start, end in sorted(ranges):
            if start > covered:
                break
            covered = max(covered, end)
        if covered == total:
            result.add(source_id)
    return result


def assemble(run_id, settings=None, tools=None, *, audit=None):
    settings = settings or Settings()
    tools = tools or []
    run = Run.get_by_id(run_id)
    conv = run.conversation
    current = run.message
    require_content(current.content)
    user = conv.user
    # An email local part is a hint, never a verified personal/booking name.
    local = user.email.split("@", 1)[0].split("+", 1)[0]
    name_hint = re.sub(r"[._-]+", " ", local).strip()
    profile = {
        "email": user.email,
        "time_zone": user.time_zone,
        "preferred_name": user.preferred_name,
        "unconfirmed_email_name_hint": name_hint
        if name_hint and all(part.isalpha() for part in name_hint.split())
        else None,
    }
    system = {
        "role": "system",
        "content": f"{PROMPT}\nCurrent UTC: {datetime.now(UTC).isoformat()}\n",
    }
    if Attachment.select().join(Message).where(Message.conversation == conv).exists():
        system["content"] += Path(__file__).with_name("prompts").joinpath("images.txt").read_text()
    actions = list(
        Action.select().join(Run).where(Run.conversation == conv).order_by(Action.created_at.desc())
    )
    unresolved = {"prepared", "awaiting_confirmation", "authorized", "dispatching", "unknown"}
    evidence: list[dict] = []
    for action in actions:
        if action.state not in unresolved and len(evidence) >= 12:
            continue
        confirmation = Confirmation.get_or_none(Confirmation.action == action)
        evidence.append(
            {
                "action_id": action.id,
                "run_id": action.run_id,
                "kind": action.kind,
                "state": action.state,
                "created_at": action.created_at,
                "confirmation_expires": confirmation.expires if confirmation else None,
                "inputs": json.loads(action.inputs) if action.state in unresolved else {},
                "result": json.loads(action.result)
                if action.state in unresolved
                else {
                    k: v
                    for k, v in json.loads(action.result).get("response", {}).items()
                    if k in {"uid", "id", "status", "start", "end"}
                },
            }
        )
    mandatory = [
        system,
        data_message("Canonical action evidence (untrusted data)", safe_data(evidence)),
        data_message("Authenticated user profile (untrusted data)", safe_data(profile)),
    ]
    latest = {"role": "user", "content": encode(source(current))}
    budget = input_budget(settings, tools)
    if audit is not None:
        audit.update(
            {
                "budget_tokens": budget,
                "schema_tokens": estimate(tools, settings),
                "system_tokens": estimate(system, settings),
                "action_evidence_tokens": estimate(mandatory[1], settings),
                "profile_tokens": estimate(mandatory[2], settings),
                "current_request_tokens": estimate(latest, settings),
            }
        )
    requests_state = request_context(run_id, limit=4)
    if requests_state["tasks"] or len(requests_state["inbox"]) > 1:
        task_rows = []
        for task in requests_state["tasks"]:
            facts = task["facts"]
            task_rows.append(
                {
                    **{k: task[k] for k in ("request_id", "revision", "state", "booking_uid")},
                    "facts": {
                        k: v
                        for k, v in facts.items()
                        if k in {"start", "duration_minutes", "event_type_id"}
                    },
                    "origin_message_id": task["origin_message_id"],
                }
            )
        state = {
            "tasks": task_rows,
            "more_tasks": requests_state["tasks_truncated"],
            "pending_messages": [i["message_id"] for i in requests_state["inbox"]],
            "more_messages": requests_state["inbox_truncated"],
        }
        while task_rows and estimate(state, settings) > min(800, budget // 5):
            task_rows.pop()
            state["more_tasks"] = True
        mandatory.append(data_message(REQUEST_STATE, state))
        if audit is not None:
            audit["request_state_tokens"] = estimate(mandatory[-1], settings)
    if audit is not None:
        audit["mandatory_tokens"] = estimate([*mandatory, latest], settings)
    if estimate([*mandatory, latest], settings) > budget:
        raise ContextBudgetExceeded("Current request and action identities exceed input budget")
    previous = Message.select().where(
        (Message.conversation == conv)
        & (Message.created_at <= current.created_at)
        & (Message.id != current.id)
    )
    previous = previous.order_by(Message.created_at.desc(), Message.id.desc())
    # Reserve a bounded chain of original requests/clarifications independently of
    # assistant verbosity. Tool results must not evict it, leaving only "retry".
    requests: list[dict] = []
    available = max(0, budget - estimate([*mandatory, latest], settings))
    # Protected history must share the remaining space with the next tool result.
    # Reserving only against optional history let this chain consume the entire
    # allowance. Keep at least half the available space for original intent when
    # fixed instructions/current input make the configured result reserve impossible.
    result_reserve = min(settings.context_tool_result_tokens, budget // 3, available // 2)
    intent_limit = min(
        settings.context_intent_tokens,
        budget // 3,
        available - result_reserve,
    )
    if audit is not None:
        audit["reserved_tool_result_tokens"] = result_reserve
    for row in previous.where(Message.role == "user").limit(20):
        entry = source(row)
        if estimate(data_message(USER_REQUESTS, [entry, *requests]), settings) > intent_limit:
            entry = source(row, summary=True)
        if estimate(data_message(USER_REQUESTS, [entry, *requests]), settings) <= intent_limit:
            requests.insert(0, entry)
    if requests:
        mandatory.append(data_message(USER_REQUESTS, requests))
        if audit is not None:
            audit["intent_tokens"] = estimate(mandatory[-1], settings)
    included = {entry["source_id"] for entry in requests} | {current.id}
    rows = [row for row in previous.limit(256) if row.id not in included]
    history = [{"role": m.role, "content": encode(source(m))} for m in reversed(rows)]
    target = max(
        estimate([*mandatory, latest], settings),
        min(
            int(budget * settings.context_compaction_threshold),
            budget - min(settings.context_tool_result_tokens, budget // 3),
        ),
    )
    if estimate([*mandatory, *history, latest], settings) <= target:
        return [*mandatory, *history, latest]

    # Select complete recent canonical messages; canonical messages contain no tool
    # protocol fragments. Older coverage is a fresh bounded extractive snapshot.
    recent: list[dict] = []
    for row in rows[: max(0, 24 - len(mandatory) - 2)]:
        candidate = {"role": row.role, "content": encode(source(row))}
        if estimate([*mandatory, candidate, *recent, latest], settings) > target:
            break
        recent.insert(0, candidate)
    included |= covered_sources(recent)
    entries: list[dict] = []
    summary_limit = min(settings.context_summary_tokens, max(0, target // 4))
    for row in rows:
        if row.id in included:
            continue
        entry = source(row, summary=True)
        candidate = data_message(HISTORY, [*entries, entry])
        if estimate(candidate, settings) > summary_limit:
            break
        if estimate([*mandatory, candidate, *recent, latest], settings) > budget:
            break
        entries.append(entry)
    if entries:
        summary = encode(entries)
        # Fresh sources, never recursively summarize a previous snapshot or duplicate it.
        existing = (
            Snapshot.select()
            .where((Snapshot.conversation == conv) & (Snapshot.summary == summary))
            .first()
        )
        if not existing:
            Snapshot.create(
                conversation=conv, boundary=max(e["created_at"] for e in entries), summary=summary
            )
        mandatory.append(data_message(HISTORY, entries))
    return [*mandatory, *recent, latest]


def compact_result(name, value):
    """Keep scheduling identities/facts; full detail can be fetched by exact ID."""
    if name in {"request_context", "resolve_requests"}:

        def reference_source(row):
            if not isinstance(row, dict):
                return row
            source_id = row.get("origin_message_id", row.get("message_id"))
            if not source_id:
                return row
            payload = {
                "original_user_message",
                "content",
                "attachments",
                "image_observations_untrusted",
            }
            result = {k: v for k, v in row.items() if k not in payload}
            if result != row:
                result["source_content_omitted"] = {
                    "source_id": source_id,
                    "retrieve_with": "search_history before interpreting this source",
                }
            return result

        if isinstance(value, list):
            return [reference_source(row) for row in value]
        if isinstance(value, dict):
            return {
                k: [reference_source(row) for row in v]
                if k in {"tasks", "inbox"} and isinstance(v, list)
                else v
                for k, v in value.items()
            }
        return value
    fields = {
        "list_event_types": {
            "id",
            "ownerId",
            "title",
            "slug",
            "length",
            "lengthInMinutes",
            "lengthInMinutesOptions",
            "hidden",
            "minimumBookingNotice",
        },
        "list_bookings": {
            "uid",
            "id",
            "title",
            "start",
            "end",
            "status",
            "eventTypeId",
        },
    }.get(name)
    if fields and isinstance(value, list):
        return [
            {k: v for k, v in row.items() if k in fields} if isinstance(row, dict) else row
            for row in value
        ]
    if fields and isinstance(value, dict) and isinstance(value.get("bookings"), list):
        return {**value, "bookings": compact_result(name, value["bookings"])}
    if name in {
        "search_history",
        "plan_booking_duration",
        "get_booking",
        "get_event_type",
        "get_available_slots",
        "create_booking",
        "reschedule_booking",
        "cancel_booking",
        "create_event_type",
        "save_preferred_name",
        "authorize_action",
    }:
        # Planner facts, source pages and mutation evidence are not disposable prose.
        return value
    return {
        "omitted": "Result exceeded context budget. Narrow the request or fetch "
        "specific resource details; repeating this exact lookup will not help."
    }


def fit_prompt(base, exchanges, tools, settings, *, audit=None):
    """Fit complete exchanges, compacting large results individually."""
    groups = [[dict(m) for m in group] for group in exchanges]
    base = list(base)
    budget = input_budget(settings, tools)
    before = base + [m for g in groups for m in g]
    changes: list[dict] = []
    if audit is not None:
        audit.update(
            {
                "budget_tokens": budget,
                "schema_tokens": estimate(tools, settings),
                "before_tokens": estimate(before, settings),
                "source_ids_before": sorted(covered_sources(base)),
                "changes": changes,
            }
        )
    optional = [
        m
        for m in base[3:-1]
        if m["role"] != "system"
        and not m["content"].startswith((USER_REQUESTS + ": ", REQUEST_STATE + ": "))
    ]

    def size():
        return estimate(base + [m for g in groups for m in g], settings)

    # Request tools echo original captions/extractions beside their durable facts.
    # Reference originals already protected in this prompt rather than serializing
    # them again. Optional history is deliberately excluded: it may be evicted below.
    originals = {}
    for message in base:
        if message in optional:
            continue
        content = message.get("content") or ""
        if content.startswith(USER_REQUESTS + ": "):
            content = content[len(USER_REQUESTS) + 2 :]
        try:
            value = json.loads(content)
        except ValueError:
            continue
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, dict) and entry.get("source_id") in covered_sources([message]):
                originals[entry["source_id"]] = entry

    def deduplicate_source(value):
        if not isinstance(value, dict):
            return value
        result = dict(value)
        original = originals.get(value.get("origin_message_id", value.get("message_id")))
        if original:
            removed = False
            for key in ("original_user_message", "content"):
                if key in result and result[key] == original["content"]:
                    del result[key]
                    removed = True
            for key in ("attachments", "image_observations_untrusted"):
                if key in result and key in original and result[key] == original[key]:
                    del result[key]
                    removed = True
            if removed:
                result["source_content_in_context"] = original["source_id"]
        return result

    for group in groups:
        names = {c["id"]: c["function"]["name"] for m in group for c in m.get("tool_calls", [])}
        for message in group:
            if message["role"] != "tool" or names.get(message["tool_call_id"]) not in {
                "request_context",
                "resolve_requests",
            }:
                continue
            original = message["content"]
            value = json.loads(original)
            if isinstance(value, list):
                value = [deduplicate_source(row) for row in value]
            elif isinstance(value, dict):
                value = {
                    k: [deduplicate_source(row) for row in v]
                    if k in {"tasks", "inbox"} and isinstance(v, list)
                    else v
                    for k, v in value.items()
                }
            compacted = encode(value)
            if len(compacted.encode()) < len(original.encode()):
                message["content"] = compacted
                changes.append(
                    {
                        "operation": "deduplicate_sources",
                        "call_id": message["tool_call_id"],
                        "before_bytes": len(original.encode()),
                        "after_bytes": len(compacted.encode()),
                    }
                )

    for group in groups:
        names = {c["id"]: c["function"]["name"] for m in group for c in m.get("tool_calls", [])}
        for message in group:
            if size() <= budget:
                break
            if message["role"] != "tool" or names.get(message["tool_call_id"]) not in {
                "list_event_types",
                "list_bookings",
            }:
                continue
            original = message["content"]
            value = json.loads(original)
            compacted = encode(compact_result(names[message["tool_call_id"]], value))
            if len(compacted.encode()) < len(original.encode()) and "omitted" not in compacted:
                message["content"] = compacted
                changes.append(
                    {
                        "operation": "compact_result",
                        "call_id": message["tool_call_id"],
                        "before_bytes": len(original.encode()),
                        "after_bytes": len(compacted.encode()),
                        "omitted": False,
                    }
                )
    while optional and size() > budget:
        message = optional.pop(0)
        base.remove(message)
        changes.append(
            {"operation": "drop_history", "source_ids": sorted(covered_sources([message]))}
        )
    if groups and size() > budget:
        names = {
            c["id"]: c["function"]["name"]
            for group in groups
            for m in group
            for c in m.get("tool_calls", [])
        }
        candidates = sorted(
            (m for group in groups for m in group if m["role"] == "tool"),
            key=lambda m: len(m["content"].encode()),
            reverse=True,
        )
        for message in candidates:
            if size() <= budget:
                break
            original = message["content"]
            value = json.loads(original)
            if (
                names.get(message["tool_call_id"]) == "search_history"
                and isinstance(value, list)
                and value
            ):
                # Continuations advance from the most recent page. Preserve that
                # page before shortening it; shrinking old pages cannot advance retrieval.
                while len(groups) > 1 and size() > budget:
                    removed = groups.pop(0)
                    changes.append(
                        {
                            "operation": "drop_exchange",
                            "call_ids": [m["tool_call_id"] for m in removed if m["role"] == "tool"],
                        }
                    )
                if not any(message is m for group in groups for m in group) or size() <= budget:
                    continue
                first = value[0]
                if isinstance(first, dict) and all(
                    key in first for key in ("offset", "end_offset", "total_characters", "content")
                ):
                    # Retain a smaller, valid source page rather than replacing its
                    # continuation with an omission notice. Schema growth and current
                    # intent can leave less room than the normal retrieval allowance.
                    def history_page(length, first=first):
                        end = first["offset"] + length
                        total = first["total_characters"]
                        return encode(
                            [
                                {
                                    **first,
                                    "content": first["content"][:length],
                                    "end_offset": end,
                                    "next_offset": end if end < total else None,
                                    "complete": end == total,
                                    "truncated": first["offset"] != 0 or end != total,
                                }
                            ]
                        )

                    low, high = 0, len(first["content"])
                    while low < high:
                        mid = (low + high + 1) // 2
                        message["content"] = history_page(mid)
                        if size() <= budget:
                            low = mid
                        else:
                            high = mid - 1
                    message["content"] = history_page(low)
                    if low == 0 and first["content"]:
                        raise ContextBudgetExceeded("History page cannot fit with current intent")
                    changes.append(
                        {
                            "operation": "page_history_result",
                            "call_id": message["tool_call_id"],
                            "before_bytes": len(original.encode()),
                            "after_bytes": len(message["content"].encode()),
                        }
                    )
                    continue
            compacted = encode(compact_result(names.get(message["tool_call_id"]), value))
            if len(compacted.encode()) >= len(original.encode()):
                continue  # Never replace tiny results with larger omission notices.
            message["content"] = compacted
            changes.append(
                {
                    "operation": "compact_result",
                    "call_id": message["tool_call_id"],
                    "before_bytes": len(original.encode()),
                    "after_bytes": len(compacted.encode()),
                    "omitted": isinstance(json.loads(compacted), dict)
                    and "omitted" in json.loads(compacted),
                }
            )
    while len(groups) > 1 and size() > budget:
        removed = groups.pop(0)
        changes.append(
            {
                "operation": "drop_exchange",
                "call_ids": [m["tool_call_id"] for m in removed if m["role"] == "tool"],
            }
        )
    result = base + [m for g in groups for m in g]
    if audit is not None:
        audit.update(
            {
                "after_tokens": estimate(result, settings),
                "tool_exchange_tokens": estimate([m for g in groups for m in g], settings),
                "source_ids_after": sorted(covered_sources(base)),
                "result_sizes": [
                    {"call_id": m["tool_call_id"], "bytes": len(m["content"].encode())}
                    for m in result
                    if m["role"] == "tool"
                ],
            }
        )
    if estimate(result, settings) > budget:
        raise ContextBudgetExceeded("Complete tool exchange exceeds input budget")
    return result


def authorized(run_id):
    action = Action.get_or_none((Action.run == run_id) & (Action.state == "authorized"))
    if action and Confirmation.get_or_none(Confirmation.action == action):
        return action.kind, json.loads(action.inputs)
    return None


def retrieve(run_id, query, excluded=None, settings=None, *, role="user", source_id=None, offset=0):
    """Return bounded pages of filtered originals; offsets count Unicode characters.

    Exact IDs never bypass conversation/time boundaries. Large image sources use
    paged JSON containing the filtered caption and observations; ordinary sources
    page their filtered text. The representation is stable across continuations.
    """
    if type(offset) is not int or offset < 0 or (offset and not source_id):
        raise ValueError("offset must be a nonnegative integer and requires source_id")
    settings = settings or Settings()
    run = Run.get_by_id(run_id)
    words = list(
        dict.fromkeys(
            word for word in re.findall(r"\w+", query.casefold()) if word not in HISTORY_STOP_WORDS
        )
    )[:32]
    rows = Message.select().where(
        (Message.conversation == run.conversation_id)
        & (Message.created_at <= run.message.created_at)
        & (Message.id != run.message_id)
    )
    if source_id:
        rows = rows.where(Message.id == source_id)
    else:
        rows = rows.where(Message.role == role)
    if excluded and not source_id:
        rows = rows.where(Message.id.not_in(excluded))
    order = [Message.created_at.desc(), Message.id.desc()]
    if words and not source_id:
        score: ColumnBase = Case(None, [(Message.content.contains(words[0]), 1)], 0)
        for word in words[1:]:
            score += Case(None, [(Message.content.contains(word), 1)], 0)
        matches = rows.where(score > 0)
        if matches.exists():
            rows = matches
            order.insert(0, score.desc())
    results: list[dict] = []
    for message in rows.order_by(*order).limit(8):
        entry = {**source(message), "conversation_id": message.conversation_id}
        text = entry["content"]
        if (
            "image_observations_untrusted" in entry
            and estimate({**entry, "content": ""}, settings)
            > settings.context_retrieval_tokens // 2
        ):
            # Otherwise unpageable extraction metadata alone can exceed every page.
            text = encode(
                {
                    k: entry.pop(k)
                    for k in ("content", "attachments", "image_observations_untrusted")
                }
            )
            entry["source_format"] = "image_source_json"
        if offset > len(text):
            raise ValueError("offset exceeds source length")

        def page(end, entry=entry, text=text):
            return {
                **entry,
                "id": entry["source_id"],
                "content": text[offset:end],
                "offset": offset,
                "end_offset": end,
                "total_characters": len(text),
                "next_offset": end if end < len(text) else None,
                "complete": end == len(text),
                # A final suffix alone is still not the full source.
                "truncated": offset != 0 or end != len(text),
            }

        # Bound serialized metadata and escaped/multibyte content together. No word
        # clipping: concatenating contiguous pages reproduces the original tail.
        low, high = offset, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if estimate([*results, page(mid)], settings) <= settings.context_retrieval_tokens:
                low = mid
            else:
                high = mid - 1
        if low == offset and offset < len(text):
            if results:
                break  # This source remains discoverable on a subsequent search.
            raise ContextBudgetExceeded(
                "History retrieval budget cannot fit source metadata/content"
            )
        candidate = page(low)
        if estimate([*results, candidate], settings) > settings.context_retrieval_tokens:
            raise ContextBudgetExceeded("History retrieval budget cannot fit source metadata")
        results.append(candidate)
        if candidate["next_offset"] is not None:
            break  # Reserve the remaining source for an explicit continuation.
    return results
