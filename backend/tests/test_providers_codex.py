"""No credentials, external network, model service or Cal.com calls."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from app.providers import ProviderError, ProviderLimits
from app.providers import codex as adapter


def notification(method, payload):
    return SimpleNamespace(method=method, payload=SimpleNamespace(model_dump=lambda **_: payload))


async def collect(provider, tools=None):
    return [
        event
        async for event in provider.stream([{"role": "user", "content": "Hello"}], tools or [])
    ]


@pytest.fixture
def sdk(monkeypatch):
    pytest.importorskip("openai_codex")
    import openai_codex.client

    class Client:
        instances = []
        events = None
        failure = None
        account_type = "chatgpt"

        def __init__(self, config, approval_handler):
            self.config = config
            self.approval_handler = approval_handler
            self.closed = False
            self.instances.append(self)
            self.events = iter(
                type(self).events
                or [
                    notification(
                        "item/completed",
                        {
                            "item": {
                                "type": "agentMessage",
                                "text": json.dumps(
                                    {
                                        "assistant_text": "Hello",
                                        "tool_proposals": [],
                                    }
                                ),
                            }
                        },
                    ),
                    notification("turn/completed", {"turn": {"status": "completed"}}),
                ]
            )

        def start(self):
            if self.failure:
                raise self.failure

        def initialize(self):
            pass

        def account_read(self, params):
            assert params == {"refreshToken": False}
            from openai_codex.generated.v2_all import GetAccountResponse

            data = {"type": self.account_type}
            if self.account_type == "chatgpt":
                data.update(email=None, planType="plus")
            return GetAccountResponse.model_validate({"account": data, "requiresOpenaiAuth": True})

        def thread_start(self, params):
            self.thread_params = params
            return SimpleNamespace(thread=SimpleNamespace(id="thread"))

        def turn_start(self, thread_id, prompt, params):
            self.turn_params = params
            self.native_input = prompt
            self.prompt = json.loads(prompt[0]["text"] if isinstance(prompt, list) else prompt)
            return SimpleNamespace(turn=SimpleNamespace(id="turn"))

        def next_turn_notification(self, turn_id):
            return next(self.events)

        def close(self):
            self.closed = True

    monkeypatch.setattr(openai_codex.client, "CodexClient", Client)
    return Client


def provider(tmp_path, **kwargs):
    return adapter.CodexProvider("test-model", enabled=True, auth_home=str(tmp_path), **kwargs)


@pytest.mark.asyncio
async def test_pinned_construction_fresh_threads_and_scrubbed_launch(sdk, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "never-inherit")
    p = provider(tmp_path)
    assert [e.kind for e in await collect(p)] == ["text_delta", "done"]
    await collect(p)
    assert len(sdk.instances) == 2
    for c in sdk.instances:
        assert c.closed
        assert c.config.env is None
        args = c.config.launch_args_override
        assert args[:2] == ("/usr/bin/env", "-i")
        assert "never-inherit" not in repr(args)
        assert "--strict-config" in args
        assert 'permissions.inference.filesystem={":root"="deny"}' in args
        assert 'forced_login_method="chatgpt"' in args
        assert "features.shell_tool=false" in args
        assert "features.stable_environment_tools=false" in args
        assert "features.skip_host_skill_discovery=true" in args
        assert c.thread_params["environments"] == []
        assert c.thread_params["dynamicTools"] == []
        assert c.thread_params["permissions"] == "inference"
        assert c.thread_params["ephemeral"] is True
        assert c.thread_params["approvalPolicy"] == "never"
        assert c.turn_params["environments"] == []
        assert c.turn_params["outputSchema"]["additionalProperties"] is False
        assert c.approval_handler("item/fileChange/requestApproval", {}) == {"decision": "decline"}
    assert sdk.instances[0].config.cwd != sdk.instances[1].config.cwd


@pytest.mark.asyncio
async def test_batch_validated_before_any_event(sdk, tmp_path):
    sdk.events = [
        notification(
            "item/completed",
            {
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps(
                        {
                            "assistant_text": "Proposed",
                            "tool_proposals": [
                                {"name": "list_bookings", "arguments_json": "{}"},
                                {"name": "unknown", "arguments_json": "{}"},
                            ],
                        }
                    ),
                }
            },
        ),
        notification("turn/completed", {"turn": {"status": "completed"}}),
    ]
    seen = []
    with pytest.raises(ProviderError):
        async for event in provider(tmp_path).stream(
            [], [{"type": "function", "function": {"name": "list_bookings"}}]
        ):
            seen.append(event)
    assert seen == []
    assert sdk.instances[0].closed


@pytest.mark.parametrize(
    "raw",
    [
        '{"assistant_text":"x","assistant_text":"y","tool_proposals":[]}',
        '{"assistant_text":"x","tool_proposals":[{"name":"list","arguments_json":"[]"}]}',
        '{"assistant_text":"x","tool_proposals":[{"name":"list","arguments_json":"{\\"x\\":NaN}"}]}',
        '{"assistant_text":"x","tool_proposals":[],"extra":true}',
    ],
)
def test_strict_decision(raw):
    with pytest.raises(ValueError):
        adapter._decision(raw, ["list"], ProviderLimits())


def test_valid_proposals_only_and_unique_ids():
    raw = json.dumps(
        {"assistant_text": "", "tool_proposals": [{"name": "list", "arguments_json": "{}"}]}
    )
    a = adapter._decision(raw, ["list"], ProviderLimits())
    b = adapter._decision(raw, ["list"], ProviderLimits())
    assert a[0].name == "list" and a[0].arguments == {}
    assert a[0].call_id != b[0].call_id
    assert a[-1].kind == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("secret-upstream-detail"), asyncio.CancelledError()]
)
async def test_cleanup_and_sanitized_errors(sdk, tmp_path, failure):
    sdk.failure = failure
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await collect(provider(tmp_path))
    else:
        with pytest.raises(ProviderError) as error:
            await collect(provider(tmp_path))
        assert "secret-upstream-detail" not in str(error.value)
    assert sdk.instances[0].closed


@pytest.mark.asyncio
async def test_input_and_output_bounds(sdk, tmp_path):
    with pytest.raises(ProviderError) as error:
        await collect(provider(tmp_path, limits=ProviderLimits(max_input_bytes=1)))
    assert error.value.code == "invalid_request"
    assert not sdk.instances
    with pytest.raises(ProviderError):
        await collect(provider(tmp_path, limits=ProviderLimits(max_response_bytes=1)))
    assert sdk.instances[0].closed


@pytest.mark.asyncio
async def test_disabled_and_version_gate_before_import(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter, "version", lambda _: "unexpected")
    with pytest.raises(ProviderError) as error:
        await collect(provider(tmp_path))
    assert error.value.code == "unsafe_runtime"


@pytest.mark.asyncio
async def test_actual_runtime_has_zero_tools_and_structured_output(monkeypatch, tmp_path):
    """Pinned binary -> loopback fake Responses endpoint. No LLM/account required."""
    pytest.importorskip("openai_codex")
    if adapter.version("openai-codex") != adapter.SDK_VERSION:
        pytest.skip("requires pinned optional SDK")
    requests = []
    text = json.dumps({"assistant_text": "Offline fixture", "tool_proposals": []})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            item = {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            events = [
                ("response.created", {"response": {"id": "resp_fixture", "status": "in_progress"}}),
                (
                    "response.output_item.added",
                    {"output_index": 0, "item": {**item, "content": []}},
                ),
                (
                    "response.output_text.delta",
                    {
                        "item_id": "msg_fixture",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text,
                    },
                ),
                ("response.output_item.done", {"output_index": 0, "item": item}),
                (
                    "response.completed",
                    {
                        "response": {
                            "id": "resp_fixture",
                            "status": "completed",
                            "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        }
                    },
                ),
            ]
            for kind, data in events:
                self.wfile.write(
                    f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()
                )
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = adapter._config

    def config(directory, auth_home):
        c = original(directory, auth_home)
        # Override only the test runtime. The production adapter fixes OpenAI and
        # ChatGPT login; it exposes no arbitrary endpoint or paid-key option.
        c.launch_args_override += (
            "-c",
            'forced_login_method="api"',
            "-c",
            'model_provider="fixture"',
            "-c",
            'model_providers.fixture={name="fixture",base_url="http://127.0.0.1:'
            + str(server.server_port)
            + '",wire_api="responses",requires_openai_auth=false,'
            "request_max_retries=0,stream_max_retries=0}",
        )
        return c

    import openai_codex.client

    real_client = openai_codex.client.CodexClient

    class FixtureClient(real_client):
        def account_read(self, params):
            from openai_codex.generated.v2_all import GetAccountResponse

            return GetAccountResponse.model_validate(
                {
                    "account": {"type": "chatgpt", "email": None, "planType": "plus"},
                    "requiresOpenaiAuth": True,
                }
            )

        def thread_start(self, params):
            return super().thread_start({**params, "modelProvider": "fixture"})

    monkeypatch.setattr(openai_codex.client, "CodexClient", FixtureClient)
    monkeypatch.setattr(adapter, "_config", config)
    try:
        events = await collect(provider(tmp_path, limits=ProviderLimits(timeout_seconds=10)))
        assert events[0].text == "Offline fixture"
        assert events[-1].kind == "done"
        assert len(requests) == 1
        assert requests[0]["tools"] == []
        assert requests[0]["text"]["format"]["type"] == "json_schema"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_timeout_closes_blocked_reader(sdk, tmp_path, monkeypatch):
    from threading import Event

    stopped = Event()
    original_close = sdk.close

    def blocked(self, turn_id):
        stopped.wait(2)
        raise RuntimeError("reader stopped")

    def close(self):
        original_close(self)
        stopped.set()

    monkeypatch.setattr(sdk, "next_turn_notification", blocked)
    monkeypatch.setattr(sdk, "close", close)
    with pytest.raises(ProviderError) as error:
        await collect(provider(tmp_path, limits=ProviderLimits(timeout_seconds=0.05)))
    assert error.value.code == "timeout"
    assert stopped.is_set()
    assert sdk.instances[0].closed


@pytest.mark.asyncio
async def test_runtime_tool_item_is_rejected(sdk, tmp_path):
    sdk.events = [notification("item/completed", {"item": {"type": "commandExecution"}})]
    with pytest.raises(ProviderError):
        await collect(provider(tmp_path))
    assert sdk.instances[0].closed


def test_shutdown_before_start_prevents_launch():
    class Client:
        def start(self):
            pytest.fail("must not launch after shutdown")

        def close(self):
            pass

    process = adapter._Process(Client())
    process.close()
    process.start()


@pytest.mark.asyncio
async def test_api_key_account_rejected_before_thread(sdk, tmp_path):
    sdk.account_type = "apiKey"
    with pytest.raises(ProviderError) as error:
        await collect(provider(tmp_path))
    assert error.value.code == "account_required"
    assert not hasattr(sdk.instances[0], "thread_params")
    assert sdk.instances[0].closed


@pytest.mark.asyncio
async def test_native_images_are_not_stringified_or_given_tools(sdk, tmp_path):
    from openai_codex.generated.v2_all import ImageUserInput

    from app.providers.images import split_images

    p = provider(tmp_path, limits=ProviderLimits(max_input_bytes=1000))
    url = "data:image/png;base64," + "eA=="
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read scheduling facts"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    assert split_images(messages, p.limits)[1] == [url]
    _ = [event async for event in p.stream(messages, [])]
    client = sdk.instances[-1]
    assert client.native_input[1] == {"type": "image", "url": url}
    assert ImageUserInput.model_validate(client.native_input[1]).url == url
    assert url not in json.dumps(client.prompt)
    assert client.prompt["tools"] == []
    assert client.thread_params["dynamicTools"] == []
    assert client.thread_params["environments"] == []
    assert client.closed
