import asyncio
import json
import logging
import time

from app.agent.context import assemble, authorized, covered_sources, estimate, fit_prompt, retrieve
from app.agent.tracing import record_tool
from app.core.guardrails import (
    POLICY_VERSION,
    SAFE_OUTPUT,
    ContentRejected,
    require_content,
    safe_data,
    safe_text,
)
from app.db.database import query
from app.db.models import Message, Run, Trace, db
from app.db.repository import (
    TERMINAL,
    checkpoint,
    claim_run,
    cleanup,
    emit,
    finish,
    mark_uncertain,
    recover,
)
from app.tools.executor import Executor, Interrupted, UnknownOutcome, Waiting
from app.tools.schemas import definitions

log = logging.getLogger("agent")


class Runner:
    """Bounded async consumers; one active run per conversation, independent of SSE."""

    def __init__(self, settings, vault, provider):
        self.settings, self.provider = settings, provider
        self.executor = Executor(vault, settings=settings)
        self.stopping = asyncio.Event()

    async def supervise(self):
        try:
            await query(recover)
        except Exception as exc:
            log.error(
                json.dumps({"event": "runner_recovery_failed", "error_class": type(exc).__name__})
            )
            raise
        workers = [
            asyncio.create_task(self.consume(), name=f"agent-worker-{index}")
            for index in range(self.settings.max_concurrent_runs)
        ]
        try:
            while not self.stopping.is_set():
                try:
                    await query(cleanup)
                    await self.pause(3600)
                except Exception as exc:
                    log.error(
                        json.dumps(
                            {"event": "runner_supervisor_failed", "error_class": type(exc).__name__}
                        )
                    )
                    await self.pause()
        finally:
            for worker in workers:
                worker.cancel()
            # execute() settles dispatched writes as uncertain on cancellation.
            # Drain workers before the lifespan closes their shared provider.
            await asyncio.gather(*workers, return_exceptions=True)

    async def pause(self, seconds=0.25):
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def consume(self):
        while not self.stopping.is_set():
            try:
                # to_thread cannot stop a transaction once it starts. If cancelled
                # during a claim, await its result and settle it before exiting.
                claim_task = asyncio.create_task(query(claim_run))
                try:
                    run_id = await asyncio.shield(claim_task)
                except asyncio.CancelledError:
                    run_id = await claim_task
                    if run_id:
                        await query(finish, run_id, "interrupted", "run.interrupted", {})
                    raise
                if run_id:
                    await self.execute(run_id)
                else:
                    await self.pause()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    json.dumps({"event": "runner_worker_failed", "error_class": type(exc).__name__})
                )
                await self.pause()

    async def execute(self, run_id):
        try:
            await self._execute(run_id)
        except asyncio.CancelledError:
            # Cover cancellation during exception handlers and final bookkeeping too.
            await self.stopped(run_id)
            raise

    async def _execute(self, run_id):
        try:
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                # Recheck queued messages accepted before the policy was installed.
                await query(lambda: require_content(Run.get_by_id(run_id).message.content))
                await self.loop(run_id)
        except ContentRejected as exc:
            await query(self.append, run_id, SAFE_OUTPUT, new_response=True)
            await query(
                Trace.create,
                run=run_id,
                kind="guardrail",
                payload=json.dumps({"reason": exc.reason, "policy_version": POLICY_VERSION}),
            )
            await query(finish, run_id, "failed", "run.failed", {"message": SAFE_OUTPUT})
        except Waiting as exc:
            text = safe_text(str(exc))
            await query(self.append, run_id, text, new_response=True)
            await query(finish, run_id, "waiting_for_user", "run.waiting", {"message": text})
        except Interrupted:
            await query(finish, run_id, "interrupted", "run.interrupted", {})
        except (UnknownOutcome, TimeoutError):
            await self.stopped(run_id)
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

    async def stopped(self, run_id):
        def settle():
            with db.atomic("IMMEDIATE"):
                # Cancellation can race an offloaded terminal commit. Keep its outcome.
                if Run.get_by_id(run_id).status in TERMINAL:
                    return
                unknown = mark_uncertain(run_id)
                finish(
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

        await query(settle)

    @staticmethod
    def append(run_id, text, *, new_response=False):
        if not text:
            return
        with db.atomic("IMMEDIATE"):
            run = Run.get_by_id(run_id)
            msg, _ = Message.get_or_create(
                conversation=run.conversation_id,
                client_request_id=f"assistant:{run_id}",
                defaults={"role": "assistant", "status": "partial"},
            )
            # Tokens within one response must stay byte-for-byte intact. Separate
            # independent model responses (and confirmation prompts), not tokens.
            if new_response and msg.content:
                text = "\n\n" + text
            require_content(msg.content + text)
            msg.content += text
            msg.save()
            emit(run_id, "message.delta", {"message_id": msg.id, "text": text})

    async def loop(self, run_id):
        if not await query(checkpoint, run_id):
            raise Interrupted()
        pending = await query(authorized, run_id)
        tools = definitions() + [
            {
                "type": "function",
                "function": {
                    "name": "search_history",
                    "description": "Retrieve original user messages for missing context. "
                    "Use short keywords or an empty query for recent messages. "
                    "Fetch an excerpt's full original using source_id; "
                    "set role=assistant only to find past assistant replies.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "source_id": {"type": "string"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        exchanges: list[list[dict]] = []
        if pending:
            # Check mandatory intent/reserves before dispatching an authorized action.
            await query(assemble, run_id, self.settings, tools)
            result = await self.tool(run_id, pending[0], pending[1], "confirmed-action")
            exchanges.append(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "confirmed-action",
                                "type": "function",
                                "function": {
                                    "name": pending[0],
                                    "arguments": json.dumps(pending[1]),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "confirmed-action",
                        "content": json.dumps(result),
                    },
                ]
            )
        total_calls = int(bool(pending))
        seen_call_ids = {"confirmed-action"} if pending else set()
        for step in range(self.settings.max_model_steps):
            if not await query(checkpoint, run_id):
                raise Interrupted()
            base = await query(assemble, run_id, self.settings, tools)
            messages = fit_prompt(base, exchanges, tools, self.settings)
            calls, text = [], ""
            usage = {}
            started = time.monotonic()
            async for event in self.provider.stream(messages, tools):
                if not await query(checkpoint, run_id):
                    raise Interrupted()
                if event.kind == "text_delta":
                    text += event.text
                    if (
                        len(text) > 24000
                        or estimate(text, self.settings) > self.settings.context_output_tokens
                    ):
                        raise RuntimeError("Output budget exceeded")
                elif event.kind == "usage":
                    usage = {
                        k: v
                        for k, v in event.usage.items()
                        if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                        and isinstance(v, int)
                        and v >= 0
                    }
                elif event.kind == "tool_call":
                    if (
                        not isinstance(event.call_id, str)
                        or not event.call_id
                        or event.call_id in seen_call_ids
                        or not isinstance(event.arguments, dict)
                    ):
                        raise RuntimeError("Invalid tool call protocol")
                    seen_call_ids.add(event.call_id)
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
                        "prompt_version": 5,
                        "estimated_input_tokens": estimate(messages, self.settings),
                        "usage": usage,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "output_chars": len(text),
                        "tool_count": len(calls),
                    }
                ),
            )
            if not await query(checkpoint, run_id):
                raise Interrupted()
            secrets = (
                self.settings.master_secret,
                *self.settings.master_secret_previous,
                self.settings.openrouter_api_key,
            )
            # Hold the entire response, including proposed calls, until validated.
            # A prohibited word/credential can span arbitrary provider chunks.
            require_content(text, secrets=secrets)
            for call in calls:
                if call.name not in {t["function"]["name"] for t in tools}:
                    raise RuntimeError("Unknown tool proposal")
                require_content(call.arguments, secrets=secrets)
            await query(self.append, run_id, text, new_response=True)
            if not calls:
                await query(finish, run_id, "succeeded", "run.completed", {})
                return
            exchange = [
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
            ]
            for call in calls:
                if not await query(checkpoint, run_id):
                    raise Interrupted()
                await query(emit, run_id, "tool.status", {"text": "Checking scheduling details."})
                result = await self.tool(
                    run_id,
                    call.name,
                    call.arguments,
                    call.call_id,
                    excluded=covered_sources(messages + exchange),
                )
                exchange.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(result),
                    }
                )
            exchanges.append(exchange)
        raise RuntimeError("Model step budget exceeded")

    async def tool(self, run_id, name, arguments, call_id, *, excluded=None):
        started = time.monotonic()
        result, error = None, None
        try:
            if name == "search_history":
                if (
                    not isinstance(arguments, dict)
                    or not {"query"} <= set(arguments) <= {"query", "role", "source_id"}
                    or not isinstance(arguments.get("query"), str)
                    or arguments.get("role", "user") not in ("user", "assistant")
                    or (
                        "source_id" in arguments
                        and (
                            not isinstance(arguments["source_id"], str)
                            or not arguments["source_id"]
                        )
                    )
                ):
                    result = {"error": "Invalid history query arguments"}
                else:
                    result = await query(
                        retrieve,
                        run_id,
                        arguments["query"],
                        excluded,
                        self.settings,
                        role=arguments.get("role", "user"),
                        source_id=arguments.get("source_id"),
                    )
            else:
                result = await self.executor.execute(run_id, name, arguments)
            return safe_data(
                result,
                secrets=(
                    self.settings.master_secret,
                    *self.settings.master_secret_previous,
                    self.settings.openrouter_api_key,
                ),
            )
        except BaseException as exc:
            # Exception text may contain upstream credentials or private prompts.
            error = (
                type(exc).__name__
                if isinstance(
                    exc,
                    (Waiting, Interrupted, UnknownOutcome, TimeoutError, asyncio.CancelledError),
                )
                else "ToolExecutionError"
            )
            raise
        finally:
            await query(
                record_tool,
                run_id,
                name,
                call_id,
                arguments,
                result,
                int((time.monotonic() - started) * 1000),
                self.settings,
                error=error,
            )
