from __future__ import annotations

import io
import json
import socket
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

import api429_cli.client as client_module
from api429_cli.client import API429Client
from api429_cli.errors import (
    AmbiguousRequestError,
    APIError,
    ConfigurationError,
    TransportError,
)

BASE_URL = "https://gateway.api429.com"
API_KEY = "gb_test_secret"


class _ExternalResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        fail_after_first_read: bool = False,
    ) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = dict(headers or {})
        self._fail_after_first_read = fail_after_first_read
        self._reads = 0

    def getheader(self, name: str) -> str | None:
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())

    def read(self, size: int = -1) -> bytes:
        if self._fail_after_first_read and self._reads:
            raise OSError("connection reset")
        self._reads += 1
        return self._body.read(size)


def _install_external_transport(
    monkeypatch: pytest.MonkeyPatch,
    response: _ExternalResponse,
    *,
    addresses: tuple[str, ...] = ("93.184.216.34",),
    failing_addresses: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    monkeypatch.setattr(
        client_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
            for address in addresses
        ],
    )
    calls: list[dict[str, Any]] = []

    class FakeConnection:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.closed = False

        def request(
            self,
            method: str,
            target: str,
            *,
            headers: dict[str, str],
        ) -> None:
            calls.append(
                {
                    "method": method,
                    "target": target,
                    "headers": headers,
                    **self.kwargs,
                }
            )
            if self.kwargs["address"].sockaddr[0] in failing_addresses:
                raise OSError("connection refused")

        def getresponse(self) -> _ExternalResponse:
            return response

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(client_module, "_PinnedHTTPSConnection", FakeConnection)
    return calls


@contextmanager
def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = API_KEY,
) -> Iterator[API429Client]:
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        yield API429Client(
            base_url=BASE_URL,
            api_key=api_key,
            http_client=http_client,
        )


def test_authenticated_request_exposes_structured_error_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "Too many requests",
                    "code": "rate_limited",
                    "trace_id": "trace-body",
                }
            },
            headers={"X-Request-ID": "request-header", "Retry-After": "7"},
        )

    with _mock_client(handler) as client, pytest.raises(APIError) as raised:
        client.models()

    error = raised.value
    assert error.status_code == 429
    assert error.message == "Too many requests"
    assert error.code == "rate_limited"
    assert error.trace_id == "trace-body"
    assert error.request_id == "request-header"
    assert error.retry_after == "7"
    assert error.exit_code == 6
    assert "gb_test_secret" not in str(error)


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ({"error": "legacy error"}, "legacy error"),
        ({"detail": "legacy detail"}, "legacy detail"),
        (
            {"detail": [{"msg": "model is required"}, {"msg": "prompt is too short"}]},
            "model is required; prompt is too short",
        ),
        ("upstream failed", "upstream failed"),
    ],
)
def test_supported_error_response_formats(
    payload: object, expected_message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(502, text=payload, request=request)
        return httpx.Response(422, json=payload, request=request)

    with _mock_client(handler) as client, pytest.raises(APIError) as raised:
        client.balance()

    assert raised.value.message == expected_message
    assert raised.value.payload == payload


def test_authenticated_request_requires_an_api_key_before_transport() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("transport must not be called without an API key")

    with _mock_client(handler, api_key=None) as client:
        with pytest.raises(ConfigurationError, match="No API key configured"):
            client.models()


def test_paid_post_transport_failure_is_reported_as_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        raise httpx.ReadTimeout("response was lost", request=request)

    with _mock_client(handler) as client:
        with pytest.raises(AmbiguousRequestError) as raised:
            client.generate_image({"model": "image-model", "prompt": "a lighthouse"})

    assert raised.value.exit_code == 3
    assert raised.value.idempotency_key is None
    assert "outcome is unknown" in str(raised.value)
    assert "no idempotency guarantee" in str(raised.value)


def test_non_paid_transport_failure_is_not_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with _mock_client(handler) as client:
        with pytest.raises(ConfigurationError, match="Could not reach API429"):
            client.models()


def test_paid_server_5xx_is_conservatively_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "upstream timeout"}, request=request)

    with _mock_client(handler) as client:
        with pytest.raises(AmbiguousRequestError, match="may already have accepted"):
            client.generate_image({"model": "image-model", "prompt": "storm"})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.api429.com",
        "https://user:pass@gateway.api429.com",
        "https://gateway.api429.com/v1",
        "https://gateway.api429.com?token=x",
        "https://gateway.api429.com#fragment",
    ],
)
def test_unsafe_base_urls_are_rejected(base_url: str) -> None:
    with pytest.raises(ConfigurationError):
        API429Client(base_url=base_url, api_key=API_KEY)


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_loopback_http_base_url_is_allowed_for_development(base_url: str) -> None:
    client = API429Client(base_url=base_url, api_key=API_KEY)
    client.close()


