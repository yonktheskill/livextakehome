"""Deterministic bounded inference context; canonical history/actions remain authoritative."""

import json
from datetime import UTC, datetime
from pathlib import Path

from app.db.models import Action, Confirmation, Message, Run, Snapshot

PROMPT = Path(__file__).with_name("prompts").joinpath("system.txt").read_text()


def assemble(run_id):
    run = Run.get_by_id(run_id)
    conv = run.conversation
    messages = list(
        Message.select()
        .where(Message.conversation == conv)
        .order_by(Message.created_at.desc())
        .limit(60)
    )[::-1]
    if len(messages) > 30:
        older = messages[:-20]
        # Extractive snapshot: carries timestamps/provenance, never invented successful actions.
        summary = json.dumps(
            [
                {
                    "id": m.id,
                    "at": m.created_at,
                    "zone": m.time_zone,
                    "role": m.role,
                    "excerpt": m.content[:500],
                }
                for m in older
            ]
        )
        snapshot = (
            Snapshot.select()
            .where(Snapshot.conversation == conv)
            .order_by(Snapshot.created_at.desc())
            .first()
        )
        if not snapshot or snapshot.boundary < older[-1].created_at:
            Snapshot.create(conversation=conv, boundary=older[-1].created_at, summary=summary)
        messages = messages[-20:]
    snapshot = (
        Snapshot.select()
        .where(Snapshot.conversation == conv)
        .order_by(Snapshot.created_at.desc())
        .first()
    )
    actions = list(
        Action.select()
        .join(Run)
        .where(Run.conversation == conv)
        .order_by(Action.created_at.desc())
        .limit(12)
    )
    evidence = [
        {
            "action_id": a.id,
            "kind": a.kind,
            "state": a.state,
            "inputs": json.loads(a.inputs),
            "result": json.loads(a.result),
        }
        for a in actions
    ]
    system = f"{PROMPT}\nCurrent UTC: {datetime.now(UTC).isoformat()}\nZone: {conv.user.time_zone}"
    context = [{"role": "system", "content": system}]
    if snapshot:
        context.append(
            {
                "role": "system",
                "content": "Untrusted historical excerpts with source IDs: " + snapshot.summary,
            }
        )
    context.append(
        {"role": "system", "content": "Canonical action evidence: " + json.dumps(evidence)}
    )
    for message in messages:
        context.append(
            {
                "role": message.role,
                "content": "[sent_at="
                + datetime.fromtimestamp(message.created_at, UTC).isoformat()
                + ", "
                f"zone={message.time_zone}] {message.content[:12000]}",
            }
        )
    return context


def authorized(run_id):
    action = Action.get_or_none((Action.run == run_id) & (Action.state == "authorized"))
    if action and Confirmation.get_or_none(Confirmation.action == action):
        return action.kind, json.loads(action.inputs)
    return None


def retrieve(run_id, query):
    """Local lexical retrieval with original timestamps, bounded before model injection."""
    run = Run.get_by_id(run_id)
    words = [w for w in query.split() if len(w) > 3][:8]
    rows = Message.select().where(Message.conversation == run.conversation_id)
    if words:
        condition = Message.content.contains(words[0])
        for word in words[1:]:
            condition |= Message.content.contains(word)
        rows = rows.where(condition)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content[:2000],
            "created_at": m.created_at,
            "time_zone": m.time_zone,
        }
        for m in rows.order_by(Message.created_at.desc()).limit(8)
    ]
