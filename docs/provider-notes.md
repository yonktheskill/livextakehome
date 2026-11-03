# Provider implementation notes

Implemented only the application provider boundary; adapters never access a database,
Cal.com, a shell, account credentials, or `.env`. Application history is authoritative.
`create_provider(name, model=None, api_key=None)` supports explicit `fake`,
`openrouter`, and `codex` selection. No provider is silently substituted.

## Dependencies and integration

Backend owner: require Python >=3.11 (the backend currently targets >=3.12) and
`httpx>=0.28,<1` for OpenRouter. Test dependencies: `pytest>=8`,
`pytest-asyncio>=0.25`; checks use Ruff and mypy. No pyproject/lockfile changes were
made by this task. No JSON-schema runtime dependency is required: the app's tool
executor remains responsible for full per-tool schema and authorization validation.

The optional official SDK inspected was `openai-codex==0.163.0`, whose import is
`openai_codex`, **not** `codex`. It installs a pinned `openai-codex-cli-bin` runtime
(approximately 119 MB on this machine), Pydantic and its other transitive dependencies.
Do not add it to the base production install. If retaining the development probe,
add an optional `codex` extra pinned to `openai-codex==0.163.0`.
Neither fake nor OpenRouter imports the SDK or needs that extra.

Factory selection does not read environment variables. The backend must pass its
configured project model/key explicitly. OpenRouter rejects a missing model/key;
there is deliberately no default paid model. The fake defaults to a clearly labelled
offline response and executes nothing. Tests can inject `FakeProvider(steps=...)`
with one deterministic event batch per model step; it records deep-copied requests
and raises on exhaustion. It is not a natural-language scheduling simulator.

## OpenRouter

`OpenRouterProvider(model, api_key, limits=ProviderLimits(), client=None)` accepts
an injected HTTPX client for offline testing. A provided client remains caller-owned;
`aclose()` closes only an internally owned client. Internally owned clients disable
ambient proxies and redirects. Only the fixed HTTPS Chat Completions endpoint is
used. Errors contain fixed application-authored text; upstream HTTP bodies, error
messages, URLs, headers and arbitrary usage metadata are not forwarded or logged.
Model output itself is untrusted text and may contain user-supplied personal data.
The runner must assemble a credential-free context and apply its normal output policy.

Default per-step limits: 2,048 output tokens, eight tool calls, 256,000 input bytes,
512,000 response bytes, 60 seconds including retries, two retries with 0.25-second
exponential backoff. Set another immutable `ProviderLimits` when constructing the
adapter to change these values; the contract's three-argument factory uses defaults.
Run-level model-step/tool/time limits remain the runner's responsibility.

Requests require supported routing parameters, deny data collection, and disable
OpenRouter provider fallbacks. This may reduce model availability. The application
must select and evaluate an appropriate tool-capable model explicitly; no live model
availability or scheduling quality was evaluated in this task.

The adapter handles SSE comments/metadata, multiline data, split UTF-8, fragmented
function names/IDs/JSON arguments across multiple indexed calls, empty-choice usage
frames and repeated terminal finish reasons in accounting frames. It supports text
content only and declares no image/token-counting/structured-output capability.
It emits text deltas immediately. It emits tool requests **only after** a successful
finish reason and `[DONE]`, validating the entire batch first. Arguments must be a
strict JSON object (no duplicate keys or non-finite numbers), names must match
supplied tools and call IDs must be nonempty and unique. Schema/ownership checks
and serial mutation dispatch still belong to the executor.

Truncation, malformed JSON, invalid calls, missing completion markers, upstream
stream errors and size violations raise sanitized `ProviderError`, without tool
proposals or `done`. Already-streamed text can precede a failure; the runner must
mark that run failed. Usage is emitted once with supported token counts only.
HTTP 429/500/502/503/504 and transport errors can retry within the bound **only before
any event has been emitted**. Inference retries may still consume provider tokens;
they cannot replay application tool side effects. Cancellation propagates unchanged
and releases the HTTP response; disconnecting may not stop upstream billing for
all OpenRouter providers. There is no automatic retry of malformed streams or
mutations, and no Cal.com request in this package.

