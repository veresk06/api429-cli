from __future__ import annotations

import base64
import hashlib
import io
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from api429_cli.client import API429Client
from api429_cli.errors import ConfigurationError
from api429_cli.output import (
    extract_media_items,
    json_dump,
    sanitize_for_display,
    save_media,
)


@contextmanager
def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[API429Client]:
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        yield API429Client(
            base_url="https://gateway.api429.com",
            api_key="gb_test_secret",
            http_client=http_client,
        )


def _unexpected_request(_: httpx.Request) -> httpx.Response:
    pytest.fail("inline media must not make an HTTP request")


def test_sanitize_and_json_dump_redact_secrets_and_inline_media() -> None:
    inline = base64.b64encode(b"private image bytes").decode("ascii")
    data_uri = f"data:image/png;base64,{inline}"
    payload = {
        "api_key": "gb_visible_only_if_broken",
        "Authorization": "Bearer another-secret",
        "nested": [
            {
                "portal_session_token": "portal-secret",
                "password": "password-secret",
                "b64_json": inline,
                "uri": data_uri,
                "status": "succeeded",
                "delete_url": "https://cdn.example/delete?token=secret",
                "url": (
                    "https://cdn.example/image.png?width=1024&"
                    "X-Amz-Credential=account-secret&"
                    "X-Amz-Signature=signature-secret&token=download-secret"
                ),
            }
        ],
    }

    sanitized = sanitize_for_display(payload)

    assert sanitized["api_key"] == "[redacted]"
    assert sanitized["Authorization"] == "[redacted]"
    assert sanitized["nested"][0]["portal_session_token"] == "[redacted]"
    assert sanitized["nested"][0]["password"] == "[redacted]"
    assert sanitized["nested"][0]["b64_json"] == (
        f"[base64 omitted: {len(inline)} characters]"
    )
    assert sanitized["nested"][0]["uri"] == (
        f"[data URI omitted: {len(data_uri)} characters]"
    )
    assert sanitized["nested"][0]["status"] == "succeeded"
    assert sanitized["nested"][0]["delete_url"] == "[redacted]"
    sanitized_url = sanitized["nested"][0]["url"]
    assert sanitized_url.startswith("https://cdn.example/image.png?width=1024&")
    assert sanitized_url.count("%5Bredacted%5D") == 3
    assert "account-secret" not in sanitized_url
    assert "signature-secret" not in sanitized_url
    assert "download-secret" not in sanitized_url

    stream = io.StringIO()
    json_dump(payload, stream=stream)
    rendered = stream.getvalue()
    assert rendered.endswith("\n")
    for secret in (
        "gb_visible_only_if_broken",
        "another-secret",
        "portal-secret",
        "password-secret",
        inline,
        data_uri,
        "account-secret",
        "signature-secret",
        "download-secret",
    ):
        assert secret not in rendered


def test_extract_media_items_supports_all_public_media_shapes() -> None:
    payload = {
        "data": [{"b64_json": "Zmlyc3Q=", "mime_type": "image/png"}],
        "videos": [
            {"video_url": "https://cdn.example/video.mp4", "mimeType": "video/mp4"}
        ],
        "images": [{"uri": "data:image/webp;base64,c2Vjb25k"}],
        "outputs": [{"result_url": "https://cdn.example/result.bin"}],
    }

    assert extract_media_items(payload) == [
        {"kind": "base64", "value": "Zmlyc3Q=", "mime_type": "image/png"},
        {
            "kind": "url",
            "value": "https://cdn.example/video.mp4",
            "mime_type": "video/mp4",
        },
        {
            "kind": "data_uri",
            "value": "data:image/webp;base64,c2Vjb25k",
            "mime_type": "",
        },
        {"kind": "url", "value": "https://cdn.example/result.bin", "mime_type": ""},
    ]


def test_save_raw_base64_media_atomically_with_metadata(tmp_path: Path) -> None:
    raw = b"\x89PNG\r\n\x1a\nmock-png"
    payload = {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]}

    with _mock_client(_unexpected_request) as client:
        saved = save_media(client, payload, tmp_path / "generated")

    destination = tmp_path / "generated.png"
    assert destination.read_bytes() == raw
    assert saved == [
        {
            "path": str(destination.resolve()),
            "mime_type": "image/png",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    ]
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".generated.png.*.tmp"))


def test_save_data_uri_decodes_parameters_and_uses_its_mime_suffix(
    tmp_path: Path,
) -> None:
    raw = "Привет, API429".encode()
    encoded = base64.b64encode(raw).decode("ascii")
    payload = {"images": [{"url": f"data:text/plain;charset=utf-8;base64,{encoded}"}]}

    with _mock_client(_unexpected_request) as client:
        saved = save_media(client, payload, tmp_path / "message")

    destination = tmp_path / "message.txt"
    assert destination.read_bytes() == raw
    assert saved[0]["path"] == str(destination.resolve())
    assert saved[0]["mime_type"] == "text/plain"
    assert saved[0]["bytes"] == len(raw)
    assert saved[0]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_save_multiple_inline_items_numbers_each_destination(tmp_path: Path) -> None:
    first = base64.b64encode(b"first").decode("ascii")
    second = base64.b64encode(b"second").decode("ascii")
    payload = {
        "data": [
            {"b64_json": first, "mime_type": "image/png"},
            {"url": f"data:image/webp;base64,{second}"},
        ]
    }

    with _mock_client(_unexpected_request) as client:
        saved = save_media(client, payload, tmp_path / "asset")

    assert [Path(item["path"]).name for item in saved] == [
        "asset-1.png",
        "asset-2.webp",
    ]
    assert (tmp_path / "asset-1.png").read_bytes() == b"first"
    assert (tmp_path / "asset-2.webp").read_bytes() == b"second"


def test_save_url_delegates_external_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"remote-video"
    requested: list[tuple[str, Path, bool]] = []

    def download(
        _client: API429Client,
        url: str,
        destination: Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        requested.append((url, destination, overwrite))
        destination.write_bytes(raw)
        return destination

    monkeypatch.setattr(API429Client, "download", download)

    with _mock_client(_unexpected_request) as client:
        saved = save_media(
            client,
            {"videos": [{"url": "https://media.cdn.example/generated/video.mp4"}]},
            tmp_path / "download",
        )

    destination = tmp_path / "download.mp4"
    assert destination.read_bytes() == raw
    assert saved[0]["path"] == str(destination.resolve())
    assert saved[0]["mime_type"] == "video/mp4"
    assert requested == [
        (
            "https://media.cdn.example/generated/video.mp4",
            destination,
            False,
        )
    ]


def test_save_media_refuses_overwrite_unless_force(tmp_path: Path) -> None:
    destination = tmp_path / "generated.png"
    destination.write_bytes(b"old")
    payload = {"data": [{"b64_json": base64.b64encode(b"new").decode("ascii")}]}

    with _mock_client(_unexpected_request) as client:
        with pytest.raises(ConfigurationError, match="already exists"):
            save_media(client, payload, destination)
        save_media(client, payload, destination, overwrite=True)

    assert destination.read_bytes() == b"new"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"b64_json": "%%%not-base64%%%"}]},
        {"images": [{"url": "data:image/png,not-base64"}]},
    ],
)
def test_invalid_inline_media_is_rejected_without_an_output_file(
    tmp_path: Path,
    payload: object,
) -> None:
    requested = tmp_path / "invalid.png"

    with _mock_client(_unexpected_request) as client:
        with pytest.raises(ConfigurationError):
            save_media(client, payload, requested)

    assert not requested.exists()
