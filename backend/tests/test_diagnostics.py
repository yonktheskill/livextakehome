import json
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.agent.tracing import error_metadata, record_diagnostic, record_tool, tool_fingerprint
from app.calcom.client import CalClient, CalError
from app.core.config import Settings
from app.db.models import Trace, db
from app.db.repository import accept
from app.main import create_app
from app.providers.base import ProviderError, ProviderLimits
from app.providers.openrouter import OpenRouterProvider


def test_metadata_retains_budgets_and_hashes_without_private_payload(account):
    user, _ = account
    run = accept(user, "hello", str(uuid4()))["run_id"]
    record_diagnostic(
        run,
        "context",
        {
            "estimated_input_tokens": 12000,
            "prompt_version": 4,
            "prompt_hash": "a" * 64,
            "system_prompt": "private instruction",
            "api_key": "private value",
            "stage": "fit",
            "call_id": "12345678-1234-abcd-1234-123456789abc",
            "fingerprints": ["1" * 64],
            "password": "private value",
        },
    )
    payload = json.loads(Trace.get().payload)
    assert payload["estimated_input_tokens"] == 12000
    assert payload["prompt_version"] == 4 and payload["prompt_hash"] == "a" * 64
    assert payload["call_id"] == "12345678-1234-abcd-1234-123456789abc"
    assert payload["fingerprints"] == ["1" * 64]
    assert "private" not in json.dumps(payload)


def test_error_metadata_does_not_capture_messages_or_unknown_details():
    try:
        raise ProviderError(
            "private upstream body",
            code="timeout",
            diagnostics={
                "phase": "notifications",
                "attempt": 2,
                "sdk_version": "0.163.0",
                "raw_response": "credential",
                "http_status": 503,
            },
        )
    except ProviderError as exc:
        result = error_metadata(exc)
    assert result["code"] == "timeout" and result["exception_class"] == "ProviderError"
    assert result["phase"] == "notifications" and result["http_status"] == 503
    assert "private" not in json.dumps(result) and "credential" not in json.dumps(result)
    assert result["frames"] == []  # Test frames are excluded from application locations.


def test_database_failure_falls_back_to_safe_structured_server_log(account, monkeypatch, caplog):
    user, _ = account
    run = accept(user, "hello", str(uuid4()))["run_id"]

    def broken(**kwargs):
        raise RuntimeError("private database exception")

    monkeypatch.setattr(Trace, "create", broken)
    record_diagnostic(run, "error", {"code": "timeout", "api_key": "private"})
    record_tool(run, "get_booking", "call", {}, {"password": "private"}, 3, Settings())
    assert len(caplog.records) == 2
    for record in caplog.records:
        payload = json.loads(record.message)
        assert payload["event"] == "diagnostic_persistence_failed" and payload["run_id"] == run
        assert "private" not in record.message


def test_tool_sizes_survive_trace_truncation(account):
    user, _ = account
    run = accept(user, "hello", str(uuid4()))["run_id"]
    result = ["value " * 1000] * 40
    record_tool(run, "get_booking", "call", {}, result, 2, Settings(trace_max_bytes=1024))
    row = Trace.get()
    payload = json.loads(row.payload)
    assert payload["output_bytes"] > 100000 and payload["output_items"] == 40
    assert payload["outputs"] == {"omitted": "trace byte limit"}
    assert len(row.payload.encode()) <= 1024
    assert tool_fingerprint("read", {"a": 1, "b": 2}) == tool_fingerprint("read", {"b": 2, "a": 1})
    assert tool_fingerprint("read", {"a": 1}) != tool_fingerprint("read", {"a": 2})


@pytest.mark.asyncio
async def test_http_failures_retain_status_and_attempt_without_response_body():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="private upstream body")
        )
    ) as client:
        provider = OpenRouterProvider(
            "test", "placeholder", client=client, limits=ProviderLimits(max_retries=0)
        )
        with pytest.raises(ProviderError) as caught:
            async for _ in provider.stream([], []):
                pass
    metadata = error_metadata(caught.value)
    assert metadata["http_status"] == 503 and metadata["attempt"] == 1
    assert "private" not in json.dumps(metadata)
    cal = CalClient(
        "placeholder",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, text="private upstream body")
        ),
    )
    try:
        with pytest.raises(CalError) as caught_cal:
            await cal.request("GET", "/v2/me")
        assert error_metadata(caught_cal.value)["http_status"] == 429
    finally:
        await cal.close()


def test_debug_timeline_has_stable_pagination_and_timestamps(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "test.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
        debug_api=True,
    )
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/auth/register",
            json={"email": "owner@example.com", "password": "long-password-123"},
        )
        run = client.post(
            "/api/chat/messages", json={"content": "hello", "client_request_id": str(uuid4())}
        ).json()["run_id"]
        with db.connection_context():
            for i in range(3):
                record_diagnostic(run, "error", {"step": i})
        first = client.get(f"/api/runs/{run}/debug?limit=2").json()
        second = client.get(f"/api/runs/{run}/debug?after={first[-1]['id']}&limit=2").json()
        assert len(first) == 2 and len(second) == 1
        assert first[-1]["id"] < second[0]["id"]
        assert all(isinstance(t["created_at"], float) for t in first + second)
        assert client.get(f"/api/runs/{run}/debug?limit=0").status_code == 422


@pytest.mark.asyncio
async def test_connection_open_failure_still_logs_original_safe_diagnostic(monkeypatch, caplog):
    from app.agent.tracing import record_async
    from app.db import database

    async def unavailable(*args, **kwargs):
        raise RuntimeError("private database connection details")

    monkeypatch.setattr(database, "query", unavailable)
    await record_async("test-run", "error", {"code": "timeout", "password": "private"})
    payload = json.loads(caplog.records[0].message)
    assert payload["event"] == "diagnostic_database_unavailable"
    assert payload["metadata"]["code"] == "timeout"
    assert "private" not in caplog.records[0].message
