import asyncio
import json
import logging
import time

from app.agent.context import assemble, authorized, retrieve
from app.db.database import query
from app.db.models import Message, Run, Trace, db
from app.db.repository import checkpoint, claim_run, cleanup, emit, finish, mark_uncertain, recover
from app.tools.executor import Executor, Interrupted, UnknownOutcome, Waiting
from app.tools.schemas import definitions

log = logging.getLogger("agent")


class Runner:
    """Single supervised consumer, bounded steps/time, independent of SSE connection."""

    def __init__(self, settings, vault, provider):
        self.settings, self.provider = settings, provider
        self.executor = Executor(vault)
        self.stopping = asyncio.Event()

    async def supervise(self):
        await query(recover)
        last_cleanup = 0.0
        while not self.stopping.is_set():
            try:
                if time.monotonic() - last_cleanup > 3600:
                    await query(cleanup)
                    last_cleanup = time.monotonic()
                run_id = await query(claim_run)
                if run_id:
                    await self.execute(run_id)
                else:
                    try:
                        await asyncio.wait_for(self.stopping.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("Runner supervisor recovered from internal error")
                await asyncio.sleep(0.25)

    async def execute(self, run_id):
        try:
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                await self.loop(run_id)
        except Waiting as exc:
            await query(self.append, run_id, str(exc))
            await query(finish, run_id, "waiting_for_user", "run.waiting", {"message": str(exc)})
        except Interrupted:
            await query(finish, run_id, "interrupted", "run.interrupted", {})
        except (UnknownOutcome, TimeoutError, asyncio.CancelledError) as exc:
            # A timeout/cancellation can occur after dispatch: preserve unknown evidence.
            unknown = await query(mark_uncertain, run_id)
            await query(
                finish,
                run_id,
                "needs_reconciliation" if unknown else "failed",
                "run.failed",
                {
                    "message": "Outcome unknown; inspect Cal.com before changing "
                    "this booking again."
                    if unknown
                    else "Run stopped or reached its time limit."
                },
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
        except Exception:
            unknown = await query(mark_uncertain, run_id)
            await query(
                finish,
                run_id,
                "needs_reconciliation" if unknown else "failed",
                "run.failed",
                {"message": "Agent failed. Check provider configuration and try again."},
            )
            log.error("Agent run failed", extra={"run_id": run_id})

    @staticmethod
    def append(run_id, text):
        with db.atomic("IMMEDIATE"):
            run = Run.get_by_id(run_id)
            msg, _ = Message.get_or_create(
                conversation=run.conversation_id,
                client_request_id=f"assistant:{run_id}",
                defaults={"role": "assistant", "status": "partial"},
            )
            msg.content += text
            msg.save()
            emit(run_id, "message.delta", {"message_id": msg.id, "text": text})

    async def loop(self, run_id):
        if not await query(checkpoint, run_id):
            raise Interrupted()
        pending = await query(authorized, run_id)
        messages = await query(assemble, run_id)
        if pending:
            result = await self.executor.execute(run_id, *pending)
            messages.append(
                {"role": "system", "content": "Confirmed action result: " + json.dumps(result)}
            )
        tools = definitions() + [
            {
                "type": "function",
                "function": {
                    "name": "search_history",
                    "description": "Retrieve source messages for corrections and missing context.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        total_calls = 0
        for step in range(self.settings.max_model_steps):
            if not await query(checkpoint, run_id):
                raise Interrupted()
            calls, text = [], ""
            usage = {}
            started = time.monotonic()
            async for event in self.provider.stream(messages, tools):
                if not await query(checkpoint, run_id):
                    raise Interrupted()
                if event.kind == "text_delta":
                    text += event.text
                    if len(text) > 24000:
                        raise RuntimeError("Output budget exceeded")
                    await query(self.append, run_id, event.text)
                elif event.kind == "usage":
                    usage = {
                        k: v
                        for k, v in event.usage.items()
                        if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and isinstance(v, int)
                        and v >= 0
                    }
                elif event.kind == "tool_call":
                    calls.append(event)
                    total_calls += 1
                    if len(calls) > 8 or total_calls > self.settings.max_tool_calls:
                        raise RuntimeError("Tool budget exceeded")
            await query(
                Trace.create,
                run=run_id,
                kind="model",
                payload=json.dumps(
                    {
                        "step": step,
                        "provider": self.settings.llm_provider,
                        "model": self.settings.llm_model,
                        "prompt_version": 1,
                        "usage": usage,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "output_chars": len(text),
                        "tool_count": len(calls),
                    }
                ),
            )
            if not calls:
                await query(finish, run_id, "succeeded", "run.completed", {})
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in calls
                    ],
                }
            )
            for call in calls:
                if not await query(checkpoint, run_id):
                    raise Interrupted()
                await query(emit, run_id, "tool.status", {"text": f"Checking {call.name}"})
                if call.name == "search_history":
                    result = await query(retrieve, run_id, str(call.arguments.get("query", "")))
                else:
                    result = await self.executor.execute(run_id, call.name, call.arguments)
                await query(
                    Trace.create,
                    run=run_id,
                    kind="tool",
                    payload=json.dumps(
                        {"name": call.name, "call_id": call.call_id, "ok": "error" not in result}
                    ),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(result)[:20000],
                    }
                )
        raise RuntimeError("Model step budget exceeded")
