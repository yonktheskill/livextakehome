import json
from uuid import uuid4

import pytest

from app.agent.context import (
    ContextBudgetExceeded,
    assemble,
    covered_sources,
    estimate,
    fit_prompt,
    input_budget,
    retrieve,
)
from app.core.config import Settings
from app.db.models import Action, Confirmation, Message, Run, Snapshot
from app.db.repository import accept


def submit(user, text):
    return accept(user, text, str(uuid4()))["run_id"]


def test_long_turn_threshold_and_current_request_untouched(account):
    user, _ = account
    first = submit(user, "Earlier booking intent. " * 500)
    latest_text = "Correction: Friday at 15:30, keep identifier booking_ABC exactly."
    latest = submit(user, latest_text)
    before = Message.get_by_id(Run.get_by_id(first).message_id).content
    settings = Settings(context_input_tokens=4000)
    messages = assemble(latest, settings)
    assert json.loads(messages[-1]["content"])["content"] == latest_text
    assert estimate(messages, settings) <= input_budget(settings, [])
    assert Snapshot.select().count() == 1  # Two messages, no count-based trigger.
    assert Message.get_by_id(Run.get_by_id(first).message_id).content == before
    assert sum(m["role"] == "system" for m in messages) == 1


def test_summary_fresh_bounded_provenance_and_retrieval_dedup(account):
    user, _ = account
    for i in range(40):
        submit(user, f"candidate {i}: Alice prefers Friday. " * 12)
    run_id = submit(user, "Find candidate preferences")
    settings = Settings(context_input_tokens=4000, context_summary_tokens=700)
    messages = assemble(run_id, settings)
    snapshots = list(Snapshot.select())
    assert len(snapshots) == 1
    entries = json.loads(snapshots[0].summary)
    assert entries and all({"source_id", "created_at", "time_zone"} <= e.keys() for e in entries)
    assert estimate(entries, settings) <= settings.context_summary_tokens
    ids = [e["source_id"] for e in entries]
    recent_ids = [json.loads(m["content"])["source_id"] for m in messages[3:]]
    assert not set(ids) & set(recent_ids)
    assert assemble(run_id, settings)[-1] == messages[-1]
    assert Snapshot.select().count() == 1
    found = retrieve(run_id, "candidate", covered_sources(messages), settings)
    assert found and not {r["id"] for r in found} & covered_sources(messages)
    assert estimate(found, settings) <= settings.context_retrieval_tokens


def test_unknown_and_confirmation_ids_and_timestamps_survive_recent_actions(account):
    user, _ = account
    run_id = submit(user, "yes")
    run = Run.get_by_id(run_id)
    unknown = Action.create(
        run=run_id,
        revision=run.revision,
        generation=1,
        kind="cancel_booking",
        inputs='{"booking_uid":"exact_ABC"}',
        fingerprint="x",
        state="unknown",
        created_at=123.25,
    )
    Confirmation.create(conversation=run.conversation_id, action=unknown, expires=999.5)
    for _ in range(15):
        Action.create(
            run=run_id,
            revision=run.revision,
            generation=1,
            kind="cancel_booking",
            inputs="{}",
            fingerprint="x",
            state="succeeded",
        )
    context = assemble(run_id)
    evidence = json.loads(context[1]["content"].split(": ", 1)[1])
    row = next(e for e in evidence if e["action_id"] == unknown.id)
    assert row["inputs"]["booking_uid"] == "exact_ABC"
    assert row["created_at"] == 123.25 and row["confirmation_expires"] == 999.5


def test_mandatory_oversize_fails_closed_without_truncating_canonical(account):
    user, _ = account
    run_id = submit(user, "keep every word " * 500)
    with pytest.raises(ContextBudgetExceeded):
        assemble(run_id, Settings(context_input_tokens=1500))
    assert Run.get_by_id(run_id).message.content == "keep every word " * 500


def test_final_cap_counts_schema_reserve_and_keeps_complete_tool_json(account):
    user, _ = account
    run_id = submit(user, "latest intent")
    settings = Settings(context_input_tokens=4000)
    tools = [{"type": "function", "function": {"name": "large", "schema": "schema " * 200}}]
    base = assemble(run_id, settings, tools)
    exchange = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "exact_call",
                    "type": "function",
                    "function": {"name": "large", "arguments": '{"booking_uid":"exact"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "exact_call",
            "content": json.dumps({"data": "x" * 30000}),
        },
    ]
    result = fit_prompt(base, [exchange], tools, settings)
    assert estimate(result, settings) <= input_budget(settings, tools)
    assert result[-2]["tool_calls"][0]["id"] == result[-1]["tool_call_id"] == "exact_call"
    assert "omitted" in json.loads(result[-1]["content"])
    assert result[2] == base[-1]  # Current intent retained in full.
