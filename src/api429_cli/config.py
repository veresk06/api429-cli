"""Configuration loading and credential persistence for the api429 CLI.

The module deliberately uses only the Python standard library.  Configuration
and credentials live in separate JSON files so callers can manage and protect
the API key independently from ordinary CLI preferences.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://gateway.api429.com"
DEFAULT_TIMEOUT = 600.0


@dataclass(frozen=True, slots=True)
class Settings:
    """Effective settings after applying all configuration layers."""

    base_url: str
    api_key: str | None = field(repr=False)
    timeout: float


def config_paths(env: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """Return the config and credentials paths, in that order.

    ``API429_CONFIG_FILE`` points at the main configuration file; the separate
    credentials file is placed alongside it.  Otherwise the directory follows
    XDG, Windows APPDATA, and finally ``~/.config`` conventions.
    """

    environ = os.environ if env is None else env
    configured_file = _nonempty(environ.get("API429_CONFIG_FILE"))
    if configured_file is not None:
        config_file = Path(configured_file).expanduser()
        return config_file, config_file.with_name(
            f"{config_file.stem}.credentials.json"
        )

    xdg_home = _nonempty(environ.get("XDG_CONFIG_HOME"))
    if xdg_home is not None:
        config_dir = Path(xdg_home).expanduser() / "api429"
    elif (
        sys.platform == "win32"
        and (appdata := _nonempty(environ.get("APPDATA"))) is not None
    ):
        config_dir = Path(appdata).expanduser() / "api429"
    else:
        config_dir = Path.home() / ".config" / "api429"

    return config_dir / "config.json", config_dir / "credentials.json"


def load_settings(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Load effective settings.

    Precedence is explicit keyword arguments, ``API429_*`` environment
    variables, JSON files, then built-in defaults.  An empty environment value
    is treated as unset.
    """

    environ = os.environ if env is None else env
    config_file, credentials_file = config_paths(environ)
    config_data = _read_json_object(config_file)
    credentials_data = _read_json_object(credentials_file, secret=True)

    effective_base_url = _first_nonempty(
        base_url,
        environ.get("API429_BASE_URL"),
        config_data.get("base_url"),
        DEFAULT_BASE_URL,
    )
    if not isinstance(effective_base_url, str):
        raise ValueError("base_url must be a non-empty string")

    effective_api_key = _first_nonempty(
        api_key,
        environ.get("API429_API_KEY"),
        credentials_data.get("api_key"),
    )
    if effective_api_key is not None and not isinstance(effective_api_key, str):
        raise ValueError("api_key must be a string")

    effective_timeout: Any
    if timeout is not None:
        effective_timeout = timeout
    elif _nonempty(environ.get("API429_TIMEOUT")) is not None:
        effective_timeout = environ["API429_TIMEOUT"]
    elif config_data.get("timeout") is not None:
        effective_timeout = config_data["timeout"]
    else:
        effective_timeout = DEFAULT_TIMEOUT

    return Settings(
        base_url=effective_base_url.strip(),
        api_key=effective_api_key.strip() if effective_api_key is not None else None,
        timeout=_parse_timeout(effective_timeout),
    )


def save_credentials(api_key: str, *, env: Mapping[str, str] | None = None) -> None:
    """Persist an API key atomically in the dedicated credentials file."""

    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")

    _, credentials_file = config_paths(env)
    _validate_secret_path(credentials_file, allow_missing=True)
    _atomic_write_json(credentials_file, {"api_key": api_key.strip()})


def clear_credentials(*, env: Mapping[str, str] | None = None) -> None:
    """Remove persisted credentials; doing so repeatedly is safe."""

    _, credentials_file = config_paths(env)
    _validate_secret_path(credentials_file, allow_missing=True)
    try:
        credentials_file.unlink()
    except FileNotFoundError:
        return


def _read_json_object(path: Path, *, secret: bool = False) -> dict[str, Any]:
    if secret:
        _validate_secret_path(path, allow_missing=True)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read configuration file: {path}") from exc

    if not isinstance(value, dict):
        raise ValueError(f"configuration file must contain a JSON object: {path}")
    return value


def _validate_secret_path(path: Path, *, allow_missing: bool) -> None:
    if path.is_symlink():
        raise ValueError(f"credential path must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    if os.name == "posix":
        if metadata.st_uid != os.getuid():
            raise ValueError(
                f"credential file is not owned by the current user: {path}"
            )
        if metadata.st_mode & 0o077:
            raise ValueError(f"credential file permissions are too broad: {path}")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    parent = path.parent
    parent_created = not parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and parent_created:
        parent.chmod(0o700)

    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(raw_path)
        if os.name == "posix":
            os.chmod(temporary_path, 0o600)

        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _parse_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout must be a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("timeout must be a positive number")
    return parsed


def _nonempty(value: Any) -> Any | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _first_nonempty(*values: Any) -> Any | None:
    for value in values:
        normalized = _nonempty(value)
        if normalized is not None:
            return normalized
    return None


__all__ = [
    "Settings",
    "clear_credentials",
    "config_paths",
    "load_settings",
    "save_credentials",
]
