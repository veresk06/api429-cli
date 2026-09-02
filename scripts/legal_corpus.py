"""Load and verify the immutable legal corpus shipped with native releases."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LEGAL_MANIFEST_PATH = "licenses/manifest.json"
MAX_LEGAL_FILE_BYTES = 2 * 1024 * 1024
MAX_LEGAL_CORPUS_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_LEGAL_PATHS = {
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "licenses/BLAKE2-CC0-1.0.txt",
    "licenses/CPython-3.13.13-Windows.txt",
    "licenses/CPython-3.13.13.txt",
    "licenses/HACL-MIT.txt",
    "licenses/MPL-2.0.txt",
    "licenses/OpenSSL-3.txt",
    "licenses/PyInstaller-6.22.2.txt",
    "licenses/PyInstaller-Hooks-Contrib-2026.7.txt",
    "licenses/SQLite.txt",
    "licenses/anyio-4.14.2.txt",
    "licenses/bzip2.txt",
    "licenses/certifi-2026.7.22.txt",
    "licenses/expat.txt",
    "licenses/h11-0.16.0.txt",
    "licenses/httpcore-1.0.9.txt",
    "licenses/httpx-0.28.1.txt",
    "licenses/idna-3.19.txt",
    "licenses/libffi.txt",
    "licenses/liblzma.txt",
    "licenses/libuuid.txt",
    "licenses/mpdecimal.txt",
    "licenses/zlib.txt",
}


class LegalCorpusError(RuntimeError):
    """The checked-in legal corpus is missing, unsafe, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class LegalFile:
    archive_path: str
    source: Path
    sha256: str
    size: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_legal_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise LegalCorpusError("legal manifest paths must be non-empty strings")
    path = PurePosixPath(value)
    is_project_file = value in {"LICENSE", "THIRD_PARTY_NOTICES.md"}
    is_license_text = (
        len(path.parts) == 2 and path.parts[0] == "licenses" and path.suffix == ".txt"
    )
    if (
        path.is_absolute()
        or ".." in path.parts
        or not (is_project_file or is_license_text)
    ):
        raise LegalCorpusError(f"unsafe or unsupported legal corpus path: {value!r}")
    return value


def load_legal_corpus(repository: Path) -> tuple[LegalFile, ...]:
    """Verify the checked-in hashes and return stable archive entries."""
    repository = repository.resolve()
    manifest_path = repository / LEGAL_MANIFEST_PATH
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise LegalCorpusError(f"missing regular legal manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegalCorpusError(f"cannot read legal manifest: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "files",
        "schema_version",
    }:
        raise LegalCorpusError("legal manifest has an unexpected shape")
    if manifest["schema_version"] != 1 or not isinstance(manifest["files"], list):
        raise LegalCorpusError("unsupported legal manifest schema")

    files: list[LegalFile] = []
    seen: set[str] = set()
    total_size = 0
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise LegalCorpusError(
                "legal manifest entries must contain path and sha256"
            )
        archive_path = _safe_legal_path(item["path"])
        expected_digest = item["sha256"]
        if not isinstance(expected_digest, str) or not _SHA256.fullmatch(
            expected_digest
        ):
            raise LegalCorpusError(f"invalid SHA-256 for {archive_path}")
        if archive_path in seen:
            raise LegalCorpusError(f"duplicate legal corpus path: {archive_path}")
        seen.add(archive_path)
        source = repository.joinpath(*PurePosixPath(archive_path).parts)
        if not source.is_file() or source.is_symlink():
            raise LegalCorpusError(
                f"legal corpus entry is not a regular file: {source}"
            )
        size = source.stat().st_size
        if size < 1 or size > MAX_LEGAL_FILE_BYTES:
            raise LegalCorpusError(
                f"legal corpus entry has an invalid size: {archive_path} ({size})"
            )
        actual_digest = _sha256_file(source)
        if actual_digest != expected_digest:
            raise LegalCorpusError(
                f"legal corpus SHA-256 mismatch for {archive_path}: "
                f"expected {expected_digest}, got {actual_digest}"
            )
        total_size += size
        files.append(LegalFile(archive_path, source, actual_digest, size))

    paths = [item.archive_path for item in files]
    expected_order = ["LICENSE", "THIRD_PARTY_NOTICES.md"] + sorted(
        path for path in paths if path.startswith("licenses/")
    )
    if paths != expected_order or len(paths) < 3:
        raise LegalCorpusError(
            "legal manifest must list LICENSE, THIRD_PARTY_NOTICES.md, then sorted texts"
        )
    missing_required = sorted(_REQUIRED_LEGAL_PATHS - set(paths))
    if missing_required:
        raise LegalCorpusError(
            f"legal manifest omits required runtime coverage: {missing_required}"
        )

    licenses_directory = repository / "licenses"
    expected_directory_entries = {
        "manifest.json",
        *(PurePosixPath(path).name for path in paths if path.startswith("licenses/")),
    }
    actual_directory_entries = {path.name for path in licenses_directory.iterdir()}
    if actual_directory_entries != expected_directory_entries:
        missing = sorted(expected_directory_entries - actual_directory_entries)
        extra = sorted(actual_directory_entries - expected_directory_entries)
        raise LegalCorpusError(
            f"legal corpus directory mismatch: missing={missing}, extra={extra}"
        )

    manifest_size = manifest_path.stat().st_size
    total_size += manifest_size
    if total_size > MAX_LEGAL_CORPUS_BYTES:
        raise LegalCorpusError(
            f"legal corpus is too large: {total_size} bytes exceeds "
            f"{MAX_LEGAL_CORPUS_BYTES}"
        )
    manifest_file = LegalFile(
        LEGAL_MANIFEST_PATH,
        manifest_path,
        _sha256_file(manifest_path),
        manifest_size,
    )
    return tuple(files[:2] + [manifest_file] + files[2:])
