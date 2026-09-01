from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from api429_cli.operations import begin_operation, list_operations, update_operation


@pytest.fixture(autouse=True)
def isolated_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API429_CONFIG_FILE", str(tmp_path / "config.json"))


def test_operation_is_persisted_before_submit_without_request_body(
    tmp_path: Path,
) -> None:
    record = begin_operation(
        endpoint="videos.generations",
        model="video-model",
        request_payload={
            "prompt": "private prompt",
            "image": "data:image/png;base64,secret",
        },
        base_url="https://gateway.api429.com",
        idempotency_key="stable-key",
    )

    records = list_operations()
    assert len(records) == 1
    assert records[0]["id"] == record["id"]
    assert records[0]["status"] == "submitting"
    assert records[0]["idempotency_key"] == "stable-key"
    assert len(records[0]["request_sha256"]) == 64
    serialized = json.dumps(records[0])
    assert "private prompt" not in serialized
    assert "base64,secret" not in serialized


def test_operation_update_and_ordering() -> None:
    first = begin_operation(
        endpoint="images.generations",
        model="image-model",
        request_payload={"prompt": "one"},
        base_url="https://gateway.api429.com",
    )
    second = begin_operation(
        endpoint="videos.generations",
        model="video-model",
        request_payload={"prompt": "two"},
        base_url="https://gateway.api429.com",
        idempotency_key="key-2",
    )

    updated = update_operation(second["id"], status="accepted", job_id="job-2")

    assert updated["status"] == "accepted"
    assert updated["job_id"] == "job-2"
    records = list_operations(limit=1)
    assert records[0]["id"] == second["id"]
    assert records[0]["job_id"] == "job-2"
    assert first["id"] != second["id"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_operation_files_are_private(tmp_path: Path) -> None:
    record = begin_operation(
        endpoint="images.generations",
        model="image-model",
        request_payload={"prompt": "fox"},
        base_url="https://gateway.api429.com",
    )
    path = tmp_path / "operations" / f"{record['id']}.json"

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_operation_id_cannot_escape_directory() -> None:
    with pytest.raises(ValueError, match="invalid operation id"):
        update_operation("../../credentials", status="failed")
