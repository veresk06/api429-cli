from __future__ import annotations

import hashlib
import importlib
import json
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
standalone = importlib.import_module("scripts.build_standalone")
BuildError = standalone.BuildError
Target = standalone.Target
artifact_names = standalone.artifact_names
assert_native_executable = standalone.assert_native_executable
validate_frozen_distributions = standalone.validate_frozen_distributions
create_archive = standalone.create_archive
executable_arches = standalone.executable_arches
pyinstaller_command = standalone.pyinstaller_command
resolve_target = standalone.resolve_target
source_date_epoch = standalone.source_date_epoch
RUNTIME_DISTRIBUTIONS = standalone.RUNTIME_DISTRIBUTIONS
STANDALONE_PYTHON_VERSION = standalone.STANDALONE_PYTHON_VERSION
load_legal_corpus = importlib.import_module("scripts.legal_corpus").load_legal_corpus
REPOSITORY = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_artifact_contract(platform: str, arch: str) -> None:
    target = Target(platform, arch)
    names = artifact_names("0.1.0", target)

    suffix = ".zip" if platform == "win32" else ".tar.gz"
    executable = "api429.exe" if platform == "win32" else "api429"
    assert names.stem == f"api429-v0.1.0-{platform}-{arch}"
    assert names.executable == executable
    assert names.archive == f"{names.stem}{suffix}"
    assert names.checksum == f"{names.archive}.sha256"
    assert names.manifest == f"{names.stem}.json"


def test_cross_compilation_is_rejected() -> None:
    with pytest.raises(BuildError, match="cross-compilation is not supported"):
        resolve_target("linux", "x64", host=Target("darwin", "arm64"))


@pytest.mark.parametrize("value", ["-1", "not-a-number"])
def test_invalid_source_date_epoch_is_rejected(value: str) -> None:
    with pytest.raises(BuildError, match="SOURCE_DATE_EPOCH"):
        source_date_epoch({"SOURCE_DATE_EPOCH": value})


def test_empty_environment_uses_stable_default_epoch() -> None:
    assert source_date_epoch({}) == 0


def test_pyinstaller_command_bundles_metadata_and_http_dependencies(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    command = pyinstaller_command(
        python=Path("python"), repository=repository, build_root=tmp_path / "build"
    )

    assert "--onefile" in command
    assert command[command.index("--copy-metadata") + 1] == "api429-cli"
    hidden_imports = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--hidden-import"
    ]
    assert hidden_imports == ["httpx", "certifi"]
    assert command[command.index("--collect-data") + 1] == "certifi"
    assert command[-1] == str(repository / "scripts" / "pyinstaller_entry.py")


def test_runtime_dependency_manifest_contract_is_complete() -> None:
    assert STANDALONE_PYTHON_VERSION == "3.13.13"
    assert RUNTIME_DISTRIBUTIONS == (
        "anyio",
        "certifi",
        "h11",
        "httpcore",
        "httpx",
        "idna",
    )


def test_frozen_distribution_allowlist_rejects_dev_dependencies() -> None:
    expected = {
        "api429-cli",
        "anyio",
        "certifi",
        "h11",
        "httpcore",
        "httpx",
        "idna",
    }
    assert validate_frozen_distributions(expected) == sorted(expected)

    with pytest.raises(BuildError, match="unexpected=\\['pytest'\\]"):
        validate_frozen_distributions(expected | {"pytest"})


def test_tar_archive_is_stable_and_contains_executable(tmp_path: Path) -> None:
    executable = tmp_path / "api429"
    executable.write_bytes(b"native-binary")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    target = Target("linux", "arm64")

    legal_files = load_legal_corpus(REPOSITORY)
    create_archive(executable, first, target, epoch=0, legal_files=legal_files)
    create_archive(executable, second, target, epoch=0, legal_files=legal_files)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, mode="r:gz") as bundle:
        member = bundle.getmember("api429")
        assert member.mode == 0o755
        assert member.mtime == 0
        extracted = bundle.extractfile(member)
        assert extracted is not None
        assert extracted.read() == b"native-binary"
        assert bundle.getnames() == [
            "api429",
            *(item.archive_path for item in legal_files),
        ]
        license_member = bundle.getmember("LICENSE")
        assert license_member.mode == 0o644
        assert license_member.mtime == 0


def test_zip_archive_is_stable_and_contains_executable(tmp_path: Path) -> None:
    executable = tmp_path / "api429.exe"
    executable.write_bytes(b"native-windows-binary")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    target = Target("win32", "x64")

    legal_files = load_legal_corpus(REPOSITORY)
    create_archive(executable, first, target, epoch=0, legal_files=legal_files)
    create_archive(executable, second, target, epoch=0, legal_files=legal_files)

    assert (
        hashlib.sha256(first.read_bytes()).digest()
        == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as bundle:
        assert bundle.namelist() == [
            "api429.exe",
            *(item.archive_path for item in legal_files),
        ]
        info = bundle.getinfo("api429.exe")
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert bundle.read("api429.exe") == b"native-windows-binary"
        license_info = bundle.getinfo("LICENSE")
        assert stat.S_IMODE(license_info.external_attr >> 16) == 0o644
        assert license_info.date_time == (1980, 1, 1, 0, 0, 0)


@pytest.mark.parametrize(
    ("platform", "arch", "header"),
    [
        ("darwin", "arm64", b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"),
        ("darwin", "x64", b"\xcf\xfa\xed\xfe\x07\x00\x00\x01"),
        (
            "linux",
            "arm64",
            b"\x7fELF\x02\x01" + (b"\0" * 12) + b"\xb7\x00",
        ),
        (
            "linux",
            "x64",
            b"\x7fELF\x02\x01" + (b"\0" * 12) + b"\x3e\x00",
        ),
    ],
)
def test_executable_arch_detection(
    tmp_path: Path, platform: str, arch: str, header: bytes
) -> None:
    executable = tmp_path / "api429"
    executable.write_bytes(header.ljust(128, b"\0"))

    assert executable_arches(executable, platform) == {arch}
    assert_native_executable(executable, Target(platform, arch))


def test_pe_arch_detection(tmp_path: Path) -> None:
    header = bytearray(256)
    header[:2] = b"MZ"
    header[60:64] = (128).to_bytes(4, byteorder="little")
    header[128:132] = b"PE\0\0"
    header[132:134] = (0xAA64).to_bytes(2, byteorder="little")
    executable = tmp_path / "api429.exe"
    executable.write_bytes(header)

    assert executable_arches(executable, "win32") == {"arm64"}


def test_manifest_shape_is_json_serializable() -> None:
    # This protects the release contract from accidental Path or bytes values.
    names = artifact_names("0.1.0", Target("darwin", "arm64"))
    payload = {
        "schema_version": 2,
        "archive": names.archive,
        "checksum": names.checksum,
        "executable": names.executable,
    }

    assert json.loads(json.dumps(payload)) == payload
