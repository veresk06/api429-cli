from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_standalone import Target, artifact_names, create_archive
from scripts.stage_npm_binaries import (
    TARGETS,
    StagingError,
    sha256_bytes,
    sha256_file,
    stage_all,
)


def _write_fixture_set(root: Path) -> tuple[Path, Path, bytes]:
    artifacts = root / "artifacts"
    npm_root = root / "npm"
    artifacts.mkdir()
    (npm_root / "packages" / "cli").mkdir(parents=True)
    (npm_root / "packages" / "cli" / "package.json").write_text(
        '{"name":"@api429/cli","version":"0.1.0"}\n', encoding="utf-8"
    )
    payload = b"synthetic-native-api429"

    for npm_target in TARGETS:
        package = npm_root / "packages" / npm_target.package_directory
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": f"@api429/{npm_target.package_directory}",
                    "version": "0.1.0",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        platform, arch = npm_target.target.split("-", maxsplit=1)
        target = Target(platform, arch)
        names = artifact_names("0.1.0", target)
        executable = root / f"{npm_target.target}-{npm_target.executable}"
        executable.write_bytes(payload)
        archive = artifacts / names.archive
        create_archive(executable, archive, target, epoch=0)
        manifest = {
            "schema_version": 1,
            "package": "api429-cli",
            "version": "0.1.0",
            "target": npm_target.target,
            "platform": platform,
            "arch": arch,
            "executable": npm_target.executable,
            "executable_sha256": sha256_bytes(payload),
            "executable_size": len(payload),
            "archive": names.archive,
            "archive_sha256": sha256_file(archive),
            "archive_size": archive.stat().st_size,
            "python": "3.13.7",
            "bundled_dependencies": {
                "anyio": "4.14.2",
                "certifi": "2026.7.22",
                "h11": "0.16.0",
                "httpcore": "1.0.9",
                "httpx": "0.28.1",
                "idna": "3.19",
            },
            "frozen_distributions": [
                "anyio",
                "api429-cli",
                "certifi",
                "h11",
                "httpcore",
                "httpx",
                "idna",
            ],
            "frozen_native_files": ["libpython3.13.synthetic"],
        }
        (artifacts / names.manifest).write_text(
            json.dumps(manifest) + "\n", encoding="utf-8"
        )
    return artifacts, npm_root, payload


def test_stage_all_verifies_then_writes_six_payloads(tmp_path: Path) -> None:
    artifacts, npm_root, payload = _write_fixture_set(tmp_path)

    result = stage_all(artifacts, npm_root, force=False)

    assert len(result) == 6
    for target in TARGETS:
        binary = (
            npm_root / "packages" / target.package_directory / "bin" / target.executable
        )
        assert binary.read_bytes() == payload
        assert binary.stat().st_mode & 0o111


def test_stage_all_does_not_write_partial_payloads_on_verification_error(
    tmp_path: Path,
) -> None:
    artifacts, npm_root, _payload = _write_fixture_set(tmp_path)
    corrupt = next(artifacts.glob("*linux-x64.tar.gz"))
    corrupt.write_bytes(corrupt.read_bytes() + b"corrupt")

    with pytest.raises(StagingError, match="size is"):
        stage_all(artifacts, npm_root, force=False)

    assert not list((npm_root / "packages").glob("*/bin/api429*"))


def test_stage_all_rejects_manifest_archive_path_traversal(tmp_path: Path) -> None:
    artifacts, npm_root, _payload = _write_fixture_set(tmp_path)
    manifest_path = next(artifacts.glob("*darwin-arm64.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archive"] = "../escape.tar.gz"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(StagingError, match="safe file name"):
        stage_all(artifacts, npm_root, force=False)


def test_stage_all_rejects_incomplete_dependency_manifest(tmp_path: Path) -> None:
    artifacts, npm_root, _payload = _write_fixture_set(tmp_path)
    manifest_path = next(artifacts.glob("*darwin-arm64.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["bundled_dependencies"]["certifi"]
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(StagingError, match="incomplete bundled dependency"):
        stage_all(artifacts, npm_root, force=False)