def test_login_then_issue_key_uses_only_the_portal_session_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/client/login":
            assert "Authorization" not in request.headers
            assert json.loads(request.content) == {
                "email": "person@example.com",
                "password": "correct horse",
            }
            return httpx.Response(
                200,
                json={
                    "portal_session_token": "portal-session",
                    "api_key": None,
                },
                request=request,
            )
        assert request.url.path == "/api/client/account-api-key"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer portal-session"
        return httpx.Response(
            200,
            json={"api_key": "gb_issued", "token_last4": "sued"},
            request=request,
        )

    with _mock_client(handler, api_key=None) as client:
        result = client.login(email="person@example.com", password="correct horse")

    assert result == {"api_key": "gb_issued", "token_last4": "sued"}
    assert [request.url.path for request in requests] == [
        "/api/client/login",
        "/api/client/account-api-key",
    ]


def test_login_does_not_leak_a_stale_gateway_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/client/login"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"api_key": "gb_replaced"}, request=request)

    with _mock_client(handler, api_key="gb_stale") as client:
        result = client.login(email="person@example.com", password="new password")

    assert result["api_key"] == "gb_replaced"


def test_path_identifiers_are_url_encoded_as_single_segments() -> None:
    raw_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_paths.append(request.url.raw_path)
        return httpx.Response(200, json={"ok": True}, request=request)

    with _mock_client(handler) as client:
        client.model_help("vendor/model id?#", markdown=True)
        client.job("job/id ?#")

    assert raw_paths == [
        b"/v1/models/vendor%2Fmodel%20id%3F%23/help?format=markdown",
        b"/v1/balancer/jobs/job%2Fid%20%3F%23",
    ]


@pytest.mark.parametrize("status_code", [200, 202])
def test_image_generation_accepts_sync_and_async_responses(status_code: int) -> None:
    payload = {"model": "image-model", "prompt": "a brass compass", "n": 2}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/images/generations"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert json.loads(request.content) == payload
        return httpx.Response(
            status_code,
            json={"status": "queued"} if status_code == 202 else {"data": []},
            request=request,
        )

    with _mock_client(handler) as client:
        response = client.generate_image(payload)

    assert response.status_code == status_code
    assert response.data == (
        {"status": "queued"} if status_code == 202 else {"data": []}
    )


def test_video_generation_sends_idempotency_key() -> None:
    payload = {"model": "video-model", "prompt": "ocean at dusk"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/videos/generations"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert request.headers["Idempotency-Key"] == "video-attempt-123"
        assert json.loads(request.content) == payload
        return httpx.Response(202, json={"job_id": "job-1"}, request=request)

    with _mock_client(handler) as client:
        response = client.generate_video(payload, idempotency_key="video-attempt-123")

    assert response.status_code == 202
    assert response.data == {"job_id": "job-1"}


def test_video_transport_failure_preserves_idempotency_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "video-attempt-456"
        raise httpx.WriteError("connection closed", request=request)

    with _mock_client(handler) as client:
        with pytest.raises(AmbiguousRequestError) as raised:
            client.generate_video(
                {"model": "video-model", "prompt": "snow"},
                idempotency_key="video-attempt-456",
            )

    assert raised.value.idempotency_key == "video-attempt-456"
    assert "video-attempt-456" in str(raised.value)


def test_wait_for_job_polls_202_until_200_and_honors_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []
    clock = iter([10.0, 10.0, 11.0, 15.0])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.raw_path == b"/v1/balancer/jobs/job%2F1/result"
        if calls == 1:
            return httpx.Response(
                202,
                json={"status": "running", "retry_after_seconds": 2},
                headers={"Retry-After": "4"},
                request=request,
            )
        return httpx.Response(
            200, json={"data": [{"url": "https://cdn/result"}]}, request=request
        )

    with _mock_client(handler) as client:
        response = client.wait_for_job(
            "job/1",
            timeout=10,
            interval=1,
            sleeper=sleeps.append,
            monotonic=lambda: next(clock),
        )

    assert calls == 2
    assert sleeps == [4.0]
    assert response.status_code == 200
    assert response.data == {"data": [{"url": "https://cdn/result"}]}


def test_wait_for_job_turns_result_409_into_terminal_job_error() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/result"):
            return httpx.Response(
                409,
                json={"error": {"message": "result unavailable"}},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "job_id": "job-2",
                "status": "failed",
                "error": {
                    "code": "provider_failed",
                    "message": "Provider rejected input",
                },
            },
            request=request,
        )

    with _mock_client(handler) as client, pytest.raises(APIError) as raised:
        client.wait_for_job("job-2", sleeper=lambda _: None)

    assert paths == [
        "/v1/balancer/jobs/job-2/result",
        "/v1/balancer/jobs/job-2",
    ]
    assert raised.value.status_code == 409
    assert raised.value.message == "Provider rejected input"
    assert raised.value.code == "provider_failed"
    assert raised.value.payload["status"] == "failed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 0.0])
