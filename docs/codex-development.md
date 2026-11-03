# Account-backed Codex development adapter

The opt-in adapter now performs structured inference through the official Python
SDK and its pinned local CLI. It does not execute Cal.com operations. The app's
runner supplies credential-free conversation history and tool definitions; only
its normal executor may validate and dispatch the returned proposals.

This supersedes the disabled-Codex section in `provider-notes.md`. Production
OpenRouter selection is unchanged, and Codex never selects OpenRouter, an API key,
another model, or an offline fake after failure.

## Install and sign in yourself

Use Python >=3.12 on macOS or Linux. Install the optional development dependency
`openai-codex==0.163.0` in the backend environment. Its bundled
`openai-codex-cli-bin` must also be exactly 0.163.0. Do not update either without
rerunning the real-runtime contract test and reviewing the tool registry.

Choose a **dedicated**, private account-login directory outside the repository.
Do not point this adapter at your ordinary Codex home. Perform ChatGPT account
login yourself; implementation and tests do not sign in or inspect credentials.
For example, run this script in the SDK environment, setting the absolute
`login_dir` first:

```python
from pathlib import Path
from importlib.metadata import distribution
from openai_codex import Codex, CodexConfig

login_dir = Path("/absolute/private/livex-codex-login")
login_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
runtime = distribution("openai-codex-cli-bin")
binary = next(runtime.locate_file(f) for f in runtime.files
              if str(f).endswith("/bin/codex"))
args = (
    "/usr/bin/env", "-i", f"HOME={login_dir}", f"CODEX_HOME={login_dir}",
    "PATH=/usr/bin:/bin", str(binary), "app-server", "--strict-config",
    "-c", 'cli_auth_credentials_store="file"',
    "-c", 'forced_login_method="chatgpt"',
)
with Codex(CodexConfig(launch_args_override=args, cwd=str(login_dir))) as codex:
    login = codex.login_chatgpt_device_code()
    print(login.verification_url, login.user_code)  # use privately; do not log/share
    result = login.wait()
    if not result.success:
        raise RuntimeError("Account login did not complete")
```

Device-code sign-in availability depends on account/workspace settings. This step
only authenticates; it does not start inference. The runtime writes its account
login file in that directory. Protect it like a credential. Do not put Cal.com
keys, API keys, project configuration, MCP definitions or skills there.

Enable the factory path explicitly in your backend process:

```sh
export LIVEX_CODEX_ENABLED=1
export LIVEX_CODEX_AUTH_HOME=/absolute/private/livex-codex-login
```

Select provider `codex` using the application's existing provider setting and
supply an explicit account-supported model. The adapter does not choose one.
Alternatively construct `CodexProvider(model, enabled=True, auth_home=absolute_path)`.
Missing login, unavailable models, account limits and other failures produce a
sanitized provider error. Account inference uses ChatGPT/Codex allowances; this
is not offline inference or a promise of unlimited/free account access. No live
account/model smoke test was performed during implementation.

## Enforced inference boundary

Each application step starts a new dedicated SDK process, private temporary
HOME/CODEX_HOME/cwd and ephemeral thread. Only an `auth.json` symlink points to the
dedicated account-login file; the adapter never reads or copies its contents.
No existing thread, project home, project instructions or developer Codex config
is resumed. Authentication refreshes that replace the symlink inside the temporary
home are discarded at shutdown; account expiry/rotation may require signing in
again. This implementation does not copy refreshed tokens back into the login home.

The low-level `CodexClient` is intentional. In 0.163.0:

- The high-level `AsyncCodex.thread_start()` does not expose `environments`.
  The real lower-level `thread/start` and `turn/start` parameters do.
  Both receive `environments: []`. The pinned tool registry omits environment
  tools, including `apply_patch`, when no environment is selected and
  `features.stable_environment_tools=false`.
- Shell, browser/computer, image, code-mode, collaboration, apps/plugins,
  hooks, skill discovery/search/install, memories and workspace-dependency
  features are disabled. MCP configuration is empty; web search is disabled.
  Bundled skills and automatic skill instructions are disabled; project document
  loading has a zero-byte budget. Dynamic tools are empty: application tools are
  proposals in structured JSON, never SDK-executable tools.
- A named permission profile denies filesystem reads and writes at `:root`, and
  disables tool network access. No unrelated full-filesystem read grant is used.
  The SDK process still needs its own trusted runtime/authentication and inference
  network connection. The permission profile applies to model execution, not to
  every filesystem operation of the trusted app-server process.
