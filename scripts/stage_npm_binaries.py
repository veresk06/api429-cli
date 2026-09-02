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

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.legal_corpus import LegalCorpusError, LegalFile, load_legal_corpus

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


def _bounded_read(
    handle: IO[bytes],
    *,
    expected_size: int,
    maximum_size: int,
    label: str,
) -> bytes:
    if expected_size < 1 or expected_size > maximum_size:
        raise StagingError(f"{label} size {expected_size} is outside the allowed range")
    payload = handle.read(expected_size + 1)
    if len(payload) != expected_size:
        raise StagingError(
            f"archive payload size is {len(payload)} bytes, expected {expected_size}"
        )
    return payload


def _expected_archive_entries(
    executable: str,
    expected_size: int,
    legal_files: Sequence[LegalFile],
) -> list[tuple[str, int, int]]:
    return [
        (executable, expected_size, 0o755),
        *((item.archive_path, item.size, 0o644) for item in legal_files),
    ]


def _extract_tar(
    path: Path,
    executable: str,
    expected_size: int,
    legal_files: Sequence[LegalFile],
) -> tuple[bytes, dict[str, bytes]]:
    expected_entries = _expected_archive_entries(executable, expected_size, legal_files)
    try:
        with tarfile.open(path, mode="r:gz") as bundle:
            members = bundle.getmembers()
            if [member.name for member in members] != [
                name for name, _size, _mode in expected_entries
            ]:
                raise StagingError(
                    f"{path.name} does not contain the exact ordered release corpus"
                )
            extracted: dict[str, bytes] = {}
            for member, (name, size, mode) in zip(
                members, expected_entries, strict=True
            ):
                if not member.isfile() or member.mode != mode:
                    raise StagingError(
                        f"{path.name} entry {name} must be a regular file with "
                        f"mode {mode:04o}"
                    )
                if member.size != size:
                    raise StagingError(
                        f"{path.name} entry {name} declares {member.size} bytes, "
                        f"expected {size}"
                    )
                handle = bundle.extractfile(member)
                if handle is None:
                    raise StagingError(f"cannot extract {name} from {path.name}")
                maximum = (
                    MAX_EXECUTABLE_BYTES
                    if name == executable
                    else max(item.size for item in legal_files)
                )
                extracted[name] = _bounded_read(
                    handle,
                    expected_size=size,
                    maximum_size=maximum,
                    label=name,
                )
            return extracted.pop(executable), extracted
    except (OSError, tarfile.TarError) as exc:
        raise StagingError(f"cannot read {path.name}: {exc}") from exc


def _extract_zip(
    path: Path,
    executable: str,
    expected_size: int,
    legal_files: Sequence[LegalFile],
) -> tuple[bytes, dict[str, bytes]]:
    expected_entries = _expected_archive_entries(executable, expected_size, legal_files)
    try:
        with zipfile.ZipFile(path) as bundle:
            infos = bundle.infolist()
            if [info.filename for info in infos] != [
                name for name, _size, _mode in expected_entries
            ]:
                raise StagingError(
                    f"{path.name} does not contain the exact ordered release corpus"
                )
            extracted: dict[str, bytes] = {}
            for info, (name, size, expected_mode) in zip(
                infos, expected_entries, strict=True
            ):
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or stat.S_ISLNK(mode)
                    or not stat.S_ISREG(mode)
                    or stat.S_IMODE(mode) != expected_mode
                ):
                    raise StagingError(
                        f"{path.name} entry {name} must be a regular file with "
                        f"mode {expected_mode:04o}"
                    )
                if info.file_size != size:
                    raise StagingError(
                        f"{path.name} entry {name} declares {info.file_size} bytes, "
                        f"expected {size}"
                    )
                maximum = (
                    MAX_EXECUTABLE_BYTES
                    if name == executable
                    else max(item.size for item in legal_files)
                )
                with bundle.open(info, mode="r") as handle:
                    extracted[name] = _bounded_read(
                        handle,
                        expected_size=size,
                        maximum_size=maximum,
                        label=name,
                    )
            return extracted.pop(executable), extracted
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


