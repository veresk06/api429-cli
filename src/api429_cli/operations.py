from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import config_paths


def begin_operation(
    *,
    endpoint: str,
    model: str,
    request_payload: Mapping[str, Any],
    base_url: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    operation_id = f"op_{uuid.uuid4().hex}"
    record = {
        "id": operation_id,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "submitting",
        "endpoint": endpoint,
        "model": model,
        "base_url": base_url,
        "request_sha256": _request_hash(request_payload),
        "idempotency_key": idempotency_key,
        "job_id": None,
        "error": None,
    }
    _write_record(record)
    return record


def update_operation(
    operation_id: str,
    *,
    status: str,
    job_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    path = _operation_path(operation_id)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        current: dict[str, Any] = (
            loaded
            if isinstance(loaded, dict)
            else {"id": operation_id, "created_at": _now()}
        )
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        current = {"id": operation_id, "created_at": _now()}
    current.update(
        {
            "updated_at": _now(),
            "status": status,
            "job_id": job_id if job_id is not None else current.get("job_id"),
            "error": error,
        }
    )
    _write_record(current)
    return current


def list_operations(*, limit: int = 50) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    directory = _operations_dir()
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in directory.glob("op_*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return records[:limit]


def _request_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operations_dir() -> Path:
    config_file, _ = config_paths()
    return config_file.parent / "operations"


def _operation_path(operation_id: str) -> Path:
    normalized = str(operation_id or "")
    if not normalized.startswith("op_") or not normalized[3:].isalnum():
        raise ValueError("invalid operation id")
    return _operations_dir() / f"{normalized}.json"


def _write_record(record: Mapping[str, Any]) -> None:
    path = _operation_path(str(record["id"]))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    try:
        if os.name == "posix":
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["begin_operation", "list_operations", "update_operation"]