- `approvalPolicy=never` is paired with an explicit declining approval handler.
  The SDK's default handler **accepts** shell/file requests; it is not used.
- `CodexConfig.env` merges with the parent environment. The launcher instead uses
  absolute `/usr/bin/env -i` before the bundled CLI. API keys, Cal.com credentials,
  proxy settings, ordinary HOME/CODEX_HOME and other parent environment values do
  not reach the CLI. No global Python environment mutation is needed.
- Forced ChatGPT login, an explicit account-type check before thread creation,
  and the built-in OpenAI model provider prevent API-key or
  arbitrary-provider fallback. Production exposes no custom endpoint option.

The process itself is trusted local code, not an OS-isolated adversarial service.
For stronger isolation, run development inference in a dedicated container/user
with no project, database, secret or Docker-socket mounts, a separate login volume,
and inference/authentication egress only. Exchange only credential-free JSON with
that service. Running a default read-only Codex session beside backend secrets is
not an equivalent substitute for the adapter controls.

## Decision validation and bounds

The output schema contains `assistant_text` and a bounded `tool_proposals` list.
Each proposal names a supplied application tool and carries a JSON object encoded
as `arguments_json`. A string avoids weakening strict output schemas for arbitrary
application tool parameter shapes. The adapter rejects extra fields, unknown
names, duplicate JSON keys, nonfinite numbers, nonobject arguments, oversized
responses and incomplete turns. It validates the complete batch before emitting
any text/tool events. Every proposal receives a fresh application call ID.
The app executor still owns full per-tool schema, ownership, confirmation and
mutation checks.

Default input bound is 256 KB, observed notification/decision bound 512 KB, eight
proposals and 60 seconds per step. Set `ProviderLimits` on direct construction to
change these bounds. Output arrives as one validated text event, not incremental
token streaming. No usage event/token-counting capability is claimed.

The SDK offers no verified `max_output_tokens` or internal `max_steps` parameter.
`ProviderLimits.max_output_tokens` therefore does not impose a Codex token ceiling.
With no executable model tools, each thread receives one inference turn; model
reasoning may still consume account tokens. The wall-clock deadline and observed
byte bound close the dedicated process. Closing also handles task cancellation;
startup/shutdown are ordered to prevent a late process launch after cancellation.
SDK close can add roughly its two-second process termination grace period.
Stopping local transport does not guarantee immediate remote compute cancellation.
SDK JSON-line decoding/queues allocate before the adapter observes notifications:
the byte bound is not an OS memory limit. Use process/container resource limits
if a hard RSS bound is required. There are no application-level inference retries.

## Offline evidence and checks

`test_providers_codex.py` includes a real pinned SDK/CLI test against a loopback
HTTP fixture, with an empty temporary login directory, scrubbed environment and
no account/model service. It captures the actual Responses request and asserts
`tools: []` and a structured output schema, then returns synthetic SSE output
through the real runtime and checks the resulting provider events. The test-only
custom provider cannot be selected through the production adapter. Other tests
cover exact launch/thread/turn parameters, fresh processes, strict batch parsing,
unknown tool items, bounds, timeout, cancellation, cleanup and startup ordering.
No auth files, `.env`, keys or paid/model endpoints are read or called by these tests.

```sh
PYTHONPATH=backend uv run --no-project --with openai-codex==0.163.0 --with httpx --with pytest --with pytest-asyncio pytest --noconftest -q -o asyncio_mode=auto -o asyncio_default_fixture_loop_scope=function backend/tests/test_providers*.py
uv run --no-project --with ruff ruff check backend/app/providers/codex.py backend/tests/test_providers_codex.py
uv run --no-project --with ruff ruff format --check backend/app/providers/codex.py backend/tests/test_providers_codex.py
PYTHONPATH=backend uv run --no-project --with mypy --with httpx --with openai-codex==0.163.0 mypy --config-file backend/pyproject.toml backend/app/providers/codex.py
```

## Sources inspected 2026-11-03

OpenAI Docs skill applied; official pages were fetched before runtime investigation:
[SDK](https://learn.chatgpt.com/docs/codex-sdk),
[configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[permissions](https://learn.chatgpt.com/docs/permissions).
Exact API and tool selection were checked against installed 0.163.0 Python source,
its real bundled CLI, and the corresponding official source:
[thread environment controls](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs),
[tool registry gates](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/src/tools/spec_plan.rs).
The newer Rust-only `ToolPolicy.allowed_tools` ceiling is not exposed by this
Python/app-server interface; this adapter uses the verified environment and feature
controls instead. Recheck these surfaces before changing the pinned runtime.