def test_wait_rejects_non_finite_or_non_positive_values(value: float) -> None:
    with _mock_client(
        lambda request: httpx.Response(200, json={}, request=request)
    ) as client:
        with pytest.raises(ConfigurationError, match="greater than zero"):
            client.wait_for_job("job-1", timeout=value, interval=1)
        with pytest.raises(ConfigurationError, match="greater than zero"):
            client.wait_for_job("job-1", timeout=10, interval=value)


def test_image_edit_uses_repeated_image_multipart_fields(tmp_path: Path) -> None:
    first = tmp_path / "front.png"
    second = tmp_path / "side.jpg"
    mask = tmp_path / "mask.png"
    first.write_bytes(b"first-image")
    second.write_bytes(b"second-image")
    mask.write_bytes(b"mask-image")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/images/edits"
        assert request.headers["content-type"].startswith(
            "multipart/form-data; boundary="
        )
        body = request.read()
        assert body.count(b'name="image"') == 2
        assert body.count(b'name="mask"') == 1
        assert b'name="image[]"' not in body
        assert b'filename="front.png"' in body
        assert b'filename="side.jpg"' in body
        assert b'filename="mask.png"' in body
        assert b'name="model"\r\n\r\nimage-edit-model' in body
        assert b'name="prompt"\r\n\r\nreplace the sky' in body
        assert b'name="seed"\r\n\r\n42' in body
        return httpx.Response(200, json={"data": []}, request=request)

    with _mock_client(handler) as client:
        response = client.edit_image(
            model="image-edit-model",
            prompt="replace the sky",
            image_paths=[first, second],
            mask_path=mask,
            fields={"seed": 42, "ignored": None},
        )

    assert response.status_code == 200


def test_external_download_pins_validated_ip_and_preserves_tls_hostname_and_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"generated-media"
    response = _ExternalResponse(body)
    calls = _install_external_transport(monkeypatch, response)

    destination = tmp_path / "image.png"
    with _mock_client(
        lambda _: pytest.fail("httpx must not fetch external media")
    ) as client:
        result = client.download(
            "https://cdn.example.net/output/image.png?signature=secret",
            destination,
        )

    assert result == destination
    assert destination.read_bytes() == body
    assert len(calls) == 1
    assert calls[0]["address"].sockaddr == ("93.184.216.34", 443)
    assert calls[0]["server_hostname"] == "cdn.example.net"
    assert calls[0]["context"].check_hostname is True
    assert calls[0]["context"].verify_mode == client_module.ssl.CERT_REQUIRED
    assert calls[0]["target"] == "/output/image.png?signature=secret"
    assert calls[0]["headers"]["Host"] == "cdn.example.net"
    assert "Authorization" not in calls[0]["headers"]


def test_external_download_cannot_be_dns_rebound_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = 0

    def rebinding_resolver(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ]

    monkeypatch.setattr(client_module.socket, "getaddrinfo", rebinding_resolver)
    connected_addresses: list[tuple[Any, ...]] = []

    class FakeConnection:
        def __init__(self, **kwargs: Any) -> None:
            connected_addresses.append(kwargs["address"].sockaddr)

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def getresponse(self) -> _ExternalResponse:
            return _ExternalResponse(b"safe")

        def close(self) -> None:
            return None

    monkeypatch.setattr(client_module, "_PinnedHTTPSConnection", FakeConnection)

    with _mock_client(
        lambda _: pytest.fail("httpx must not fetch external media")
    ) as client:
        client.download("https://rebind.invalid/media", tmp_path / "media")

    assert resolutions == 1
    assert connected_addresses == [("93.184.216.34", 443)]
    assert (tmp_path / "media").read_bytes() == b"safe"


def test_download_refuses_dns_name_resolving_to_private_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api429_cli.client.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )

    with _mock_client(lambda _: pytest.fail("transport must not be called")) as client:
        with pytest.raises(ConfigurationError, match="resolves to a private address"):
            client.download("https://malicious.invalid/file", tmp_path / "file")


