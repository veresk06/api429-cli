from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .client import API429Client
from .errors import ConfigurationError

_DATA_URI_RE = re.compile(r"^data:([^;,]+)?(?:;[^;,]+)*;base64,(.*)$", re.DOTALL)
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "portal_session_token",
        "password",
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "delete_url",
    }
)
_INLINE_KEYS = frozenset({"b64_json", "image_base64", "audio_base64", "video_base64"})
_SIGNED_URL_SECRET_PARAMS = frozenset(
    {
        "apikey",
        "authorization",
        "awsaccesskeyid",
        "clientsecret",
        "credential",
        "googleaccessid",
        "hash",
        "hmac",
        "idtoken",
        "key",
        "keypairid",
        "password",
        "policy",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "sig",
        "signature",
        "token",
        "xamzcredential",
        "xamzsecuritytoken",
        "xamzsignature",
        "xgoogcredential",
        "xgoogsecuritytoken",
        "xgoogsignature",
    }
)
_MAX_INLINE_BYTES = 512 * 1024 * 1024


def json_dump(value: Any, *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    json.dump(
        sanitize_for_display(value),
        stream,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    stream.write("\n")


def sanitize_for_display(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _SECRET_KEYS:
                sanitized[key] = "[redacted]"
            elif normalized in _INLINE_KEYS and isinstance(child, str):
                sanitized[key] = f"[base64 omitted: {len(child)} characters]"
            else:
                sanitized[key] = sanitize_for_display(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_display(item) for item in value]
    if (
        isinstance(value, str)
        and value.startswith("data:")
        and ";base64," in value[:256]
    ):
        return f"[data URI omitted: {len(value)} characters]"
    if isinstance(value, str):
        return _sanitize_signed_url(value)
    return value


def _sanitize_signed_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return value
    changed = False
    sanitized_pairs: list[tuple[str, str]] = []
    for key, child in pairs:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        secret = normalized in _SIGNED_URL_SECRET_PARAMS or normalized.endswith(
            (
                "accesstoken",
                "credential",
                "password",
                "secret",
                "securitytoken",
                "signature",
            )
        )
        if secret:
            child = "[redacted]"
            changed = True
        sanitized_pairs.append((key, child))
    if not changed:
        return value
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(sanitized_pairs, doseq=True),
            parsed.fragment,
        )
    )


def print_models(payload: Any) -> None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        json_dump(payload)
        return
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append((str(item.get("id") or ""), str(item.get("owned_by") or "")))
    if not rows:
        print("No models are currently available for this token.")
        return
    width = min(72, max(len(row[0]) for row in rows))
    print(f"{'MODEL':<{width}}  PROVIDER")
    for model_id, owner in rows:
        print(f"{model_id:<{width}}  {owner}")


def print_balance(payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("balance"), dict):
        json_dump(payload)
        return
    balance = payload["balance"]
    amount = balance.get("available_balance_usd", balance.get("balance_usd"))
    debt = balance.get("debt_usd")
    suffix = str(payload.get("token_last4") or "")
    print(f"Balance: ${float(amount or 0):.4f}")
    if debt:
        print(f"Debt: ${float(debt):.4f}")
    if suffix:
        print(f"Token: …{suffix}")
    if balance.get("low_balance"):
        print("Warning: low balance", file=sys.stderr)


def print_usage(payload: Any) -> None:
    if not isinstance(payload, dict):
        json_dump(payload)
        return
    periods = [
        name for name in ("day", "week", "month") if isinstance(payload.get(name), dict)
    ]
    if not periods:
        calls = payload.get("calls") or {}
        tokens = payload.get("tokens") or {}
        print(
            f"Calls: {calls.get('total', 0)}  "
            f"Cost: ${float(tokens.get('discounted_cost_usd') or 0):.6f}"
        )
        return
    print(f"{'PERIOD':<8} {'CALLS':>8} {'SUCCESS':>8} {'FAILED':>8} {'COST USD':>12}")
    for name in periods:
        report = payload[name]
        calls = report.get("calls") or {}
        tokens = report.get("tokens") or {}
        print(
            f"{name:<8} {int(calls.get('total') or 0):>8} "
            f"{int(calls.get('success') or 0):>8} "
            f"{int(calls.get('failure') or 0):>8} "
            f"{float(tokens.get('discounted_cost_usd') or 0):>12.6f}"
        )


def extract_media_items(payload: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "videos", "images", "outputs"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        if not candidates:
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(payload)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or item.get("mimeType") or "")
        raw_b64 = item.get("b64_json")
        if isinstance(raw_b64, str) and raw_b64:
            items.append(
                {"kind": "base64", "value": raw_b64, "mime_type": mime or "image/png"}
            )
            continue
        for key in ("url", "video_url", "image_url", "uri", "result_url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                kind = "data_uri" if value.startswith("data:") else "url"
                items.append({"kind": kind, "value": value, "mime_type": mime})
                break
    return items


def save_media(
    client: API429Client,
    payload: Any,
    output: Path,
    *,
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    items = extract_media_items(payload)
    if not items:
        raise ConfigurationError("The response contains no downloadable media")
    saved: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        destination = _destination_for_item(output, item, index=index, total=len(items))
        if destination.is_symlink():
            raise ConfigurationError(f"Refusing to overwrite symlink: {destination}")
        if destination.exists() and not overwrite:
            raise ConfigurationError(
                f"Output file already exists: {destination}. Pass --force to replace it."
            )
        if item["kind"] == "url":
            client.download(item["value"], destination, overwrite=overwrite)
        elif item["kind"] == "data_uri":
            mime, data = _decode_data_uri(item["value"])
            item["mime_type"] = item["mime_type"] or mime
            _atomic_write(destination, data, overwrite=overwrite)
        else:
            data = _decode_base64(item["value"])
            _atomic_write(destination, data, overwrite=overwrite)
        size, digest = _file_metadata(destination)
        saved.append(
            {
                "path": str(destination.resolve()),
                "mime_type": item["mime_type"]
                or mimetypes.guess_type(destination.name)[0],
                "bytes": size,
                "sha256": digest,
            }
        )
    return saved


def _destination_for_item(
    requested: Path,
    item: dict[str, str],
    *,
    index: int,
    total: int,
) -> Path:
    suffix = requested.suffix
    if not suffix:
        suffix = _guess_suffix(item)
    stem = requested.stem if requested.suffix else requested.name
    name = f"{stem}-{index}{suffix}" if total > 1 else f"{stem}{suffix}"
    return requested.with_name(name)


def _guess_suffix(item: dict[str, str]) -> str:
    mime = item.get("mime_type") or ""
    if mime:
        return mimetypes.guess_extension(mime.split(";", 1)[0]) or ""
    value = item.get("value") or ""
    if item.get("kind") == "data_uri":
        match = _DATA_URI_RE.match(value)
        if match and match.group(1):
            return mimetypes.guess_extension(match.group(1)) or ""
    if item.get("kind") == "url":
        return Path(urlsplit(value).path).suffix
    return ""


def _decode_data_uri(value: str) -> tuple[str, bytes]:
    match = _DATA_URI_RE.match(value)
    if not match:
        raise ConfigurationError("Unsupported generated data URI")
    return match.group(1) or "application/octet-stream", _decode_base64(match.group(2))


def _decode_base64(value: str) -> bytes:
    estimated = len(value) * 3 // 4
    if estimated > _MAX_INLINE_BYTES:
        raise ConfigurationError(
            "Generated inline media exceeds the 512 MiB safety limit"
        )
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("Generated media contains invalid base64") from exc


def _atomic_write(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise ConfigurationError(
            f"Output file already exists: {path}. Pass --force to replace it."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        if os.name == "posix":
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise ConfigurationError(
                    f"Output file already exists: {path}. Pass --force to replace it."
                ) from exc
            temporary_path.unlink()
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _file_metadata(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


__all__ = [
    "extract_media_items",
    "json_dump",
    "print_balance",
    "print_models",
    "print_usage",
    "sanitize_for_display",
    "save_media",
]
