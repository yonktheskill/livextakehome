import json
import os
import stat

from app.container_entrypoint import provision_codex_login


def test_bootstrap_login_is_private_consumed_and_never_overwritten(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("LIVEX_CODEX_AUTH_HOME", str(home))
    monkeypatch.setenv("CODEX_LOGIN_BOOTSTRAP", json.dumps({"test": "first"}))
    provision_codex_login()
    target = home / "auth.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "CODEX_LOGIN_BOOTSTRAP" not in os.environ
    monkeypatch.setenv("CODEX_LOGIN_BOOTSTRAP", json.dumps({"test": "second"}))
    provision_codex_login()
    assert json.loads(target.read_text()) == {"test": "first"}
