import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from app.agent.context import (
    ContextBudgetExceeded,
    assemble,
    authorized,
    covered_sources,
    estimate,
    fit_prompt,
    retrieve,
)
from app.agent.tracing import error_metadata, record_async, record_tool_async, tool_fingerprint
from app.core.guardrails import (
    POLICY_VERSION,
    SAFE_OUTPUT,
    ContentRejected,
    require_content,
    safe_data,
    safe_text,
)
from app.db.database import query
from app.db.models import Action, Message, Run, db
from app.db.repository import (
    TERMINAL,
    checkpoint,
    claim_run,
    cleanup,
    emit,
    finish,
    mark_uncertain,
    recover,
    wait_for_user,
)
from app.tools.executor import Executor, Interrupted, UnknownOutcome, Waiting
from app.tools.schemas import definitions

MUTATIONS = frozenset(
    {
        "create_booking",
        "reschedule_booking",
        "cancel_booking",
        "create_event_type",
        "save_preferred_name",
    }
)

log = logging.getLogger("agent")


class RunFailure(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass
class RunState:
    stage: str = "run"
    step: int | None = None
    total_calls: int = 0
    started: float = field(default_factory=time.monotonic)


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
            log.error(json.dumps({"event": "runner_recovery_failed", **error_metadata(exc)}))
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
                        json.dumps({"event": "runner_supervisor_failed", **error_metadata(exc)})
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
                log.error(json.dumps({"event": "runner_worker_failed", **error_metadata(exc)}))
                await self.pause()

    async def execute(self, run_id):
        state = RunState()
        try:
            await self._execute(run_id, state)
        except asyncio.CancelledError as exc:
            # Cover cancellation during exception handlers and final bookkeeping too.
            await self.stopped(run_id, state, exc, "cancelled")
            raise

    async def _execute(self, run_id, state):
        try:
            await record_async(
                run_id,
                "run.started",
                {
                    "provider": self.settings.llm_provider,
                    "model": self.settings.llm_model,
                    "limits": {
                        "run_seconds": self.settings.run_timeout_seconds,
                        "model_steps": self.settings.max_model_steps,
                        "tool_calls": self.settings.max_tool_calls,
                        "input_tokens": self.settings.context_input_tokens,
                        "provider_seconds": getattr(
                            getattr(self.provider, "limits", None), "timeout_seconds", None
                        ),
                    },
                },
                self.settings,
            )
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                # Recheck queued messages accepted before the policy was installed.
                await query(lambda: require_content(Run.get_by_id(run_id).message.content))
                from app.agent.vision import EXTRACTION_VERSION, extract
                from app.db.image_context import cache, pending_images

                state.stage = "vision"
                for message_id, caption, images in await query(
                    pending_images, run_id, EXTRACTION_VERSION
                ):
                    if not await query(checkpoint, run_id):
                        raise Interrupted()
                    value = await extract(self.provider, caption, images)
                    await query(cache, message_id, EXTRACTION_VERSION, value)
                await self.loop(run_id, state)
        except ContentRejected as exc:
            await self.record_failure(run_id, exc, state, "content_rejected")
            await query(self.append, run_id, SAFE_OUTPUT, new_response=True)
            await record_async(
                run_id,
                "guardrail",
                {"reason": exc.reason, "policy_version": POLICY_VERSION},
                self.settings,
            )
            await query(finish, run_id, "failed", "run.failed", {"message": SAFE_OUTPUT})
        except Waiting as exc:
            text = safe_text(str(exc))
            await query(wait_for_user, run_id, text)
        except Interrupted:
            await query(finish, run_id, "interrupted", "run.interrupted", {})
        except (UnknownOutcome, TimeoutError) as exc:
            await self.stopped(
                run_id,
                state,
                exc,
                "run_timeout" if isinstance(exc, TimeoutError) else "unknown_outcome",
            )
        except Exception as exc:
            await self.record_failure(run_id, exc, state)
            unknown = await query(mark_uncertain, run_id)
            await query(
                finish,
                run_id,
                "needs_reconciliation" if unknown else "failed",
                "run.failed",
                {
                    "message": self.failure_message(exc),
                    "code": getattr(exc, "code", "internal_error"),
                },
            )
            log.error("Agent run failed", extra={"run_id": run_id})

    async def stopped(self, run_id, state, exc, code):
        await self.record_failure(run_id, exc, state, code)

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

    async def record_failure(self, run_id, exc, state, code=None):
        dispatched = None
        try:
            dispatched = await query(
                lambda: (
                    Action.select()
                    .where((Action.run == run_id) & Action.state.in_(["dispatching", "unknown"]))
                    .exists()
                )
            )
        except Exception:
            pass
        await record_async(
            run_id,
            "error",
            {
                **error_metadata(exc),
                "code": code or getattr(exc, "code", "internal_error"),
                "stage": state.stage,
                "action_outcome_uncertain": dispatched,
                "tool_calls_consumed": state.total_calls,
                "max_tool_calls": self.settings.max_tool_calls,
                "max_model_steps": self.settings.max_model_steps,
                "run_timeout_seconds": self.settings.run_timeout_seconds,
                "provider_timeout_seconds": getattr(
                    getattr(self.provider, "limits", None), "timeout_seconds", None
                ),
                "step": state.step,
                "elapsed_ms": int((time.monotonic() - state.started) * 1000),
            },
            self.settings,
        )

    @staticmethod
    def failure_message(exc):
        return {
            "repeated_calls": (
                "The assistant repeated the same lookups without progress. "
                "This was an internal execution problem; no further changes were attempted."
            ),
            "model_step_limit": (
                "The assistant reached its decision limit before finishing. "
                "This was an internal execution problem; no further changes were attempted."
            ),
            "tool_limit": "The assistant reached its tool-call limit before finishing.",
            "context_budget": (
                "The scheduling details could not fit in the context budget. "
                "This was an internal execution problem; no further changes were attempted."
            ),
            "timeout": "The model provider timed out. Please try again.",
            "unsupported": "The configured provider or model does not support image input.",
            "invalid_extraction": (
                "Could not read the image details. Retry or type the meeting details."
            ),
        }.get(getattr(exc, "code", ""), "Agent failed. Check provider configuration and try again.")

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

    async def loop(self, run_id, state):
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
                    "Fetch originals using source_id and offset (Unicode characters, default 0). "
                    "When next_offset is non-null, continue with that source_id and offset "
                    "until complete=true; a page alone may omit important trailing details. "
                    "For source_format=image_source_json, assemble content pages as JSON "
                    "containing the original caption and untrusted image observations. "
                    "set role=assistant only to find past assistant replies.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "source_id": {"type": "string", "minLength": 1},
                            "offset": {"type": "integer", "minimum": 0},
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
            result = await self.tool(
                run_id, pending[0], pending[1], "confirmed-action", state=state
            )
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
        total_calls = state.total_calls = int(bool(pending))
        seen_call_ids = {"confirmed-action"} if pending else set()
        previous_batch, repeats = None, 0
        signatures = []
        recovery = False
        for step in range(self.settings.max_model_steps):
            state.step, state.stage = step, "context"
            if not await query(checkpoint, run_id):
                raise Interrupted()
            audit = {
                "step": step,
                "schema_hash": hashlib.sha256(
                    json.dumps(tools, sort_keys=True).encode()
                ).hexdigest(),
            }
            try:
                base = await query(assemble, run_id, self.settings, tools, audit=audit)
                if recovery:
                    base = [
                        base[0],
                        *base[1:-1],
                        {
                            "role": "system",
                            "content": "Repeated tool cycles returned unchanged facts. "
                            "Use retained results and original intent "
                            "to give a truthful final status. "
                            "No more tool calls or changes are permitted in this recovery attempt. "
                            "Do not claim success without successful action evidence.",
                        },
                        base[-1],
                    ]
                messages = fit_prompt(base, exchanges, tools, self.settings, audit=audit)
                audit["prompt_hash"] = hashlib.sha256(
                    json.dumps(messages, sort_keys=True).encode()
                ).hexdigest()
            finally:
                await record_async(run_id, "context", audit, self.settings)
            calls, text = [], ""
            usage = {}
            started = time.monotonic()
            state.stage = "model"
            await record_async(run_id, "model.started", {"step": step}, self.settings)
            try:
                async for event in self.provider.stream(messages, [] if recovery else tools):
                    if not await query(checkpoint, run_id):
                        raise Interrupted()
                    if event.kind == "text_delta":
                        text += event.text
                        if (
                            len(text) > 24000
                            or estimate(text, self.settings) > self.settings.context_output_tokens
                        ):
                            raise RunFailure("output_limit", "Output budget exceeded")
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
                            raise RunFailure("tool_protocol", "Invalid tool call protocol")
                        seen_call_ids.add(event.call_id)
                        calls.append(event)
                        total_calls += 1
                        state.total_calls = total_calls
                        if len(calls) > 8 or total_calls > self.settings.max_tool_calls:
                            raise RunFailure("tool_limit", "Tool budget exceeded")
            except BaseException as exc:
                await record_async(
                    run_id,
                    "model.failed",
                    {
                        "step": step,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        **error_metadata(exc),
                    },
                    self.settings,
                )
                raise
            await record_async(
                run_id,
                "model",
                {
                    "step": step,
                    "provider": self.settings.llm_provider,
                    "model": self.settings.llm_model,
                    "prompt_version": 7,
                    "estimated_input_tokens": estimate(messages, self.settings),
                    "usage": usage,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "output_chars": len(text),
                    "tool_count": len(calls),
                },
                self.settings,
            )
            if not await query(checkpoint, run_id):
                raise Interrupted()
            state.stage = "validation"
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
                    raise RunFailure("unknown_tool", "Unknown tool proposal")
                require_content(call.arguments, secrets=secrets)
            if recovery and calls:
                raise RunFailure("repeated_calls", "Recovery proposed more tool calls")
            await query(self.append, run_id, text, new_response=True)
            state.stage = "decision"
            batch = tuple(sorted(tool_fingerprint(c.name, c.arguments) for c in calls))
            repeats = repeats + 1 if batch == previous_batch else 1
            previous_batch = batch
            if calls:
                await record_async(
                    run_id,
                    "decision",
                    {"step": step, "fingerprints": list(batch), "consecutive_repeats": repeats},
                    self.settings,
                )

            if not calls:
                await query(finish, run_id, "succeeded", "run.completed", {})
                return
            exchange: list[dict] = [
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
                    state=state,
                )
                exchange.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(result),
                    }
                )
            signature = (
                batch,
                tool_fingerprint("results", [json.loads(m["content"]) for m in exchange[1:]]),
            )
            signatures.append(signature)
            exchanges.append(exchange)
            # Compare complete cycles including result fingerprints: changing results
            # are progress, even when arguments repeat. Call IDs are deliberately absent.
            recovery = any(
                len(signatures) >= period * 2
                and signatures[-period:] == signatures[-2 * period : -period]
                for period in range(1, 5)
            )
            if recovery:
                await record_async(run_id, "loop.recovery", {"step": step}, self.settings)
            if step == self.settings.max_model_steps - 1:
                # No extra model invocation or write after the cap. A persisted,
                # successful final action can still be reported deterministically.
                completed = await query(
                    lambda: list(
                        Action.select().where(
                            (Action.run == run_id) & (Action.state == "succeeded")
                        )
                    )
                )
                results = [json.loads(m["content"]) for m in exchange[1:]]
                if all(c.name in MUTATIONS for c in calls) and all(
                    any(a.kind == c.name and json.loads(a.result) == result for a in completed)
                    for c, result in zip(calls, results, strict=True)
                ):
                    references = [
                        str(result["response"].get("uid", result["response"].get("id", "")))
                        for result in results
                        if isinstance(result.get("response"), dict)
                    ]
                    report = "Scheduling action completed."
                    if any(references):
                        report += " Reference: " + ", ".join(filter(None, references)) + "."
                    require_content(report, secrets=secrets)
                    await query(self.append, run_id, report, new_response=True)
                    await query(finish, run_id, "succeeded", "run.completed", {})
                    return
        state.stage = "budget"
        raise RunFailure(
            "repeated_calls" if recovery else "model_step_limit",
            "Repeated calls without progress" if recovery else "Model step budget exceeded",
        )

    async def tool(self, run_id, name, arguments, call_id, *, excluded=None, state=None):
        state = state or RunState()
        state.stage = "tool"
        started = time.monotonic()
        await record_async(
            run_id,
            "tool.started",
            {
                "step": state.step,
                "name": name,
                "call_id": call_id,
                "fingerprint": tool_fingerprint(name, arguments),
            },
            self.settings,
        )
        result, error = None, None
        try:
            if name == "search_history":
                if (
                    not isinstance(arguments, dict)
                    or not {"query"} <= set(arguments) <= {"query", "role", "source_id", "offset"}
                    or not isinstance(arguments.get("query"), str)
                    or arguments.get("role", "user") not in ("user", "assistant")
                    or type(arguments.get("offset", 0)) is not int
                    or arguments.get("offset", 0) < 0
                    or ("offset" in arguments and "source_id" not in arguments)
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
                    try:
                        result = await query(
                            retrieve,
                            run_id,
                            arguments["query"],
                            excluded,
                            self.settings,
                            role=arguments.get("role", "user"),
                            source_id=arguments.get("source_id"),
                            offset=arguments.get("offset", 0),
                        )
                    except ContextBudgetExceeded:
                        raise
                    except ValueError:
                        result = {"error": "Invalid history offset for source"}
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
            await record_async(
                run_id,
                "tool.failed",
                {"call_id": call_id, "name": name, **error_metadata(exc)},
                self.settings,
            )
            raise
        finally:
            await record_tool_async(
                run_id,
                name,
                call_id,
                arguments,
                result,
                int((time.monotonic() - started) * 1000),
                self.settings,
                error=error,
            )
