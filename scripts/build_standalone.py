#!/usr/bin/env python3
"""Build, verify, and package a native standalone API429 executable.

Run this script with the native Python interpreter for the requested target.
PyInstaller does not cross-compile, so an explicit target must match the host.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform as platform_module
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import IO, Literal

PACKAGE_DISTRIBUTION = "api429-cli"
EXECUTABLE_NAME = "api429"
STANDALONE_PYTHON_VERSION = "3.13.13"
SUPPORTED_PLATFORMS = ("darwin", "linux", "win32")
SUPPORTED_ARCHITECTURES = ("x64", "arm64")
DEFAULT_SOURCE_DATE_EPOCH = 0
ZIP_MIN_EPOCH = 315532800  # 1980-01-01, the earliest ZIP timestamp.
ZIP_MAX_EPOCH = 4354819198  # 2107-12-31 23:59:58, rounded for ZIP.
RUNTIME_DISTRIBUTIONS = ("anyio", "certifi", "h11", "httpcore", "httpx", "idna")
EXPECTED_FROZEN_DISTRIBUTIONS = {
    PACKAGE_DISTRIBUTION,
    *RUNTIME_DISTRIBUTIONS,
}
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class BuildError(RuntimeError):
    """A standalone artifact could not be built safely."""


@dataclass(frozen=True)
class Target:
    platform: str
    arch: str

    def __post_init__(self) -> None:
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {self.platform}")
        if self.arch not in SUPPORTED_ARCHITECTURES:
            raise ValueError(f"unsupported architecture: {self.arch}")

    @property
    def executable(self) -> str:
        return f"{EXECUTABLE_NAME}.exe" if self.platform == "win32" else EXECUTABLE_NAME

    @property
    def archive_suffix(self) -> str:
        return ".zip" if self.platform == "win32" else ".tar.gz"

    @property
    def label(self) -> str:
        return f"{self.platform}-{self.arch}"


@dataclass(frozen=True)
class ArtifactNames:
    stem: str
    executable: str
    archive: str
    checksum: str
    manifest: str


def normalize_platform(value: str) -> str:
    """Normalize Python platform identifiers to the public artifact contract."""
    normalized = value.lower()
    if normalized.startswith("linux"):
        return "linux"
    if normalized in {"darwin", "win32"}:
        return normalized
    raise BuildError(
        f"unsupported build platform {value!r}; expected darwin, linux, or win32"
    )


def normalize_arch(value: str) -> str:
    """Normalize native machine names to Node-compatible architecture names."""
    normalized = value.lower()
    if normalized in {"x86_64", "amd64", "x64"}:
        return "x64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    raise BuildError(
        f"unsupported build architecture {value!r}; expected x86_64/amd64 or arm64/aarch64"
    )


def detect_host_target() -> Target:
    return Target(
        normalize_platform(sys.platform),
        normalize_arch(platform_module.machine()),
    )


def resolve_target(
    requested_platform: str | None,
    requested_arch: str | None,
    *,
    host: Target | None = None,
) -> Target:
    """Resolve the requested target and reject accidental cross-compilation."""
    native = host or detect_host_target()
    requested = Target(
        requested_platform or native.platform,
        requested_arch or native.arch,
    )
    if requested != native:
        raise BuildError(
            "cross-compilation is not supported: "
            f"requested {requested.label}, native builder is {native.label}"
        )
    return requested


def artifact_names(version: str, target: Target) -> ArtifactNames:
    if not _SAFE_VERSION.fullmatch(version):
        raise BuildError(
            f"package version is not safe for an artifact name: {version!r}"
        )
    stem = f"api429-v{version}-{target.label}"
    archive = f"{stem}{target.archive_suffix}"
    return ArtifactNames(
        stem=stem,
        executable=target.executable,
        archive=archive,
        checksum=f"{archive}.sha256",
        manifest=f"{stem}.json",
    )


def source_date_epoch(environment: dict[str, str] | None = None) -> int:
    selected_environment = os.environ if environment is None else environment
    raw = selected_environment.get("SOURCE_DATE_EPOCH", str(DEFAULT_SOURCE_DATE_EPOCH))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BuildError("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
    if value < 0:
        raise BuildError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_stream(source: Path, destination: IO[bytes]) -> None:
    with source.open("rb") as handle:
        shutil.copyfileobj(handle, destination, length=1024 * 1024)


def create_archive(
    executable: Path,
    archive: Path,
    target: Target,
    *,
    epoch: int,
) -> None:
    """Create an archive with stable ordering, names, modes, and timestamps."""
    if target.platform == "win32":
        bounded_epoch = min(max(epoch, ZIP_MIN_EPOCH), ZIP_MAX_EPOCH)
        zip_info = zipfile.ZipInfo(
            target.executable,
            date_time=time.gmtime(bounded_epoch)[:6],
        )
        zip_info.compress_type = zipfile.ZIP_DEFLATED
        zip_info.create_system = 3
        zip_info.external_attr = (stat.S_IFREG | 0o755) << 16
        with (
            zipfile.ZipFile(
                archive, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as zip_bundle,
            zip_bundle.open(zip_info, mode="w") as destination,
        ):
            _copy_stream(executable, destination)
        return

    tar_info = tarfile.TarInfo(target.executable)
    tar_info.size = executable.stat().st_size
    tar_info.mode = 0o755
    tar_info.mtime = epoch
    tar_info.uid = 0
    tar_info.gid = 0
    tar_info.uname = ""
    tar_info.gname = ""
    with (
        archive.open("wb") as archive_handle,
        gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=archive_handle,
            mtime=epoch,
        ) as compressed,
        tarfile.open(
            mode="w", fileobj=compressed, format=tarfile.USTAR_FORMAT
        ) as tar_bundle,
        executable.open("rb") as executable_handle,
    ):
        tar_bundle.addfile(tar_info, executable_handle)


def _mach_o_arches(header: bytes) -> set[str]:
    thin_formats: dict[bytes, Literal["big", "little"]] = {
        b"\xfe\xed\xfa\xce": "big",
        b"\xce\xfa\xed\xfe": "little",
        b"\xfe\xed\xfa\xcf": "big",
        b"\xcf\xfa\xed\xfe": "little",
    }
    fat_formats: dict[bytes, tuple[Literal["big", "little"], int]] = {
        b"\xca\xfe\xba\xbe": ("big", 20),
        b"\xbe\xba\xfe\xca": ("little", 20),
        b"\xca\xfe\xba\xbf": ("big", 32),
        b"\xbf\xba\xfe\xca": ("little", 32),
    }
    cpu_names = {0x01000007: "x64", 0x0100000C: "arm64"}
    magic = header[:4]
    if magic in thin_formats:
        byteorder = thin_formats[magic]
        cpu_type = int.from_bytes(header[4:8], byteorder=byteorder)
        return {cpu_names[cpu_type]} if cpu_type in cpu_names else set()
    if magic not in fat_formats:
        return set()
    byteorder, stride = fat_formats[magic]
    count = int.from_bytes(header[4:8], byteorder=byteorder)
    if count > 32:
        return set()
    arches: set[str] = set()
    for index in range(count):
        offset = 8 + (index * stride)
        cpu_type = int.from_bytes(header[offset : offset + 4], byteorder=byteorder)
        if cpu_type in cpu_names:
            arches.add(cpu_names[cpu_type])
    return arches


def executable_arches(path: Path, platform: str) -> set[str]:
    """Read architecture labels directly from a Mach-O, ELF, or PE header."""
    with path.open("rb") as handle:
        header = handle.read(4096)
    if platform == "darwin":
        return _mach_o_arches(header)
    if platform == "linux" and header.startswith(b"\x7fELF") and len(header) >= 20:
        byteorders: dict[int, Literal["little", "big"]] = {
            1: "little",
            2: "big",
        }
        byteorder = byteorders.get(header[5])
        if byteorder is None:
            return set()
        machine = int.from_bytes(header[18:20], byteorder=byteorder)
        arch = {62: "x64", 183: "arm64"}.get(machine)
        return {arch} if arch else set()
    if platform == "win32" and header.startswith(b"MZ") and len(header) >= 64:
        pe_offset = int.from_bytes(header[60:64], byteorder="little")
        if (
            pe_offset + 6 > len(header)
            or header[pe_offset : pe_offset + 4] != b"PE\0\0"
        ):
            return set()
        machine = int.from_bytes(
            header[pe_offset + 4 : pe_offset + 6], byteorder="little"
        )
        arch = {0x8664: "x64", 0xAA64: "arm64"}.get(machine)
        return {arch} if arch else set()
    return set()


def assert_native_executable(path: Path, target: Target) -> None:
    arches = executable_arches(path, target.platform)
    if arches != {target.arch}:
        rendered = ", ".join(sorted(arches)) if arches else "unknown"
        raise BuildError(
            f"built executable architecture is {rendered}; expected only {target.arch}"
        )


def pyinstaller_command(
    *,
    python: Path,
    repository: Path,
    build_root: Path,
) -> list[str]:
    """Return the auditable PyInstaller invocation used by native runners."""
    return [
        str(python),
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--noupx",
        "--name",
        EXECUTABLE_NAME,
        "--distpath",
        str(build_root / "dist"),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        "--paths",
        str(repository / "src"),
        "--copy-metadata",
        PACKAGE_DISTRIBUTION,
        "--hidden-import",
        "httpx",
        "--hidden-import",
        "certifi",
        "--collect-data",
        "certifi",
        str(repository / "scripts" / "pyinstaller_entry.py"),
    ]


def _require_distribution(name: str, install_hint: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError as exc:
        raise BuildError(f"missing {name!r}; {install_hint}") from exc


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_frozen_distributions(distributions: set[str]) -> list[str]:
    """Reject build-environment packages accidentally collected by PyInstaller."""
    actual = {_normalize_distribution_name(name) for name in distributions}
    expected = {
        _normalize_distribution_name(name) for name in EXPECTED_FROZEN_DISTRIBUTIONS
    }
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise BuildError(
            "frozen third-party distribution mismatch: "
            f"missing={missing}, unexpected={unexpected}; "
            "build standalone artifacts in a clean environment"
        )
    return sorted(actual)


def audit_frozen_bundle(executable: Path, python: Path) -> tuple[list[str], list[str]]:
    """Audit embedded distributions and inventory bundled native libraries."""
    audit_script = r"""
