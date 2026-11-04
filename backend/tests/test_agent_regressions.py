"""Realistic multi-tool context pressure and failed-operation diagnostics."""

import json
from uuid import uuid4

import pytest
from conftest import start_test_run

from app.agent.context import assemble, fit_prompt
from app.agent.runner import Runner
from app.core.config import Settings
from app.db.models import Event, Run, Trace
from app.db.repository import accept
from app.providers.base import ProviderError, ProviderEvent


def exchange():
    types = [
        {
            "id": i + 1,
            "title": "Consultation",
            "lengthInMinutes": 30,
            "bookingFields": [{"slug": "notes", "description": "details " * 100}],
        }
        for i in range(3)
    ]
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}
                for name in ("list_bookings", "list_event_types")
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "list_bookings",
            "content": '{"bookings":[],"truncated":false}',
        },
        {"role": "tool", "tool_call_id": "list_event_types", "content": json.dumps(types)},
    ]


def test_large_result_compacts_without_erasing_small_result(account):
    user, _ = account
    run_id = accept(user, "Book a consultation tomorrow", str(uuid4()))["run_id"]
    start_test_run(run_id)
    settings = Settings(context_input_tokens=4000)
    base = assemble(run_id, settings)
    audit = {}
    result = fit_prompt(base, [exchange()], [], settings, audit=audit)
    assert json.loads(result[-2]["content"]) == {"bookings": [], "truncated": False}
    assert json.loads(result[-1]["content"])[0] == {
        "id": 1,
        "title": "Consultation",
        "lengthInMinutes": 30,
    }
    assert audit["changes"][0]["call_id"] == "list_event_types"
    assert audit["after_tokens"] <= audit["budget_tokens"]


@pytest.mark.asyncio
async def test_repeating_reads_stop_with_specific_durable_error(account):
    user, vault = account
    run_id = accept(user, "List bookings", str(uuid4()))["run_id"]
    start_test_run(run_id)

    class Repeating:
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            yield ProviderEvent(
                "tool_call", name="list_event_types", arguments={}, call_id=str(self.count)
            )

    class Reads:
        async def execute(self, *args):
            return [{"id": 1, "lengthInMinutes": 30}]

    provider = Repeating()
    runner = Runner(Settings(), vault, provider)
    runner.executor = Reads()
    await runner.execute(run_id)
    assert provider.count == 3
    assert Run.get_by_id(run_id).status == "failed"
    payload = json.loads(Event.get((Event.run == run_id) & (Event.kind == "run.failed")).payload)
    assert payload["code"] == "repeated_calls"
    assert Trace.select().where(Trace.kind == "error").count() == 1


@pytest.mark.asyncio
async def test_provider_failure_retains_safe_reason_and_started_operation(account):
    user, vault = account
    run_id = accept(user, "Hello", str(uuid4()))["run_id"]
    start_test_run(run_id)

    class Broken:
        async def stream(self, messages, tools):
            raise ProviderError("Private upstream detail", code="timeout")
            yield

    await Runner(Settings(), vault, Broken()).execute(run_id)
    rows = list(Trace.select().where(Trace.run == run_id))
    kinds = {t.kind for t in rows}
    assert {"run.started", "model.started", "model.failed", "error"} <= kinds
    assert "Private upstream detail" not in "".join(t.payload for t in rows)
    error = json.loads(next(t.payload for t in rows if t.kind == "error"))
    assert error["code"] == "timeout" and error["stage"] == "model"
    assert Run.get_by_id(run_id).status == "failed"


@pytest.mark.asyncio
async def test_changing_lookup_results_are_progress(account):
    user, vault = account
    run_id = accept(user, "List bookings", str(uuid4()))["run_id"]
    start_test_run(run_id)

    class Changing:
        count = 0

        async def execute(self, *args):
            self.count += 1
            return [{"id": self.count, "lengthInMinutes": 30}]

    class Decisions:
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            if self.count < 5:
                yield ProviderEvent(
                    "tool_call", name="list_event_types", arguments={}, call_id=str(self.count)
                )
            else:
                yield ProviderEvent("text_delta", text="The scheduling details changed.")

    runner = Runner(Settings(), vault, Decisions())
    runner.executor = Changing()
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "succeeded"
    assert runner.executor.count == 4


@pytest.mark.asyncio
async def test_realistic_two_lookup_batch_reaches_final_answer(account):
    user, vault = account
    for _ in range(12):
        accept(user, "Book a consultation tomorrow at noon in Asia/Singapore.", str(uuid4()))
    run_id = accept(user, "retry", str(uuid4()))["run_id"]
    start_test_run(run_id)
    data = exchange()

    class Reads:
        async def execute(self, run_id, name, args):
            return json.loads(next(m["content"] for m in data if m.get("tool_call_id") == name))

    class Decisions:
        count = 0

        async def stream(self, messages, tools):
            self.count += 1
            if self.count == 1:
                for name in ("list_bookings", "list_event_types"):
                    yield ProviderEvent("tool_call", name=name, arguments={}, call_id=name)
            else:
                results = {
                    m["tool_call_id"]: json.loads(m["content"])
                    for m in messages
                    if m["role"] == "tool"
                }
                assert results["list_bookings"] == {"bookings": [], "truncated": False}
                assert [r["id"] for r in results["list_event_types"]] == [1, 2, 3]
                yield ProviderEvent("text_delta", text="A consultation type is available.")

    provider = Decisions()
    runner = Runner(Settings(), vault, provider)
    runner.executor = Reads()
    await runner.execute(run_id)
    assert Run.get_by_id(run_id).status == "succeeded"
    assert provider.count == 2


@pytest.mark.asyncio
async def test_run_deadline_is_diagnosed_while_model_is_active(account):
    import asyncio

    user, vault = account
    run_id = accept(user, "Hello", str(uuid4()))["run_id"]
    start_test_run(run_id)

    class Blocked:
        async def stream(self, messages, tools):
            await asyncio.Event().wait()
            yield

    settings = Settings().model_copy(update={"run_timeout_seconds": 0.02})
    await Runner(settings, vault, Blocked()).execute(run_id)
    rows = {t.kind: json.loads(t.payload) for t in Trace.select().where(Trace.run == run_id)}
    assert rows["error"]["code"] == "run_timeout"
    assert rows["error"]["stage"] == "model"
    assert "model.failed" in rows
    assert Run.get_by_id(run_id).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget,code",
    [("step", "model_step_limit"), ("tool", "tool_limit"), ("output", "output_limit")],
)
async def test_budget_failures_have_specific_diagnostic_codes(account, budget, code):
    user, vault = account
    run_id = accept(user, "Hello", str(uuid4()))["run_id"]
    start_test_run(run_id)
    settings = Settings(
        max_model_steps=1,
        max_tool_calls=0 if budget == "tool" else 24,
        context_output_tokens=1 if budget == "output" else 4096,
    )

    class Decisions:
        async def stream(self, messages, tools):
            if budget == "output":
                yield ProviderEvent("text_delta", text="Output exceeds the tiny test budget.")
            else:
                yield ProviderEvent(
                    "tool_call", name="search_history", arguments={"query": ""}, call_id="test-call"
                )

    await Runner(settings, vault, Decisions()).execute(run_id)
    error = json.loads(Trace.get((Trace.run == run_id) & (Trace.kind == "error")).payload)
    assert error["code"] == code
    assert error["max_model_steps"] == 1
    assert Run.get_by_id(run_id).status == "failed"
