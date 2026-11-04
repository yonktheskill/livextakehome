"""Configuration parsing and explicit provider wiring; no inference or login."""

import re
import shutil
import stat
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.providers import factory
from app.providers.codex import SDK_VERSION, CodexProvider
from app.providers.fake import FakeProvider


@pytest.mark.parametrize(
    "value, expected", [("1", True), ("true", True), ("0", False), ("false", False)]
)
def test_feature_flags_and_concurrency_use_settings_environment(monkeypatch, value, expected):
    monkeypatch.setenv("LIVEX_CODEX_ENABLED", value)
    monkeypatch.setenv("RUNNER_ENABLED", value)
    monkeypatch.setenv("DEBUG_API", value)
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "6")
    settings = Settings()
    assert settings.livex_codex_enabled is expected
    assert settings.runner_enabled is expected
    assert settings.debug_api is expected
    assert settings.max_concurrent_runs == 6


@pytest.mark.parametrize("value", ["0", "65"])
def test_concurrency_environment_preserves_bounds(monkeypatch, value):
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", value)
    with pytest.raises(ValidationError, match="max_concurrent_runs"):
        Settings()


def test_codex_is_off_by_default_and_dotenv_files_are_not_loaded(tmp_path, monkeypatch):
    for key in ("LIVEX_CODEX_ENABLED", "LIVEX_CODEX_AUTH_HOME", "MAX_CONCURRENT_RUNS", "DEBUG_API"):
        monkeypatch.delenv(key, raising=False)
    content = (
        "LIVEX_CODEX_ENABLED=1\nLIVEX_CODEX_AUTH_HOME=/dotenv/login\n"
        "MAX_CONCURRENT_RUNS=1\nDEBUG_API=true\n"
    )
    (tmp_path / ".env").write_text(content)
    (tmp_path / ".env.local").write_text(content)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.livex_codex_enabled is False
    assert settings.livex_codex_auth_home is None
    assert settings.max_concurrent_runs == 4
    assert settings.debug_api is False


def test_provider_and_factory_do_not_implicitly_read_codex_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVEX_CODEX_ENABLED", "1")
    monkeypatch.setenv("LIVEX_CODEX_AUTH_HOME", str(tmp_path / "ambient"))
    for provider in (CodexProvider("test-model"), factory.create_provider("codex", "test-model")):
        assert provider.enabled is False
        assert provider.auth_home is None
    home = str(tmp_path / "explicit")
    provider = factory.create_provider(
        "codex", "test-model", codex_enabled=True, codex_auth_home=home
    )
    assert provider.enabled is True
    assert provider.auth_home == Path(home)


def test_app_uses_explicit_codex_settings_over_disabled_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVEX_CODEX_ENABLED", "0")
    monkeypatch.setenv("LIVEX_CODEX_AUTH_HOME", "relative-ambient-path")
    monkeypatch.setattr(metadata, "version", lambda _: SDK_VERSION)
    provider = FakeProvider()
    captured = {}

    def create_provider(name, **kwargs):
        captured.update(name=name, **kwargs)
        return provider

    monkeypatch.setattr(factory, "create_provider", create_provider)
    home = str(tmp_path / "explicit-login")
    settings = Settings(
        database_path=str(tmp_path / "app.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=True,
        llm_provider="codex",
        llm_model="test-model",
        livex_codex_enabled=True,
        livex_codex_auth_home=home,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        assert captured["name"] == "codex"
        assert captured["model"] == "test-model"
        assert captured["codex_enabled"] is True
        assert captured["codex_auth_home"] == home
    assert provider._closed
    assert not provider.requests


def test_explicit_disabled_setting_cannot_be_enabled_by_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVEX_CODEX_ENABLED", "1")
    monkeypatch.setenv("LIVEX_CODEX_AUTH_HOME", str(tmp_path / "ambient-login"))
    settings = Settings(
        database_path=str(tmp_path / "disabled.sqlite3"),
        master_secret=Fernet.generate_key().decode(),
        runner_enabled=True,
        llm_provider="codex",
        llm_model="test-model",
        livex_codex_enabled=False,
    )
    with pytest.raises(RuntimeError, match="explicit development opt-in"):
        with TestClient(create_app(settings)):
            pass


def test_shared_template_lists_every_backend_setting():
    template = Path(__file__).resolve().parents[2] / ".env.example"
    # Optional TLS settings remain commented so local setup preserves ambient CA roots.
    names = re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", template.read_text(), re.MULTILINE)
    assert {name.upper() for name in Settings.model_fields} <= set(names)
    assert len(names) == len(set(names))


def test_init_dev_uses_complete_template_and_never_overwrites_config(tmp_path):
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "init_dev.py"
    shutil.copyfile(root / "scripts" / "init_dev.py", script)
    shutil.copyfile(root / ".env.example", tmp_path / ".env.example")
    # A new flag needs only a template entry to be included by local initialization.
    with (tmp_path / ".env.example").open("a") as template:
        template.write("FUTURE_FEATURE_ENABLED=false\n")
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    destination = tmp_path / ".env.local"
    content = destination.read_text()
    original = content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    values = dict(
        line.split("=", 1) for line in content.splitlines() if line and not line.startswith("#")
    )
    Fernet(values["MASTER_SECRET"].encode())
    assert values["MASTER_SECRET"] not in result.stdout + result.stderr
    assert Path(values["LIVEX_CODEX_AUTH_HOME"]).is_absolute()
    for name in ("MAX_CONCURRENT_RUNS", "LIVEX_CODEX_ENABLED", "FUTURE_FEATURE_ENABLED"):
        assert name in values
    template = (tmp_path / ".env.example").read_text()
    for name in ("MASTER_SECRET", "LIVEX_CODEX_AUTH_HOME"):
        content = content.replace(f"{name}={values[name]}\n", f"{name}=\n")
    assert content == template
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "left unchanged" in result.stderr
    assert destination.read_text() == original
