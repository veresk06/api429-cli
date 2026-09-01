from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from api429_cli import config


def _env(tmp_path: Path) -> dict[str, str]:
    return {"API429_CONFIG_FILE": str(tmp_path / "config.json")}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_secret_json(path: Path, value: object) -> None:
    _write_json(path, value)
    if os.name == "posix":
        path.chmod(0o600)


def test_defaults(tmp_path: Path) -> None:
    settings = config.load_settings(env=_env(tmp_path))

    assert settings == config.Settings(
        base_url="https://gateway.api429.com",
        api_key=None,
        timeout=600.0,
    )


def test_precedence_explicit_over_environment_over_files(tmp_path: Path) -> None:
    env = _env(tmp_path)
    config_file, credentials_file = config.config_paths(env)
    _write_json(config_file, {"base_url": "https://file.example", "timeout": 10})
    _write_secret_json(credentials_file, {"api_key": "file-secret"})
    env.update(
        API429_BASE_URL="https://env.example",
        API429_API_KEY="env-secret",
        API429_TIMEOUT="20",
    )

    from_env = config.load_settings(env=env)
    assert from_env == config.Settings(
        base_url="https://env.example",
        api_key="env-secret",
        timeout=20.0,
    )

    explicit = config.load_settings(
        base_url="https://explicit.example",
        api_key="explicit-secret",
        timeout=30,
        env=env,
    )
    assert explicit == config.Settings(
        base_url="https://explicit.example",
        api_key="explicit-secret",
        timeout=30.0,
    )


def test_file_values_override_defaults(tmp_path: Path) -> None:
    env = _env(tmp_path)
    config_file, credentials_file = config.config_paths(env)
    _write_json(config_file, {"base_url": "https://file.example", "timeout": 45.5})
    _write_secret_json(credentials_file, {"api_key": "file-secret"})

    assert config.load_settings(env=env) == config.Settings(
        base_url="https://file.example",
        api_key="file-secret",
        timeout=45.5,
    )


def test_api_key_is_not_exposed_by_settings_repr(tmp_path: Path) -> None:
    settings = config.load_settings(api_key="top-secret", env=_env(tmp_path))

    assert "top-secret" not in repr(settings)


def test_api_key_in_main_config_is_not_treated_as_credentials(tmp_path: Path) -> None:
    env = _env(tmp_path)
    config_file, _ = config.config_paths(env)
    _write_json(config_file, {"api_key": "misplaced-secret"})

    assert config.load_settings(env=env).api_key is None


def test_config_paths_prefers_explicit_file(tmp_path: Path) -> None:
    config_file = tmp_path / "named.json"

    assert config.config_paths({"API429_CONFIG_FILE": str(config_file)}) == (
        config_file,
        tmp_path / "named.credentials.json",
    )


def test_config_paths_uses_xdg_before_platform_default(tmp_path: Path) -> None:
    xdg_home = tmp_path / "xdg"

    assert config.config_paths({"XDG_CONFIG_HOME": str(xdg_home)}) == (
        xdg_home / "api429" / "config.json",
        xdg_home / "api429" / "credentials.json",
    )


def test_config_paths_uses_windows_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setattr(config.sys, "platform", "win32")

    assert config.config_paths({"APPDATA": str(appdata)}) == (
        appdata / "api429" / "config.json",
        appdata / "api429" / "credentials.json",
    )


def test_config_paths_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))

    assert config.config_paths({}) == (
        tmp_path / ".config" / "api429" / "config.json",
        tmp_path / ".config" / "api429" / "credentials.json",
    )


def test_save_credentials_writes_only_credentials_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    config_file, credentials_file = config.config_paths(env)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", recording_replace)

    config.save_credentials("top-secret", env=env)

    assert not config_file.exists()
    assert json.loads(credentials_file.read_text(encoding="utf-8")) == {
        "api_key": "top-secret"
    }
    assert len(replacements) == 1
    temporary_file, destination = replacements[0]
    assert destination == credentials_file
    assert temporary_file.parent == credentials_file.parent
    assert not temporary_file.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_save_credentials_sets_private_permissions(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _, credentials_file = config.config_paths(env)

    config.save_credentials("top-secret", env=env)

    directory_mode = stat.S_IMODE(credentials_file.parent.stat().st_mode)
    file_mode = stat.S_IMODE(credentials_file.stat().st_mode)
    assert directory_mode == 0o700
    assert file_mode == 0o600


def test_save_credentials_never_prints_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save_credentials("top-secret", env=_env(tmp_path))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "top-secret" not in captured.out + captured.err


def test_clear_credentials_is_idempotent(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _, credentials_file = config.config_paths(env)
    config.save_credentials("top-secret", env=env)

    config.clear_credentials(env=env)
    config.clear_credentials(env=env)

    assert not credentials_file.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_existing_custom_parent_permissions_are_not_changed(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    env = {"API429_CONFIG_FILE": str(parent / "custom.json")}

    config.save_credentials("top-secret", env=env)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_credential_symlink_is_rejected(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _, credentials_file = config.config_paths(env)
    target = tmp_path / "unrelated.json"
    target.write_text('{"api_key":"do-not-touch"}', encoding="utf-8")
    credentials_file.symlink_to(target)

    with pytest.raises(ValueError, match="must not be a symlink"):
        config.save_credentials("replacement", env=env)
    with pytest.raises(ValueError, match="must not be a symlink"):
        config.clear_credentials(env=env)

    assert "do-not-touch" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["", "zero", 0, -1, float("inf"), True])
def test_invalid_timeout_is_rejected(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        config.load_settings(timeout=value, env=_env(tmp_path))  # type: ignore[arg-type]


def test_invalid_json_does_not_echo_file_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    config_file, _ = config.config_paths(env)
    config_file.write_text('{"api_key": "top-secret"', encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        config.load_settings(env=env)

    assert "top-secret" not in str(exc_info.value)
    captured = capsys.readouterr()
    assert "top-secret" not in captured.out + captured.err