import json
import sys
from importlib import metadata
from PyInstaller.archive.readers import CArchiveReader

archive = CArchiveReader(sys.argv[1])
pyz = archive.open_embedded_archive("PYZ.pyz")
top_level = {name.split(".", 1)[0] for name in pyz.toc}
mapping = metadata.packages_distributions()
distributions = sorted(
    {distribution for name in top_level for distribution in mapping.get(name, ())}
)
native_files = sorted(
    name for name, entry in archive.toc.items() if entry[-1] == "b"
)
print(json.dumps({"distributions": distributions, "native_files": native_files}))
"""
    try:
        result = subprocess.run(
            [str(python), "-c", audit_script, str(executable)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"could not audit the frozen module archive: {exc}") from exc
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError("frozen module audit returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise BuildError("frozen module audit returned an invalid result")
    distributions = value.get("distributions")
    native_files = value.get("native_files")
    if not isinstance(distributions, list) or not all(
        isinstance(item, str) and item for item in distributions
    ):
        raise BuildError("frozen module audit returned invalid distribution names")
    if (
        not isinstance(native_files, list)
        or not native_files
        or not all(isinstance(item, str) and item for item in native_files)
    ):
        raise BuildError("frozen module audit returned an invalid native inventory")
    return validate_frozen_distributions(set(distributions)), sorted(native_files)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_bytes(
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _publish(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_standalone(
    *,
    target: Target,
    output_dir: Path,
    force: bool,
    python: Path = Path(sys.executable),
) -> tuple[dict[str, object], Path]:
    repository = Path(__file__).resolve().parent.parent
    version = _require_distribution(
        PACKAGE_DISTRIBUTION,
        "install the project first with `python -m pip install .`",
    )
    pyinstaller_version = _require_distribution(
        "pyinstaller",
        "install `scripts/requirements-standalone.txt` first",
    )
    hooks_version = _require_distribution(
        "pyinstaller-hooks-contrib",
        "install `scripts/requirements-standalone.txt` first",
    )
    bundled_dependencies = {
        name: _require_distribution(name, "install the project dependencies first")
        for name in RUNTIME_DISTRIBUTIONS
    }
    python_version = platform_module.python_version()
    if python_version != STANDALONE_PYTHON_VERSION:
        raise BuildError(
            f"standalone releases require Python {STANDALONE_PYTHON_VERSION}; "
            f"builder is Python {python_version}"
        )

    names = artifact_names(version, target)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destinations = {
        "archive": output_dir / names.archive,
        "checksum": output_dir / names.checksum,
        "manifest": output_dir / names.manifest,
    }
    existing = [path for path in destinations.values() if path.exists()]
    if existing and not force:
        rendered = ", ".join(str(path) for path in existing)
        raise BuildError(
            f"refusing to replace existing artifact(s): {rendered}; use --force"
        )

    epoch = source_date_epoch()
    with tempfile.TemporaryDirectory(prefix=f"{names.stem}-") as temporary:
        build_root = Path(temporary)
        environment = os.environ.copy()
        environment["SOURCE_DATE_EPOCH"] = str(epoch)
        environment["PYTHONHASHSEED"] = "0"
        command = pyinstaller_command(
            python=python,
            repository=repository,
            build_root=build_root,
        )
        try:
            subprocess.run(command, cwd=repository, env=environment, check=True)
        except subprocess.CalledProcessError as exc:
            raise BuildError(
                f"PyInstaller failed with exit code {exc.returncode}"
            ) from exc

        executable = build_root / "dist" / names.executable
        if not executable.is_file() or executable.stat().st_size == 0:
            raise BuildError(f"PyInstaller did not produce {names.executable}")
        if target.platform != "win32":
            executable.chmod(
                executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        assert_native_executable(executable, target)
        frozen_distributions, frozen_native_files = audit_frozen_bundle(
            executable, python
        )

        expected_version = f"api429 {version}"
        try:
            smoke = subprocess.run(
                [str(executable), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError(
                f"standalone --version smoke test could not run: {exc}"
            ) from exc
        actual_version = smoke.stdout.strip()
        if smoke.returncode != 0 or actual_version != expected_version:
            raise BuildError(
                "standalone --version smoke test failed: "
                f"exit={smoke.returncode}, stdout={actual_version!r}, "
                f"stderr={smoke.stderr.strip()!r}; expected {expected_version!r}"
            )

        archive = build_root / names.archive
        create_archive(executable, archive, target, epoch=epoch)
        archive_digest = sha256_file(archive)
        executable_digest = sha256_file(executable)
        checksum = build_root / names.checksum
        checksum.write_bytes(f"{archive_digest}  {names.archive}\n".encode("ascii"))
        manifest: dict[str, object] = {
            "schema_version": 1,
            "package": PACKAGE_DISTRIBUTION,
            "version": version,
            "target": target.label,
            "platform": target.platform,
            "arch": target.arch,
            "executable": names.executable,
            "executable_sha256": executable_digest,
            "executable_size": executable.stat().st_size,
            "archive": names.archive,
            "archive_format": target.archive_suffix.removeprefix("."),
            "archive_sha256": archive_digest,
            "archive_size": archive.stat().st_size,
            "checksum": names.checksum,
            "source_date_epoch": epoch,
            "smoke": expected_version,
            "python": python_version,
            "pyinstaller": pyinstaller_version,
            "pyinstaller_hooks_contrib": hooks_version,
            "bundled_dependencies": bundled_dependencies,
            "frozen_distributions": frozen_distributions,
            "frozen_native_files": frozen_native_files,
        }
        manifest_path = build_root / names.manifest
        _write_json(manifest_path, manifest)

        _publish(archive, destinations["archive"])
        _publish(checksum, destinations["checksum"])
        _publish(manifest_path, destinations["manifest"])
    return manifest, destinations["manifest"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and package API429 for the current native platform"
    )
    parser.add_argument("--platform", choices=SUPPORTED_PLATFORMS)
    parser.add_argument("--arch", choices=SUPPORTED_ARCHITECTURES)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist") / "standalone",
        help="artifact destination (default: dist/standalone)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="atomically replace artifacts for this exact version and target",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = resolve_target(args.platform, args.arch)
        manifest, manifest_path = build_standalone(
            target=target,
            output_dir=args.output_dir,
            force=args.force,
        )
    except (BuildError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = dict(manifest)
    result["manifest"] = str(manifest_path)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
