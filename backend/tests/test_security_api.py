from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import KeyVault, digest, passwords, verify_password
from app.db.models import Session
from app.main import create_app


def test_password_salts_vault_binding_and_missing_secret():
    first, second = passwords.hash("a strong password"), passwords.hash("a strong password")
    assert first != second and first.startswith("$argon2id$")
    assert verify_password(first, "a strong password")
    assert not verify_password(first, "wrong")
    vault = KeyVault(Fernet.generate_key().decode())
    ciphertext = vault.seal("not-real", 1, 42)
    assert "not-real" not in ciphertext
    assert vault.open(ciphertext, 1, 42) == "not-real"
    with pytest.raises(RuntimeError):
        vault.open(ciphertext, 2, 42)
    with pytest.raises(RuntimeError, match="MASTER_SECRET"):
        KeyVault("")


def test_auth_csrf_dedupe_ownership_history_and_replay(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "api.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/auth/me").status_code == 401
        credentials = {"email": "alice@example.com", "password": "long-password-123"}
        assert (
            client.post(
                "/api/auth/register",
                json=credentials,
                headers={"Origin": "https://attacker.example"},
            ).status_code
            == 403
        )
        response = client.post("/api/auth/register", json=credentials)
        assert response.status_code == 200
        raw = client.cookies.get("cal_session")
        assert "HttpOnly" in response.headers["set-cookie"]
        from app.db.models import db

        with db.connection_context():
            assert Session.get().token_hash == digest(raw)
        assert client.get("/api/auth/me").json()["email"] == credentials["email"]
        msg = {"content": "hello", "client_request_id": str(uuid4())}
        first = client.post("/api/chat/messages", json=msg).json()
        assert client.post("/api/chat/messages", json=msg).json() == first
        history = client.get("/api/chat/history").json()
        assert len(history["messages"]) == 1
        with db.connection_context():
            from app.db.repository import emit, finish
            from app.db.requests import resolve_requests

            resolve_requests(
                first["run_id"],
                first["message_id"],
                [{"relation": "dismiss", "reason": "Greeting only"}],
            )

            emit(first["run_id"], "tool.status", {"text": "test"})
            finish(first["run_id"], "succeeded", "run.completed", {})
        replay = client.get(f"/api/runs/{first['run_id']}/events", headers={"Last-Event-ID": "1"})
        assert "event: run.completed" in replay.text and "id: 1\n" not in replay.text
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401
        client.post(
            "/api/auth/register", json={"email": "bob@example.com", "password": "long-password-123"}
        )
        assert client.get(f"/api/runs/{first['run_id']}").status_code == 404
        assert client.get(f"/api/runs/{first['run_id']}/events").status_code == 404
        assert client.post("/api/connection", json={}).status_code == 405
        invalid = client.put("/api/connection", json={"api_key": "short"})
        assert invalid.status_code == 422 and "short" not in invalid.text


def test_master_key_rotation_without_plaintext_exposure():
    previous, current = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old = KeyVault(previous).seal("placeholder-only", 1, 42)
    rotating = KeyVault(current, [previous])
    assert rotating.open(old, 1, 42) == "placeholder-only"
    new = rotating.seal(rotating.open(old, 1, 42), 1, 42)
    assert KeyVault(current).open(new, 1, 42) == "placeholder-only"
    with pytest.raises(RuntimeError):
        KeyVault(previous).open(new, 1, 42)


def test_default_codex_startup_guidance_is_fail_closed(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "codex.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        llm_provider="codex",
        livex_codex_enabled=False,
    )
    with pytest.raises(RuntimeError, match="explicit development opt-in"):
        with TestClient(create_app(settings)):
            pass
