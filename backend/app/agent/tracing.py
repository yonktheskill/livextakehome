"""Bounded execution evidence only: never prompts, private reasoning or vault access."""

import json
import re
from itertools import islice

from app.db.models import Trace

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
        result = {}
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


def record_tool(run_id, name, call_id, arguments, result, elapsed_ms, settings, *, error=None):
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
    Trace.create(run=run_id, kind="tool", payload=payload)
