#!/usr/bin/env python3
"""Verify standalone artifacts and stage their payloads into npm packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
EXPECTED_RUNTIME_DISTRIBUTIONS = {
    "anyio",
    "certifi",
    "h11",
    "httpcore",
    "httpx",
    "idna",
}


class StagingError(RuntimeError):
    """A release artifact failed validation and must not enter an npm package."""


@dataclass(frozen=True, slots=True)
class NpmTarget:
    target: str
    package_directory: str
    executable: str


TARGETS = (
    NpmTarget("darwin-arm64", "cli-darwin-arm64", "api429"),
    NpmTarget("darwin-x64", "cli-darwin-x64", "api429"),
    NpmTarget("linux-arm64", "cli-linux-arm64-gnu", "api429"),
    NpmTarget("linux-x64", "cli-linux-x64-gnu", "api429"),
    NpmTarget("win32-arm64", "cli-win32-arm64", "api429.exe"),
    NpmTarget("win32-x64", "cli-win32-x64", "api429.exe"),
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagingError(f"cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StagingError(f"expected a JSON object in {path}")
    return value


def _safe_file_name(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise StagingError(f"manifest field {field!r} must be a non-empty string")
    path = PurePosixPath(value)
    if path.name != value or value in {".", ".."}:
        raise StagingError(f"manifest field {field!r} is not a safe file name")
    return value


def _bounded_read(handle: IO[bytes], *, expected_size: int) -> bytes:
    if expected_size < 1 or expected_size > MAX_EXECUTABLE_BYTES:
        raise StagingError(
            f"executable size {expected_size} is outside the allowed range"
        )
    payload = handle.read(expected_size + 1)
    if len(payload) != expected_size:
        raise StagingError(
            f"archive payload size is {len(payload)} bytes, expected {expected_size}"
        )
    return payload


def _extract_tar(path: Path, executable: str, expected_size: int) -> bytes:
    try:
        with tarfile.open(path, mode="r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) != 1:
                raise StagingError(f"{path.name} must contain exactly one entry")
            member = members[0]
            if member.name != executable or not member.isfile():
                raise StagingError(
                    f"{path.name} must contain one regular file named {executable}"
                )
            if member.size != expected_size:
                raise StagingError(
                    f"{path.name} declares {member.size} bytes, expected {expected_size}"
                )
            handle = bundle.extractfile(member)
            if handle is None:
                raise StagingError(f"cannot extract {executable} from {path.name}")
            return _bounded_read(handle, expected_size=expected_size)
    except (OSError, tarfile.TarError) as exc:
        raise StagingError(f"cannot read {path.name}: {exc}") from exc


def _extract_zip(path: Path, executable: str, expected_size: int) -> bytes:
    try:
        with zipfile.ZipFile(path) as bundle:
            infos = bundle.infolist()
            if len(infos) != 1:
                raise StagingError(f"{path.name} must contain exactly one entry")
            info = infos[0]
            if info.filename != executable or info.is_dir():
                raise StagingError(
                    f"{path.name} must contain one regular file named {executable}"
                )
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise StagingError(f"{path.name} must not contain a symbolic link")
            if info.file_size != expected_size:
                raise StagingError(
                    f"{path.name} declares {info.file_size} bytes, expected {expected_size}"
                )
            with bundle.open(info, mode="r") as handle:
                return _bounded_read(handle, expected_size=expected_size)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise StagingError(f"cannot read {path.name}: {exc}") from exc


def _package_version(npm_root: Path, target: NpmTarget) -> str:
    manifest = _read_json(
        npm_root / "packages" / target.package_directory / "package.json"
    )
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise StagingError(f"invalid package version for {target.package_directory}")
    return version


def _find_manifest(artifacts: Path, target: NpmTarget) -> Path:
    candidates = sorted(artifacts.glob(f"api429-v*-{target.target}.json"))
    if len(candidates) != 1:
        raise StagingError(
            f"expected exactly one manifest for {target.target}, found {len(candidates)}"
        )
    return candidates[0]


def _integer_field(manifest: dict[str, object], field: str) -> int:
    value = manifest.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StagingError(f"manifest field {field!r} must be an integer")
    return value


def _verify_build_metadata(manifest: dict[str, object], manifest_path: Path) -> None:
    python_version = manifest.get("python")
    if not isinstance(python_version, str) or not python_version:
        raise StagingError(f"{manifest_path.name}: missing Python build version")
    dependencies = manifest.get("bundled_dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != (
        EXPECTED_RUNTIME_DISTRIBUTIONS
    ):
        raise StagingError(
            f"{manifest_path.name}: incomplete bundled dependency manifest"
        )
    if not all(isinstance(value, str) and value for value in dependencies.values()):
        raise StagingError(
            f"{manifest_path.name}: bundled dependency versions must be strings"
        )
    frozen = manifest.get("frozen_distributions")
    expected_frozen = sorted({"api429-cli", *EXPECTED_RUNTIME_DISTRIBUTIONS})
    if frozen != expected_frozen:
        raise StagingError(
            f"{manifest_path.name}: frozen distribution audit is incomplete"
        )
    native_files = manifest.get("frozen_native_files")
    if (
        not isinstance(native_files, list)
        or not native_files
        or not all(
            isinstance(value, str)
            and value
            and not PurePosixPath(value).is_absolute()
            and ".." not in PurePosixPath(value).parts
            for value in native_files
        )
    ):
        raise StagingError(f"{manifest_path.name}: invalid frozen native inventory")


def verify_artifact(
    artifacts: Path,
    npm_root: Path,
    target: NpmTarget,
) -> tuple[bytes, dict[str, object], Path]:
    manifest_path = _find_manifest(artifacts, target)
    manifest = _read_json(manifest_path)
    expected_version = _package_version(npm_root, target)

    expected_fields: dict[str, object] = {
        "schema_version": 1,
        "package": "api429-cli",
        "version": expected_version,
        "target": target.target,
        "executable": target.executable,
    }
    for field, expected in expected_fields.items():
        if manifest.get(field) != expected:
            raise StagingError(
                f"{manifest_path.name}: {field}={manifest.get(field)!r}, "
                f"expected {expected!r}"
            )
    _verify_build_metadata(manifest, manifest_path)

    archive_name = _safe_file_name(manifest.get("archive"), field="archive")
    archive = artifacts / archive_name
    if not archive.is_file():
        raise StagingError(f"missing archive {archive}")
    expected_archive_size = _integer_field(manifest, "archive_size")
    if archive.stat().st_size != expected_archive_size:
        raise StagingError(
            f"{archive.name} size is {archive.stat().st_size}, "
            f"expected {expected_archive_size}"
        )
    archive_digest = manifest.get("archive_sha256")
    if not isinstance(archive_digest, str) or sha256_file(archive) != archive_digest:
        raise StagingError(f"SHA-256 mismatch for {archive.name}")

    expected_executable_size = _integer_field(manifest, "executable_size")
    if archive_name.endswith(".tar.gz"):
        payload = _extract_tar(archive, target.executable, expected_executable_size)
    elif archive_name.endswith(".zip"):
        payload = _extract_zip(archive, target.executable, expected_executable_size)
    else:
        raise StagingError(f"unsupported archive format: {archive_name}")

    executable_digest = manifest.get("executable_sha256")
    if (
        not isinstance(executable_digest, str)
        or sha256_bytes(payload) != executable_digest
    ):
        raise StagingError(f"SHA-256 mismatch for {target.executable}")
    return payload, manifest, manifest_path


def _atomic_write(path: Path, payload: bytes, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise StagingError(f"refusing to replace {path}; use --force")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o755)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage_all(
    artifacts: Path,
    npm_root: Path,
    *,
    force: bool,
) -> list[dict[str, object]]:
    artifacts = artifacts.resolve()
    npm_root = npm_root.resolve()
    if not artifacts.is_dir():
        raise StagingError(f"artifact directory does not exist: {artifacts}")
    if not (npm_root / "packages" / "cli" / "package.json").is_file():
        raise StagingError(f"not an API429 npm workspace: {npm_root}")

    verified: list[tuple[NpmTarget, bytes, dict[str, object], Path]] = []
    for target in TARGETS:
        payload, manifest, manifest_path = verify_artifact(artifacts, npm_root, target)
        verified.append((target, payload, manifest, manifest_path))

    result: list[dict[str, object]] = []
    for target, payload, manifest, manifest_path in verified:
        destination = (
            npm_root / "packages" / target.package_directory / "bin" / target.executable
        )
        _atomic_write(destination, payload, force=force)
        result.append(
            {
                "target": target.target,
                "package_directory": target.package_directory,
                "binary": str(destination),
                "binary_sha256": manifest["executable_sha256"],
                "manifest": str(manifest_path),
            }
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    repository = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Verify six standalone artifacts and stage npm payloads"
    )
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--npm-dir", type=Path, default=repository / "npm")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        staged = stage_all(
            args.artifacts_dir,
            args.npm_dir,
            force=args.force,
        )
    except StagingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"schema_version": 1, "staged": staged}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