## Codex: explicit unsupported/unsafe-runtime result

**The Codex development path is intentionally disabled. It is not a working model
adapter, and no live isolation, account access or model execution has been proved.**
`CodexProvider.stream()` checks installed distribution metadata only, then raises
`sdk_unavailable` or `unsafe_runtime`. It never imports/constructs a runtime client,
reads sign-in files, accepts API keys, starts a subprocess, or falls back to a paid
API. This satisfies the permitted fail-closed outcome; it does not satisfy live
Codex inference or streaming parity.

Inspection of actual 0.163.0 installed Python source and signatures established:

- `AsyncCodex(CodexConfig(...))` controls a local app-server. Entering its context
  initializes the runtime; this was **not** done.
- `CodexConfig` accepts `env`, `cwd`, and `config_overrides`. These are configuration
  surfaces, not proof that host files or model tools are inaccessible.
- `AsyncCodex.thread_start` accepts `ephemeral`, `base_instructions`,
  `developer_instructions`, model and sandbox parameters, supporting fresh context.
- `AsyncThread.run` accepts `output_schema`, but exposes no explicit `max_steps`,
  `max_output_tokens`, or per-call model-tool allowlist parameters. That is a finding
  about this high-level API, not proof that no lower-level solution can exist.
- Documented read-only sandbox mode still permits reading host files; approval
  mode by itself does not prevent reads or tools permitted by a sandbox.
- App-server dynamic tool integration is experimental. Merely supplying a schema
  or asking the model not to use shell tools does not enforce this task's boundary.

A future enablement needs a verified denial of **all** built-in shell/filesystem,
network and integration tools, a scrubbed process environment, no access to project
secrets/config, a narrowly scoped developer authentication mechanism, bounded
internal inference/output, cancellation cleanup and mocked contract tests for the
actual enforcement surface. Each model step must use a fresh thread and a structured
next-decision schema containing only text and named application-tool proposals;
the app must validate and execute every proposal. Do not enable based on prompt
instructions, a read-only working directory, or a mocked successful completion alone.
The current tests verify the failure boundary and inspect official API shapes only;
they do not claim runtime isolation or structured-decision execution.

## Sources checked 2026-11-03

Applied the OpenAI Docs skill and fetched current official documentation, followed
by credential-free package inspection. No Codex/OpenRouter inference was run.

- [Official Codex SDK](https://learn.chatgpt.com/docs/codex-sdk): Python SDK,
  async usage, pinned local runtime, thread and sandbox semantics.
- [Official app-server](https://learn.chatgpt.com/docs/app-server): structured turn
  output, item events, permissions and experimental dynamic tools.
- [OpenRouter streaming](https://openrouter.ai/docs/api-reference/streaming): SSE
  comments, accounting frames, terminal errors and cancellation limitations.
- [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection):
  parameter support, fallback and data-collection controls.

## Offline verification

From repository root (no database or runtime started):

```sh
PYTHONPATH=backend uv run --no-project --with openai-codex==0.163.0 --with httpx --with pytest --with pytest-asyncio pytest --noconftest -q -o asyncio_mode=auto -o asyncio_default_fixture_loop_scope=function backend/tests/test_providers*.py
uv run --no-project --with ruff ruff check backend/app/providers backend/tests/test_providers*.py
uv run --no-project --with ruff ruff format --check backend/app/providers backend/tests/test_providers*.py
uv run --no-project --with mypy --with httpx mypy --config-file backend/pyproject.toml backend/app/providers
```

The optional SDK test imports and inspects classes only. Without the optional SDK,
that inspection test skips; fail-closed behaviour is still tested using monkeypatched
metadata. All HTTP tests use HTTPX MockTransport and placeholder credentials.

Final provider-only result: 34 tests passed with the SDK installed; Ruff lint/format
and mypy passed. `--noconftest` deliberately excludes concurrently implemented
backend integration fixtures, keeping these tests independent of DB/auth packages.
