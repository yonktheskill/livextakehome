"""Central application settings and feature flags.

Load once at startup and pass settings to consumers. Environment only: deliberately
never loads an existing dotenv file; Make/Compose explicitly supply .env.local.
The complete editable template is the repository's .env.example.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Storage, authentication and browser boundary.
    database_path: str = "data/app.sqlite3"
    master_secret: str = ""
    master_secret_previous: list[str] = Field(default_factory=list)
    public_origin: str = "http://localhost:3002"
    cookie_secure: bool = False
    session_days: int = 7
    # Only ingress peers that overwrite X-Real-IP may be listed here.
    trusted_ingress_cidrs: list[str] = Field(default_factory=list)

    # Provider selection and explicit local-only Codex opt-in.
    llm_provider: str = "codex"
    llm_model: str | None = None
    openrouter_api_key: str | None = None
    livex_codex_enabled: bool = False
    livex_codex_auth_home: str | None = None

    # Cal.com TLS trust; ambient HTTP proxies are still ignored.
    ssl_cert_file: str | None = None
    ssl_cert_dir: str | None = None

    # Runner feature flag and per-process/per-run limits.
    runner_enabled: bool = True
    max_concurrent_runs: int = Field(default=4, ge=1, le=64)
    max_model_steps: int = 12
    max_tool_calls: int = 24
    run_timeout_seconds: int = 180

    # Estimated token/cost limits; no paid summarizer or tokenizer dependency.
    # Half of gpt-6.1-sol's default Codex window for input, including schemas.
    # Override both limits when selecting a model with a different context window.
    context_window_tokens: int = Field(default=500000, ge=1024)
    context_input_tokens: int = Field(default=250000, ge=512)
    context_output_tokens: int = Field(default=4096, ge=1)
    context_safety_tokens: int = Field(default=1024, ge=0)
    context_tool_result_tokens: int = Field(default=2500, ge=0)
    context_tool_reserve_tokens: int = Field(default=1024, ge=0)
    context_bytes_per_token: float = Field(default=2.0, gt=0, le=4)
    context_compaction_threshold: float = Field(default=0.7, gt=0, le=1)
    context_intent_tokens: int = Field(default=1600, ge=0)
    context_summary_tokens: int = Field(default=1200, ge=0)
    context_retrieval_tokens: int = Field(default=1600, ge=0)

    # Diagnostics feature flag and trace payload limit.
    trace_max_bytes: int = Field(default=16000, ge=1024, le=100000)
    debug_api: bool = False
