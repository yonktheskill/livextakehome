"""Environment only: deliberately never loads an existing dotenv file."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    database_path: str = "data/app.sqlite3"
    master_secret: str = ""
    master_secret_previous: list[str] = Field(default_factory=list)
    public_origin: str = "http://localhost:3000"
    cookie_secure: bool = False
    session_days: int = 7
    llm_provider: str = "codex"
    llm_model: str | None = None
    openrouter_api_key: str | None = None
    runner_enabled: bool = True
    max_model_steps: int = 12
    max_tool_calls: int = 24
    run_timeout_seconds: int = 180
    debug_api: bool = False