def _verify_build_metadata(
    manifest: dict[str, object],
    manifest_path: Path,
    legal_files: Sequence[LegalFile],
) -> None:
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
    expected_legal_files = [
        {"path": item.archive_path, "sha256": item.sha256, "size": item.size}
        for item in legal_files
    ]
    if manifest.get("legal_files") != expected_legal_files:
        raise StagingError(
            f"{manifest_path.name}: standalone legal corpus does not match this source"
        )


def verify_artifact(
    artifacts: Path,
    npm_root: Path,
    target: NpmTarget,
    legal_files: Sequence[LegalFile],
) -> tuple[bytes, dict[str, bytes], dict[str, object], Path]:
    manifest_path = _find_manifest(artifacts, target)
    manifest = _read_json(manifest_path)
    expected_version = _package_version(npm_root, target)

    expected_fields: dict[str, object] = {
        "schema_version": 2,
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
    _verify_build_metadata(manifest, manifest_path, legal_files)

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
        payload, legal_payloads = _extract_tar(
            archive,
            target.executable,
            expected_executable_size,
            legal_files,
        )
    elif archive_name.endswith(".zip"):
        payload, legal_payloads = _extract_zip(
            archive,
            target.executable,
            expected_executable_size,
            legal_files,
        )
    else:
        raise StagingError(f"unsupported archive format: {archive_name}")

    executable_digest = manifest.get("executable_sha256")
    if (
        not isinstance(executable_digest, str)
        or sha256_bytes(payload) != executable_digest
    ):
        raise StagingError(f"SHA-256 mismatch for {target.executable}")
    for item in legal_files:
        legal_payload = legal_payloads.get(item.archive_path)
        if legal_payload is None or sha256_bytes(legal_payload) != item.sha256:
            raise StagingError(
                f"SHA-256 mismatch for legal corpus entry {item.archive_path}"
            )
    return payload, legal_payloads, manifest, manifest_path


def _atomic_write(path: Path, payload: bytes, *, force: bool, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if (path.exists() or path.is_symlink()) and not force:
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
        temporary.chmod(mode)
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

    try:
        legal_files = load_legal_corpus(npm_root.parent)
    except LegalCorpusError as exc:
        raise StagingError(f"invalid release legal corpus: {exc}") from exc

    verified: list[
        tuple[NpmTarget, bytes, dict[str, bytes], dict[str, object], Path]
    ] = []
    for target in TARGETS:
        payload, legal_payloads, manifest, manifest_path = verify_artifact(
            artifacts, npm_root, target, legal_files
        )
        verified.append((target, payload, legal_payloads, manifest, manifest_path))

    destinations = [
        npm_root / "packages" / target.package_directory / relative_path
        for target, _payload, _legal_payloads, _manifest, _manifest_path in verified
        for relative_path in [
            f"bin/{target.executable}",
            *(item.archive_path for item in legal_files),
        ]
    ]
    if not force:
        existing = [path for path in destinations if path.exists() or path.is_symlink()]
        if existing:
            rendered = ", ".join(str(path) for path in existing)
            raise StagingError(
                f"refusing to replace staged file(s): {rendered}; use --force"
            )

    result: list[dict[str, object]] = []
    for target, payload, legal_payloads, manifest, manifest_path in verified:
        package = npm_root / "packages" / target.package_directory
        destination = package / "bin" / target.executable
        _atomic_write(destination, payload, force=force, mode=0o755)
        for item in legal_files:
            _atomic_write(
                package.joinpath(*PurePosixPath(item.archive_path).parts),
                legal_payloads[item.archive_path],
                force=force,
                mode=0o644,
            )
        result.append(
            {
                "target": target.target,
                "package_directory": target.package_directory,
                "binary": str(destination),
                "binary_sha256": manifest["executable_sha256"],
                "legal_files": len(legal_files),
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
