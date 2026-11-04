import json
import time
from uuid import uuid4

import pytest
from conftest import start_test_run
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent.runner import Runner
from app.agent.tracing import record_tool, sanitize
from app.core.config import Settings
from app.db.models import Trace, db
from app.db.repository import accept, cleanup
from app.main import create_app
from app.providers.base import ProviderEvent
from app.providers.fake import FakeProvider
from app.tools.executor import Waiting


def test_recursive_redaction_and_key_shaped_values():
    private = [
        "password-private",
        "cookie-private",
        "code-private",
        "sk-test_abcdefghijk",
        "cal_live_abcdefghijk",
        "Bearer credential-value",
        "654321",
    ]
    result = sanitize(
        {
            "safe": "Friday",
            "nested": [
                {"Password": private[0], "Set-Cookie": private[1], "verificationCode": private[2]}
            ],
            "description": " ".join(private[3:]),
            "json": json.dumps({"api_key": "arbitrary-private"}),
            "password-private": "hidden",
            "chain_of_thought": "never retain",
        }
    )
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in [*private, "arbitrary-private", "never retain"])
    assert result["safe"] == "Friday"


@pytest.mark.asyncio
async def test_durable_tool_inputs_outputs_and_errors_no_vault_read(account):
    user, _ = account
    run_id = accept(user, "private original prompt", str(uuid4()))["run_id"]
    start_test_run(run_id)
    settings = Settings(llm_provider="fake", openrouter_api_key="project-private")
    runner = Runner(settings, None, FakeProvider())

    class OfflineExecutor:
        async def execute(self, run_id, name, arguments):
            if name == "wait":
                raise Waiting("password=private-exception")
            return {
                "booking_uid": "booking_ABC",
                "nested": {"password": "output-private"},
                "description": "project-private cal_live_abcdefghijkl",
            }

    runner.executor = OfflineExecutor()
    await runner.tool(
        run_id,
        "get_booking",
        {"booking_uid": "booking_ABC", "nested": [{"apiKey": "input-private"}]},
        "call_ABC",
    )
    with pytest.raises(Waiting):
        await runner.tool(run_id, "wait", {}, "call_wait")
    traces = [
        json.loads(t.payload) for t in Trace.select().where(Trace.kind == "tool").order_by(Trace.id)
    ]
    assert traces[0]["inputs"]["booking_uid"] == "booking_ABC"
    assert traces[0]["outputs"]["booking_uid"] == "booking_ABC"
    assert traces[0]["call_id"] == "call_ABC" and traces[0]["run_id"] == run_id
    assert traces[0]["elapsed_ms"] >= 0 and traces[0]["ok"]
    assert traces[1]["error"] == "Waiting" and not traces[1]["ok"]
    payload = json.dumps(traces)
    assert all(
        s not in payload
        for s in [
            "input-private",
            "output-private",
            "project-private",
            "cal_live_abcdefghijkl",
            "private-exception",
            "private original prompt",
        ]
    )


def test_trace_byte_bound_and_existing_retention(account):
    user, _ = account
    run_id = accept(user, "hi", str(uuid4()))["run_id"]
    start_test_run(run_id)
    settings = Settings(trace_max_bytes=1024)
    record_tool(
        run_id,
        "get_booking",
        "call",
        {"large": "data " * 5000},
        {"large": ["data" * 500] * 40},
        2,
        settings,
    )
    row = Trace.get()
    assert len(row.payload.encode()) <= 1024
    assert json.loads(row.payload)["outputs"]["omitted"] == "trace byte limit"
    row.created_at = time.time() - 31 * 86400
    row.save()
    assert cleanup() == 1 and Trace.select().count() == 0


def test_debug_endpoint_owner_scoped_and_opt_in(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "debug.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
        llm_provider="fake",
        debug_api=True,
    )
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/auth/register",
            json={"email": "alice@example.com", "password": "long-password-123"},
        )
        run_id = client.post(
            "/api/chat/messages", json={"content": "hello", "client_request_id": str(uuid4())}
        ).json()["run_id"]
        with db.connection_context():
            record_tool(run_id, "test", "call", {"password": "private"}, {"ok": True}, 0, settings)
        response = client.get(f"/api/runs/{run_id}/debug")
        assert response.status_code == 200 and "private" not in response.text
        assert response.json()[0]["metadata"]["outputs"] == {"ok": True}
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/register", json={"email": "bob@example.com", "password": "long-password-123"}
        )
        assert client.get(f"/api/runs/{run_id}/debug").status_code == 404
        settings.debug_api = False
        assert client.get(f"/api/runs/{run_id}/debug").status_code == 404


@pytest.mark.asyncio
async def test_schema_failure_is_traced_without_network(account):
    user, vault = account
    run_id = accept(user, "hello", str(uuid4()))["run_id"]
    start_test_run(run_id)
    runner = Runner(
        Settings(llm_provider="fake"),
        vault,
        FakeProvider(
            steps=[
                [
                    ProviderEvent(
                        "tool_call", call_id="call_invalid", name="search_history", arguments={}
                    ),
                ],
                [ProviderEvent("text_delta", text="Try again")],
            ]
        ),
    )
    await runner.execute(run_id)
    row = Trace.get(Trace.kind == "tool")
    trace = json.loads(row.payload)
    assert not trace["ok"] and trace["outputs"]["error"] == "Invalid history query arguments"
