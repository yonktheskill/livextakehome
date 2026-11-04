"""Bounded execution evidence only: never prompts, private reasoning or vault access."""

import hashlib
import json
import logging
import re
from itertools import islice
from pathlib import Path

from app.db.models import Trace

log = logging.getLogger("agent.diagnostics")
ROOT = Path(__file__).resolve().parents[3]
REDACTED = "[redacted]"
SENSITIVE = re.compile(
    r"key|secret|password|passwd|cookie|authorization|credential|token|"
    r"verification|otp|passcode|securitycode|prompt|reasoning|chainofthought",
    re.I,
)
VALUE_SECRET = re.compile(
    r"\b(?:sk[-_](?:or[-_])?|cal_(?:live_|test_)?|gh[pousr]_|github_pat_|"
    r"AKIA|AIza|xox[baprs]-)[A-Za-z0-9_./+\-=]{8,}|"
    r"\bBearer\s+\S+|\bBasic\s+\S+|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b\d{4,8}\b",
    re.I,
)
LABELLED_SECRET = re.compile(
    r"\b(?:api[-_ ]?key|password|passwd|secret|token|cookie|authorization|"
    r"verification[-_ ]?code|otp|passcode)\s*[:=]\s*[^\n,;]+",
    re.I,
)


def sanitize(value, *, secrets=(), depth=0):
    if depth > 8:
        return "[depth limit]"
    if isinstance(value, dict):
        result: dict = {}
        for key, item in islice(value.items(), 40):
            name = str(key)
            if SENSITIVE.search(re.sub(r"[^a-z]", "", name.lower())):
                result["[sensitive field]"] = REDACTED
            else:
                safe_key = sanitize(name, secrets=secrets, depth=depth + 1)
                safe_key = safe_key if isinstance(safe_key, str) else "[structured field]"
                result[safe_key] = sanitize(item, secrets=secrets, depth=depth + 1)
        if len(value) > 40:
            result["_omitted"] = "additional fields"
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets=secrets, depth=depth + 1) for v in value[:40]] + (
            ["[additional items omitted]"] if len(value) > 40 else []
        )
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return sanitize(parsed, secrets=secrets, depth=depth + 1)
        # Sanitize before bounding so a cutoff cannot expose a partial key.
        for secret in secrets:
            if secret:
                value = value.replace(secret, REDACTED)
        value = LABELLED_SECRET.sub(REDACTED, value)
        value = VALUE_SECRET.sub(REDACTED, value)
        return value[:2000] + (" [omitted]" if len(value) > 2000 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported value]"


def _record_tool(run_id, name, call_id, arguments, result, elapsed_ms, settings, *, error=None):
    # Never decrypt/read a connection to obtain redaction values.
    secrets = (
        settings.openrouter_api_key,
        settings.master_secret,
        *settings.master_secret_previous,
    )
    metadata = sanitize(
        {
            "run_id": run_id,
            "name": name,
            "call_id": call_id,
            "elapsed_ms": elapsed_ms,
            "ok": error is None and not (isinstance(result, dict) and "error" in result),
            "error": error,
            "inputs": arguments,
            "outputs": result,
        },
        secrets=secrets,
    )
    metadata["input_bytes"] = _size(arguments)
    metadata["output_bytes"] = _size(result)
    metadata["output_items"] = len(result) if isinstance(result, (list, dict)) else None
    metadata["run_id"] = run_id
    metadata["elapsed_ms"] = elapsed_ms
    payload = json.dumps(metadata, ensure_ascii=True, allow_nan=False)
    if len(payload.encode()) > settings.trace_max_bytes:
        # Replace whole structures, keeping valid JSON and correlation metadata.
        metadata["outputs"] = {"omitted": "trace byte limit"}
        payload = json.dumps(metadata, ensure_ascii=True, allow_nan=False)
    if len(payload.encode()) > settings.trace_max_bytes:
        metadata["inputs"] = {"omitted": "trace byte limit"}
        payload = json.dumps(metadata, ensure_ascii=True, allow_nan=False)
    if len(payload.encode()) > settings.trace_max_bytes:
        metadata["name"] = "[omitted: trace byte limit]"
        metadata["call_id"] = "[omitted: trace byte limit]"
        payload = json.dumps(metadata, ensure_ascii=True, allow_nan=False)
    _persist(run_id, "tool", metadata)


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, default=lambda _: "[unsupported]").encode())


