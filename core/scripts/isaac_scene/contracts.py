from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


RESULT_SCHEMA_VERSION = "isaac-scene-command-result/v1"
SENSITIVE_FRAGMENTS = ("password", "passwd", "token", "secret", "api_key", "private_key")


def run_id(command: str, payload: Any = None) -> str:
    material = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
    digest = hashlib.sha1(f"{command}:{time.time_ns()}:{material}".encode()).hexdigest()[:12]
    return f"isaac_{command.replace('-', '_')}_{digest}"


def issue(code: str, summary: str, *, retryable: bool = False, details: Any = None) -> dict[str, Any]:
    row: dict[str, Any] = {"code": code, "summary": summary, "retryable": retryable}
    if details not in (None, "", [], {}):
        row["details"] = details
    return row


def envelope(
    command: str,
    result: Any,
    *,
    ok: bool = True,
    status: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    errors = list(errors or [])
    warnings = list(warnings or [])
    if status is None:
        status = "fail" if errors or not ok else ("warn" if warnings else "pass")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": bool(ok and not errors),
        "status": status,
        "command": command,
        "run_id": run_id(command, result),
        "result": result,
        "artifacts": list(artifacts or []),
        "warnings": warnings,
        "errors": errors,
        "metrics": dict(metrics or {}),
        "next_recommended_actions": list(next_actions or []),
    }


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if any(fragment in key.lower() for fragment in SENSITIVE_FRAGMENTS) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
