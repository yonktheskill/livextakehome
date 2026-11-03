import json
from uuid import uuid4

import pytest

from app.agent.context import (
    HISTORY,
    USER_REQUESTS,
    ContextBudgetExceeded,
    assemble,
    covered_sources,
    data_message,
    estimate,
    fit_prompt,
    input_budget,
    retrieve,
    source,
)
from app.core.config import Settings
from app.db.models import Action, Confirmation, Conversation, Message, Run, Snapshot, User
from app.db.repository import accept
from app.tools.schemas import definitions


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
    # Even a two-message conversation can require an excerpt. Its original stays
    # available, rather than treating the truncated source as completely covered.
    assert "[excerpt; retrieve source for full text]" in json.dumps(messages)
    assert Run.get_by_id(first).message_id not in covered_sources(messages)
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
    recent_ids = covered_sources(
        [m for m in messages if not m["content"].startswith(HISTORY + ": ")]
    )
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


def test_retry_keeps_original_user_request_and_attendee_through_tool_pressure(account):
    user, _ = account
    user.time_zone = "Asia/Singapore"
    user.save()
    original = submit(user, "Book a 30 minute meeting tomorrow at 12 noon.")
    attendee = submit(user, "Attendee: Alice, alice@example.com")
    for _ in range(5):
        Message.create(
            conversation=Run.get_by_id(original).conversation_id,
            role="assistant",
            content="I'll look for the original request before retrying. " * 100,
        )
    retry = submit(user, "retry")
    settings = Settings()
    tools = definitions()
    base = assemble(retry, settings, tools)
    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "large_read",
                "type": "function",
                "function": {"name": "list_event_types", "arguments": "{}"},
            }
        ],
    }
    result = fit_prompt(
        base,
        [
            [
                tool_call,
                {
                    "role": "tool",
                    "tool_call_id": "large_read",
                    "content": json.dumps({"data": "x" * 30000}),
                },
            ]
        ],
        tools,
        settings,
    )
    protected = next(m for m in result if m["content"].startswith(USER_REQUESTS + ": "))
    requests = json.loads(protected["content"].split(": ", 1)[1])
    assert [r["source_id"] for r in requests] == [
        Run.get_by_id(original).message_id,
        Run.get_by_id(attendee).message_id,
    ]
    assert requests[0]["content"] == "Book a 30 minute meeting tomorrow at 12 noon."
    assert requests[0]["time_zone"] == "Asia/Singapore"
    assert requests[0]["created_at"] == Run.get_by_id(original).message.created_at
    assert requests[1]["content"] == "Attendee: Alice, alice@example.com"
    assert json.loads(result[-3]["content"])["content"] == "retry"
    assert result[-2] == tool_call
    assert json.loads(result[-1]["content"])["omitted"]
    assert estimate(result, settings) <= input_budget(settings, tools)


def test_search_returns_original_users_and_ranks_meaningful_terms(account):
    user, _ = account
    original = submit(user, "book a 30 minute meeting tomorrow, 12 noon")
    submit(user, "What about November 2026?")
    conversation = Run.get_by_id(original).conversation_id
    for _ in range(10):
        Message.create(
            conversation=conversation,
            role="assistant",
            content="Original scheduling request: 4 November 2026 at noon Asia/Singapore",
        )
    run_id = submit(user, "retry")
    found = retrieve(
        run_id,
        "Find original user messages requesting the 30-minute meeting on 4 November 2026 "
        "at noon Asia/Singapore, including attendee details. Return user source messages, "
        "not assistant summaries.",
    )
    assert found[0]["id"] == Run.get_by_id(original).message_id
    assert all(m["role"] == "user" for m in found)
    # Meta-language that has no literal match still recovers recent user context.
    assert retrieve(run_id, "original request")[0]["content"] == "What about November 2026?"
    assert retrieve(run_id, "", role="assistant")[0]["role"] == "assistant"


def test_excerpts_remain_retrievable_in_full_by_source_id(account):
    user, _ = account
    text = "Meeting preferences with original details.\n" * 20
    original = submit(user, text)
    message = Run.get_by_id(original).message
    run_id = submit(user, "retry")
    excerpt = source(message, summary=True)
    context = [data_message(HISTORY, [excerpt])]
    assert excerpt["truncated"] and message.id not in covered_sources(context)
    full = retrieve(run_id, "", covered_sources(context), source_id=message.id)
    assert full[0]["content"] == text and not full[0]["truncated"]
    assert message.id in covered_sources([data_message(HISTORY, full)])
    assert retrieve(run_id, "", {message.id}) == []
    # An explicit source lookup can re-read even a source already seen in full.
    assert retrieve(run_id, "", {message.id}, source_id=message.id)[0]["content"] == text


def test_retrieval_cannot_read_other_conversations_future_or_own_narration(account):
    user, _ = account
    original = submit(user, "original meeting")
    run_id = submit(user, "retry")
    Message.create(
        conversation=Run.get_by_id(run_id).conversation_id,
        role="assistant",
        content="I'll retry the original meeting.",
    )
    future = submit(user, "later meeting request")
    other = User.create(email="another@example.com", password_hash="unused")
    Conversation.create(user=other)
    other_run = submit(other, "private meeting")
    assert [m["id"] for m in retrieve(run_id, "meeting")] == [Run.get_by_id(original).message_id]
    assert retrieve(run_id, "", role="assistant") == []
    for excluded_run in (future, other_run):
        assert retrieve(run_id, "", source_id=Run.get_by_id(excluded_run).message_id) == []
