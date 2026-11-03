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
    # Estimated token/cost limits; no paid summarizer or tokenizer dependency.
    context_window_tokens: int = Field(default=32768, ge=1024)
    context_input_tokens: int = Field(default=8000, ge=512)
    context_output_tokens: int = Field(default=4096, ge=1)
    context_safety_tokens: int = Field(default=1024, ge=0)
    context_tool_reserve_tokens: int = Field(default=1024, ge=0)
    context_bytes_per_token: float = Field(default=2.0, gt=0, le=4)
    context_compaction_threshold: float = Field(default=0.7, gt=0, le=1)
    context_summary_tokens: int = Field(default=1200, ge=0)
    context_retrieval_tokens: int = Field(default=1600, ge=0)
    trace_max_bytes: int = Field(default=16000, ge=1024, le=100000)
    debug_api: bool = False