def tool_fingerprint(name, arguments):
    """Compare repeated proposals without retaining another copy of arguments."""
    encoded = json.dumps([name, arguments], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _diagnostic_data(value, secrets=()):
    if isinstance(value, dict):
        result: dict = {}
        for key, item in islice(value.items(), 80):
            if (
                isinstance(key, str)
                and (key.endswith("_tokens") or key in {"prompt_version", "policy_version"})
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
            ):
                result[key] = item
            elif (
                key in {"prompt_hash", "schema_hash", "fingerprint"}
                and isinstance(item, str)
                and re.fullmatch(r"[0-9a-f]{64}", item)
            ):
                result[key] = item
            elif isinstance(key, str) and not SENSITIVE.search(re.sub(r"[^a-z]", "", key.lower())):
                result[key] = _diagnostic_data(item, secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [_diagnostic_data(item, secrets) for item in value[:80]]
    if (
        isinstance(value, str)
        and re.fullmatch(
            r"(?:[0-9a-f]{32,64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|(?:thr_|thread_|turn_)[A-Za-z0-9_-]{1,100})",
            value,
        )
        and not any(secret and secret in value for secret in secrets)
    ):
        return value
    return sanitize(value, secrets=secrets)


def _fallback(event, metadata):
    # A JSON message works with the default server formatter as well as JSON log sinks.
    log.error(json.dumps({"event": event, **metadata}, allow_nan=False))


def _persist(run_id, kind, metadata):
    try:
        Trace.create(run=run_id, kind=kind, payload=json.dumps(metadata, allow_nan=False))
    except Exception:
        _fallback(
            "diagnostic_persistence_failed",
            {
                "run_id": run_id,
                "kind": kind,
                "metadata": metadata,
            },
        )


def record_diagnostic(run_id, kind, metadata, settings=None):
    """Best-effort diagnostic write. Never replace an application failure."""
    try:
        secrets = (
            ()
            if settings is None
            else (
                settings.master_secret,
                *settings.master_secret_previous,
                settings.openrouter_api_key,
            )
        )
        data = _diagnostic_data(metadata, secrets)
        data["run_id"] = run_id
        limit = settings.trace_max_bytes if settings is not None else 16000
        if _size(data) > limit:
            data = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
            data["details_omitted"] = True
        if _size(data) > limit:
            data = {"run_id": run_id, "details_omitted": True}
        _persist(run_id, kind, data)
    except Exception:
        _fallback("diagnostic_serialization_failed", {"run_id": run_id, "kind": kind})


def record_tool(run_id, name, call_id, arguments, result, elapsed_ms, settings, *, error=None):
    try:
        _record_tool(run_id, name, call_id, arguments, result, elapsed_ms, settings, error=error)
    except Exception:
        _fallback(
            "tool_diagnostic_failed", {"run_id": run_id, "kind": "tool", "elapsed_ms": elapsed_ms}
        )


def error_metadata(exc):
    """Retain exception identity and source locations, never messages or locals."""

    def class_name(error):
        name = type(error).__name__
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"

    data: dict = {"exception_class": class_name(exc)}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
        data["code"] = code
    if isinstance(getattr(exc, "retryable", None), bool):
        data["retryable"] = exc.retryable
    details = getattr(exc, "diagnostics", {})
    if isinstance(details, dict):
        for key in (
            "phase",
            "http_status",
            "attempt",
            "sdk_version",
            "runtime_version",
            "cause_class",
            "thread_id",
            "turn_id",
        ):
            value = details.get(key)
            if isinstance(value, (str, int, bool)):
                data[key] = _diagnostic_data(value)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        data["cause_class"] = class_name(cause)
    frames = []
    frame = exc.__traceback__
    while frame is not None:
        filename = Path(frame.tb_frame.f_code.co_filename)
        try:
            relative = filename.resolve().relative_to(ROOT / "backend" / "app")
        except (ValueError, OSError):
            pass
        else:
            frames.append(
                {
                    "file": "backend/app/" + str(relative),
                    "function": frame.tb_frame.f_code.co_name,
                    "line": frame.tb_lineno,
                }
            )
        frame = frame.tb_next
    data["frames"] = frames[-12:]
    return data


async def record_async(run_id, kind, metadata, settings=None):
    """Also handle failures opening the DB connection, before the writer runs."""
    from app.db.database import query

    try:
        await query(record_diagnostic, run_id, kind, metadata, settings)
    except Exception:
        secrets = (
            ()
            if settings is None
            else (
                settings.master_secret,
                *settings.master_secret_previous,
                settings.openrouter_api_key,
            )
        )
        _fallback(
            "diagnostic_database_unavailable",
            {"run_id": run_id, "kind": kind, "metadata": _diagnostic_data(metadata, secrets)},
        )


async def record_tool_async(
    run_id, name, call_id, arguments, result, elapsed_ms, settings, *, error=None
):
    from app.db.database import query

    try:
        await query(
            record_tool, run_id, name, call_id, arguments, result, elapsed_ms, settings, error=error
        )
    except Exception:
        _fallback(
            "tool_diagnostic_database_unavailable", {"run_id": run_id, "elapsed_ms": elapsed_ms}
        )