def test_download_refuses_private_ip_literal_and_scheme_relative_url(
    tmp_path: Path,
) -> None:
    with _mock_client(lambda _: pytest.fail("transport must not be called")) as client:
        with pytest.raises(ConfigurationError, match="private address"):
            client.download("https://127.0.0.1/file", tmp_path / "private")
        with pytest.raises(ConfigurationError, match="scheme-relative"):
            client.download("//127.0.0.1/file", tmp_path / "relative")


def test_external_download_tries_each_validated_public_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_external_transport(
        monkeypatch,
        _ExternalResponse(b"fallback"),
        addresses=("93.184.216.34", "142.250.74.78"),
        failing_addresses=frozenset({"93.184.216.34"}),
    )

    with _mock_client(
        lambda _: pytest.fail("httpx must not fetch external media")
    ) as client:
        client.download("https://cdn.invalid/file", tmp_path / "file")

    assert [call["address"].sockaddr[0] for call in calls] == [
        "93.184.216.34",
        "142.250.74.78",
    ]
    assert (tmp_path / "file").read_bytes() == b"fallback"


def test_external_redirect_is_not_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_external_transport(
        monkeypatch,
        _ExternalResponse(
            status=302,
            headers={"Location": "https://127.0.0.1/private"},
        ),
    )

    with _mock_client(
        lambda _: pytest.fail("httpx must not fetch external media")
    ) as client:
        with pytest.raises(APIError) as raised:
            client.download("https://cdn.invalid/file", tmp_path / "file")

    assert raised.value.status_code == 302
    assert len(calls) == 1
    assert not (tmp_path / "file").exists()


def test_external_download_size_cap_and_failure_preserve_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "file"
    destination.write_bytes(b"original")
    oversized = _ExternalResponse(
        b"must not be read",
        headers={"Content-Length": str(client_module.MAX_DOWNLOAD_BYTES + 1)},
    )
    _install_external_transport(monkeypatch, oversized)

    with _mock_client(
        lambda _: pytest.fail("httpx must not fetch external media")
    ) as client:
        with pytest.raises(ConfigurationError, match="2 GiB"):
            client.download(
                "https://cdn.invalid/oversized",
                destination,
                overwrite=True,
            )

    assert destination.read_bytes() == b"original"

    interrupted = _ExternalResponse(b"partial", fail_after_first_read=True)
    _install_external_transport(monkeypatch, interrupted)
    with _mock_client(
        lambda _: pytest.fail("httpx must not fetch external media")
    ) as client:
        with pytest.raises(TransportError, match="failed while downloading"):
            client.download(
                "https://cdn.invalid/interrupted",
                destination,
                overwrite=True,
            )

    assert destination.read_bytes() == b"original"


def test_pinned_connection_uses_validated_sockaddr_and_original_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class FakeRawSocket:
        def settimeout(self, value: float) -> None:
            events.append(("connect_timeout", value))

        def connect(self, sockaddr: tuple[Any, ...]) -> None:
            events.append(("connect", sockaddr))

        def close(self) -> None:
            events.append(("raw_close", None))

    class FakeTLSSocket:
        def settimeout(self, value: float) -> None:
            events.append(("read_timeout", value))

    class FakeContext:
        # Python 3.10/3.11 inspect these SSLContext attributes in
        # HTTPSConnection.__init__, while newer versions no longer require
        # them for this test double.
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(
            self,
            raw_socket: FakeRawSocket,
            *,
            server_hostname: str,
        ) -> FakeTLSSocket:
            events.append(("sni", server_hostname))
            return FakeTLSSocket()

    monkeypatch.setattr(
        client_module.socket,
        "socket",
        lambda *_args: FakeRawSocket(),
    )
    address = client_module._ResolvedAddress(
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        ("93.184.216.34", 443),
    )
    connection = client_module._PinnedHTTPSConnection(
        server_hostname="cdn.example.net",
        port=443,
        address=address,
        connect_timeout=7.0,
        read_timeout=30.0,
        context=FakeContext(),  # type: ignore[arg-type]
    )

    connection.connect()

    assert events == [
        ("connect_timeout", 7.0),
        ("connect", ("93.184.216.34", 443)),
        ("sni", "cdn.example.net"),
        ("read_timeout", 30.0),
    ]


def test_download_refuses_overwrite_unless_explicit(tmp_path: Path) -> None:
    destination = tmp_path / "result.bin"
    destination.write_bytes(b"original")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"replacement", request=request)

    with _mock_client(handler) as client:
        with pytest.raises(ConfigurationError, match="already exists"):
            client.download("/result.bin", destination)
        client.download("/result.bin", destination, overwrite=True)

    assert destination.read_bytes() == b"replacement"
