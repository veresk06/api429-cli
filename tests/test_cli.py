from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from api429_cli.cli import main
from api429_cli.client import APIResponse


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.base_url = str(kwargs.get("base_url") or "https://gateway.api429.com")
        self.calls: list[tuple[str, Any]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def balance(self) -> APIResponse:
        self.calls.append(("balance", None))
        return APIResponse(
            {
                "token_last4": "abcd",
                "balance": {
                    "balance_usd": 9.5,
                    "available_balance_usd": 9.5,
                    "low_balance": False,
                },
            },
            200,
            {},
        )

    def models(self) -> APIResponse:
        self.calls.append(("models", None))
        return APIResponse(
            {
                "object": "list",
                "data": [
                    {"id": "image-model", "owned_by": "test"},
                    {"id": "video-model", "owned_by": "test"},
                ],
            },
            200,
            {},
        )

    def model_help(self, model: str, *, markdown: bool = False) -> APIResponse:
        self.calls.append(("model_help", (model, markdown)))
        data: Any = "# Help" if markdown else {"model_id": model}
        return APIResponse(data, 200, {})

    def validate_key(self, token: str) -> dict[str, Any]:
        self.calls.append(("validate_key", token))
        return self.balance().data

    def login(self, *, email: str, password: str) -> dict[str, Any]:
        self.calls.append(("login", (email, password)))
        return {
            "account": {"email": email},
            "api_key": "gb_logged_in",
            "token_last4": "d_in",
            "balance_usd": 10,
        }

    def usage(self, *, daily: bool = False) -> APIResponse:
        self.calls.append(("usage", daily))
        return APIResponse({"calls": {"total": 0}, "tokens": {}}, 200, {})

    def generate_image(self, payload: dict[str, Any]) -> APIResponse:
        self.calls.append(("generate_image", payload))
        return APIResponse(
            {"created": 1, "data": [{"url": "https://cdn.example/image.png"}]},
            200,
            {},
        )

    def edit_image(self, **kwargs: Any) -> APIResponse:
        self.calls.append(("edit_image", kwargs))
        return APIResponse({"data": [{"b64_json": "aGVsbG8="}]}, 200, {})

    def generate_video(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> APIResponse:
        self.calls.append(("generate_video", (payload, idempotency_key)))
        return APIResponse(
            {"id": "job_video", "status": "queued", "object": "video.generation.job"},
            202,
            {},
        )

    def wait_for_job(self, job_id: str, **kwargs: Any) -> APIResponse:
        self.calls.append(("wait_for_job", (job_id, kwargs)))
        return APIResponse(
            {
                "object": "video.generation",
                "data": [{"url": "https://cdn.example/video.mp4"}],
            },
            200,
            {},
        )

    def job(self, job_id: str) -> APIResponse:
        self.calls.append(("job", job_id))
        return APIResponse({"id": job_id, "status": "running"}, 200, {})

    def cancel_job(self, job_id: str) -> APIResponse:
        self.calls.append(("cancel_job", job_id))
        return APIResponse({"id": job_id, "status": "cancelled"}, 200, {})

    def download(self, url: str, destination: Path, *, overwrite: bool = False) -> Path:
        self.calls.append(("download", (url, destination, overwrite)))
        destination.write_bytes(b"media")
        return destination


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setenv("API429_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("API429_API_KEY", "gb_test")


def test_models_list_json_is_valid_machine_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--json", "models", "list"], client_factory=FakeClient)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["object"] == "list"
    assert captured.err == ""


def test_image_generate_requires_explicit_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["image", "generate", "--prompt", "fox", "--yes"],
        client_factory=FakeClient,
    )

    assert exit_code == 2
    assert "explicit model is required" in capsys.readouterr().err
    assert not any(
        call[0] == "generate_image" for call in FakeClient.instances[-1].calls
    )


def test_image_generate_preflights_and_submits_explicit_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "image",
            "generate",
            "--model",
            "image-model",
            "--prompt",
            "a fox",
            "--resolution",
            "2K",
            "--param",
            "seed=7",
            "--yes",
        ],
        client_factory=FakeClient,
    )

    assert exit_code == 0
    client = FakeClient.instances[-1]
    names = [call[0] for call in client.calls]
    assert names[:2] == ["balance", "models"]
    payload = next(value for name, value in client.calls if name == "generate_image")
    assert payload == {
        "model": "image-model",
        "prompt": "a fox",
        "resolution": "2K",
        "seed": 7,
    }
    assert "exact price: unavailable" in capsys.readouterr().err


def test_video_generate_uses_supplied_idempotency_key_and_returns_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--json",
            "video",
            "generate",
            "--model",
            "video-model",
            "--prompt",
            "waves",
            "--idempotency-key",
            "stable-1",
            "--yes",
        ],
        client_factory=FakeClient,
    )

    assert exit_code == 0
    client = FakeClient.instances[-1]
    payload, key = next(
        value for name, value in client.calls if name == "generate_video"
    )
    assert payload == {"model": "video-model", "prompt": "waves"}
    assert key == "stable-1"
    output = json.loads(capsys.readouterr().out)
    assert output["job_id"] == "job_video"
    assert output["idempotency_key"] == "stable-1"


def test_video_output_implies_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "result.mp4"
    exit_code = main(
        [
            "video",
            "generate",
            "--model",
            "video-model",
            "--prompt",
            "waves",
            "--yes",
            "--output",
            str(destination),
        ],
        client_factory=FakeClient,
    )

    assert exit_code == 0
    client = FakeClient.instances[-1]
    assert any(name == "wait_for_job" for name, _ in client.calls)
    assert destination.read_bytes() == b"media"
    assert "Saved" in capsys.readouterr().out


def test_token_stdin_never_echoes_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("API429_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("gb_super_secret\n"))

    exit_code = main(
        ["--json", "auth", "login", "--token-stdin", "--no-save"],
        client_factory=FakeClient,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "gb_super_secret" not in captured.out + captured.err
    assert json.loads(captured.out)["authenticated"] is True


def test_unknown_model_stops_before_paid_submission(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "image",
            "generate",
            "--model",
            "missing-model",
            "--prompt",
            "fox",
            "--yes",
        ],
        client_factory=FakeClient,
    )

    assert exit_code == 2
    client = FakeClient.instances[-1]
    assert not any(name == "generate_image" for name, _ in client.calls)
    assert "not currently executable" in capsys.readouterr().err


def test_interactive_paid_prompt_keeps_json_stdout_clean(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", TTYInput("yes\n"))

    exit_code = main(
        [
            "--json",
            "image",
            "generate",
            "--model",
            "image-model",
            "--prompt",
            "fox",
        ],
        client_factory=FakeClient,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["response"]["created"] == 1
    assert "Submit and accept" not in captured.out
    assert "Submit and accept" in captured.err


def test_jobs_list_reports_local_operation_without_api_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    generated = main(
        [
            "image",
            "generate",
            "--model",
            "image-model",
            "--prompt",
            "fox",
            "--yes",
        ],
        client_factory=FakeClient,
    )
    assert generated == 0
    capsys.readouterr()

    exit_code = main(["--json", "jobs", "list"], client_factory=FakeClient)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["object"] == "local.operation.list"
    assert payload["data"][0]["model"] == "image-model"
    assert not FakeClient.instances[-1].calls
