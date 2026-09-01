from __future__ import annotations

import http.client
import ipaddress
import json
import math
import mimetypes
import os
import socket
import ssl
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import __version__
from .errors import AmbiguousRequestError, APIError, ConfigurationError, TransportError

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "ambiguous"})
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ResolvedAddress:
    family: int
    socket_type: int
    protocol: int
    sockaddr: tuple[Any, ...]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose peer address cannot be changed by a second DNS lookup."""

    def __init__(
        self,
        *,
        server_hostname: str,
        port: int,
        address: _ResolvedAddress,
        connect_timeout: float,
        read_timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            server_hostname,
            port=port,
            timeout=read_timeout,
            context=context,
        )
        self._validated_address = address
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._server_hostname = server_hostname
        self._tls_context = context

    def connect(self) -> None:
        address = self._validated_address
        raw_socket = socket.socket(
            address.family,
            address.socket_type,
            address.protocol,
        )
        try:
            raw_socket.settimeout(self._connect_timeout)
            raw_socket.connect(address.sockaddr)
            tls_socket = self._tls_context.wrap_socket(
                raw_socket,
                server_hostname=self._server_hostname,
            )
            tls_socket.settimeout(self._read_timeout)
        except BaseException:
            raw_socket.close()
            raise
        self.sock = tls_socket


@dataclass(slots=True)
class APIResponse:
    data: Any
    status_code: int
    headers: Mapping[str, str]


class API429Client:
    """Small synchronous API429 client shared by the CLI and future MCP layer."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float = 600.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        normalized = str(base_url or "").strip().rstrip("/")
        _validate_base_url(normalized)
        self.base_url = normalized
        self.api_key = str(api_key or "").strip() or None
        self.timeout_seconds = float(timeout)
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigurationError("API429 timeout must be greater than zero")
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(
                self.timeout_seconds, connect=min(15.0, self.timeout_seconds)
            ),
            follow_redirects=False,
            headers={
                "User-Agent": f"api429-cli/{__version__}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> API429Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _auth_headers(self, *, required: bool = True) -> dict[str, str]:
        if not self.api_key:
            if required:
                raise ConfigurationError(
                    "No API key configured. Run `api429 auth login` or set API429_API_KEY."
                )
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _decode_body(response: httpx.Response) -> Any:
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                pass
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return response.text

    @staticmethod
    def _error_from_response(response: httpx.Response, payload: Any) -> APIError:
        message = response.reason_phrase or "Request failed"
        code = None
        trace_id = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("detail") or message)
                code = str(error.get("code") or "") or None
                trace_id = str(error.get("trace_id") or "") or None
            elif isinstance(error, str):
                message = error
            elif payload.get("detail") is not None:
                detail = payload.get("detail")
                if isinstance(detail, list):
                    message = "; ".join(
                        str(item.get("msg") if isinstance(item, dict) else item)
                        for item in detail
                    )
                else:
                    message = str(detail)
            trace_id = trace_id or str(payload.get("trace_id") or "") or None
        elif isinstance(payload, str) and payload.strip():
            message = payload.strip()
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "x-correlation-id"
        )
        return APIError(
            status_code=response.status_code,
            message=message,
            code=code,
            trace_id=trace_id,
            request_id=request_id,
            retry_after=response.headers.get("retry-after"),
            payload=payload,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        expected: Iterable[int] = (200,),
        paid_submission: bool = False,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> APIResponse:
        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers.update(self._auth_headers(required=True))
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http.request(
                method.upper(),
                self._url(path),
                headers=headers,
                **kwargs,
            )
        except httpx.RequestError as exc:
            if paid_submission:
                suffix = (
                    f" Idempotency key: {idempotency_key}."
                    if idempotency_key
                    else " This endpoint has no idempotency guarantee."
                )
                raise AmbiguousRequestError(
                    "The connection failed during a paid request and its outcome is unknown. "
                    "Do not submit it again automatically; check jobs/usage or contact support."
                    + suffix,
                    idempotency_key=idempotency_key,
                ) from exc
            raise TransportError(f"Could not reach API429: {exc}") from exc
        payload = self._decode_body(response)
        if response.status_code not in set(expected):
            if paid_submission and (
                response.status_code == 408
                or response.status_code >= 500
                or (idempotency_key is not None and response.status_code == 409)
            ):
                suffix = (
                    f" Idempotency key: {idempotency_key}."
                    if idempotency_key
                    else " This endpoint has no idempotency guarantee."
                )
                raise AmbiguousRequestError(
                    f"The paid request returned HTTP {response.status_code}, but the server may "
                    "already have accepted upstream work. Do not resubmit it automatically."
                    + suffix,
                    idempotency_key=idempotency_key,
                )
            raise self._error_from_response(response, payload)
        return APIResponse(payload, response.status_code, response.headers)

    def login(self, *, email: str, password: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            "/api/client/login",
            auth=False,
            json={"email": email, "password": password},
        )
        if not isinstance(response.data, dict):
            raise ConfigurationError("API429 returned an invalid login response")
        result = response.data
        if not result.get("api_key") and result.get("portal_session_token"):
            issued = self.request(
                "POST",
                "/api/client/account-api-key",
                auth=False,
                headers={"Authorization": f"Bearer {result['portal_session_token']}"},
            ).data
            if isinstance(issued, dict):
                result = issued
        if not result.get("api_key"):
            raise ConfigurationError("The account did not return an API key")
        return result

    def validate_key(self, api_key: str) -> dict[str, Any]:
        previous = self.api_key
        self.api_key = str(api_key).strip()
        try:
            data = self.balance().data
        finally:
            self.api_key = previous
        if not isinstance(data, dict):
            raise ConfigurationError("API429 returned an invalid balance response")
        return data

    def models(self) -> APIResponse:
        return self.request("GET", "/v1/models")

    def model_help(self, model_id: str, *, markdown: bool = False) -> APIResponse:
        encoded = quote(model_id, safe="")
        suffix = "?format=markdown" if markdown else ""
        return self.request("GET", f"/v1/models/{encoded}/help{suffix}")

    def balance(self) -> APIResponse:
        return self.request("GET", "/api/client/balance")

    def usage(self, *, daily: bool = False) -> APIResponse:
        endpoint = "/api/client/usage/daily" if daily else "/api/client/usage/summary"
        return self.request("GET", endpoint)

    def generate_image(self, payload: Mapping[str, Any]) -> APIResponse:
        return self.request(
            "POST",
            "/v1/images/generations",
            expected=(200, 202),
            paid_submission=True,
            json=dict(payload),
        )

    def edit_image(
        self,
        *,
        model: str,
        prompt: str,
        image_paths: Iterable[Path],
        mask_path: Path | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> APIResponse:
        with ExitStack() as stack:
            files: list[tuple[str, tuple[str, Any, str]]] = []
            for path in image_paths:
                handle = stack.enter_context(path.open("rb"))
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files.append(("image", (path.name, handle, mime)))
            if mask_path is not None:
                handle = stack.enter_context(mask_path.open("rb"))
                mime = (
                    mimetypes.guess_type(mask_path.name)[0]
                    or "application/octet-stream"
                )
                files.append(("mask", (mask_path.name, handle, mime)))
            data = {"model": model, "prompt": prompt}
            for key, value in (fields or {}).items():
                if value is not None:
                    data[key] = str(value)
            return self.request(
                "POST",
                "/v1/images/edits",
                expected=(200,),
                paid_submission=True,
                data=data,
                files=files,
            )

    def generate_video(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> APIResponse:
        return self.request(
            "POST",
            "/v1/videos/generations",
            expected=(202,),
            paid_submission=True,
            idempotency_key=idempotency_key,
            json=dict(payload),
        )

    def video_status(self, job_id: str) -> APIResponse:
        return self.request("GET", f"/v1/videos/generations/{quote(job_id, safe='')}")

    def job(self, job_id: str) -> APIResponse:
        return self.request("GET", f"/v1/balancer/jobs/{quote(job_id, safe='')}")

    def job_result(self, job_id: str, *, timeout: float | None = None) -> APIResponse:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self.request(
            "GET",
            f"/v1/balancer/jobs/{quote(job_id, safe='')}/result",
            expected=(200, 202),
            **kwargs,
        )

    def cancel_job(self, job_id: str) -> APIResponse:
        return self.request("DELETE", f"/v1/balancer/jobs/{quote(job_id, safe='')}")

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 600.0,
        interval: float = 3.0,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> APIResponse:
        if (
            not math.isfinite(timeout)
            or not math.isfinite(interval)
            or timeout <= 0
            or interval <= 0
        ):
            raise ConfigurationError(
                "Wait timeout and interval must be greater than zero"
            )
        deadline = monotonic() + timeout
        while True:
            before_request = monotonic()
            remaining = deadline - before_request
            if remaining <= 0:
                raise ConfigurationError(
                    f"Timed out waiting for job {job_id}. The job was not cancelled."
                )
            try:
                response = self.job_result(
                    job_id,
                    timeout=max(0.001, min(remaining, self.timeout_seconds)),
                )
            except TransportError:
                now = monotonic()
                if now >= deadline:
                    raise ConfigurationError(
                        f"Timed out waiting for job {job_id}. The job was not cancelled."
                    )
                sleeper(min(interval, max(0.0, deadline - now)))
                continue
            except APIError as exc:
                if exc.status_code == 409:
                    status = self.job(job_id).data
                    state = (
                        status.get("status") if isinstance(status, dict) else "unknown"
                    )
                    error = status.get("error") if isinstance(status, dict) else None
                    detail = error.get("message") if isinstance(error, dict) else None
                    raise APIError(
                        status_code=409,
                        message=detail or f"Job ended with status {state}",
                        code=(error.get("code") if isinstance(error, dict) else None),
                        payload=status,
                    ) from exc
                if exc.status_code in {429, 502, 503, 504}:
                    now = monotonic()
                    if now >= deadline:
                        raise ConfigurationError(
                            f"Timed out waiting for job {job_id}. The job was not cancelled."
                        ) from exc
                    try:
                        retry_delay = float(exc.retry_after or interval)
                    except (TypeError, ValueError):
                        retry_delay = interval
                    if not math.isfinite(retry_delay) or retry_delay <= 0:
                        retry_delay = interval
                    sleeper(min(max(interval, retry_delay), max(0.0, deadline - now)))
                    continue
                raise
            if response.status_code == 200:
                return response
            now = monotonic()
            if now >= deadline:
                raise ConfigurationError(
                    f"Timed out waiting for job {job_id}. The job was not cancelled."
                )
            retry_header = response.headers.get("retry-after")
            body_retry = (
                response.data.get("retry_after_seconds")
                if isinstance(response.data, dict)
                else None
            )
            try:
                delay = max(interval, float(retry_header or body_retry or interval))
            except (TypeError, ValueError):
                delay = interval
            sleeper(min(delay, max(0.0, deadline - now)))

    def download(self, url: str, destination: Path, *, overwrite: bool = False) -> Path:
        target = str(url).strip()
        if not target:
            raise ConfigurationError("Empty download URL")
        parsed = urlsplit(target)
        same_origin = False
        if not parsed.scheme:
            if parsed.netloc:
                raise ConfigurationError(
                    "Generated media URL must not be scheme-relative"
                )
            target = self._url(target)
            parsed = urlsplit(target)
            same_origin = True
        else:
            base = urlsplit(self.base_url)
            same_origin = _origin(parsed) == _origin(base)
            if parsed.scheme not in {"http", "https"}:
                raise ConfigurationError("Generated media URL must use HTTP or HTTPS")
            if not same_origin and parsed.scheme != "https":
                raise ConfigurationError("External generated media URLs must use HTTPS")
            if parsed.username or parsed.password:
                raise ConfigurationError(
                    "Generated media URL must not contain credentials"
                )
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ConfigurationError("Generated media URL has an invalid port") from exc
        headers = self._auth_headers(required=True) if same_origin else {}
        if destination.is_symlink():
            raise ConfigurationError(f"Refusing to overwrite symlink: {destination}")
        if destination.exists() and not overwrite:
            raise ConfigurationError(
                f"Output file already exists: {destination}. Pass --force to replace it."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary)
        try:
            if os.name == "posix":
                os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                if same_origin:
                    with self._http.stream(
                        "GET",
                        target,
                        headers=headers,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code != 200:
                            payload = _read_bounded_error_body(response)
                            raise self._error_from_response(response, payload)
                        _enforce_download_content_length(
                            response.headers.get("content-length")
                        )
                        total = 0
                        for chunk in response.iter_bytes(
                            chunk_size=_DOWNLOAD_CHUNK_BYTES
                        ):
                            total = _write_download_chunk(handle, chunk, total=total)
                else:
                    hostname = _canonical_download_hostname(parsed.hostname)
                    port = parsed_port or 443
                    connection, external_response = _open_pinned_https(
                        hostname=hostname,
                        port=port,
                        parsed=parsed,
                        connect_timeout=min(15.0, self.timeout_seconds),
                        read_timeout=self.timeout_seconds,
                        user_agent=str(
                            self._http.headers.get("user-agent", "api429-cli")
                        ),
                    )
                    try:
                        if external_response.status != 200:
                            synthetic = _external_error_response(
                                external_response,
                                target=target,
                            )
                            payload = self._decode_body(synthetic)
                            raise self._error_from_response(synthetic, payload)
                        _enforce_download_content_length(
                            external_response.getheader("content-length")
                        )
                        total = 0
                        while True:
                            chunk = external_response.read(_DOWNLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            total = _write_download_chunk(handle, chunk, total=total)
                    except (OSError, http.client.HTTPException) as exc:
                        raise TransportError(
                            "The generated media connection failed while downloading"
                        ) from exc
                    finally:
                        connection.close()
                handle.flush()
                os.fsync(handle.fileno())
            _commit_download(temporary_path, destination, overwrite=overwrite)
            if os.name == "posix":
                destination.chmod(0o600)
        except httpx.RequestError as exc:
            raise TransportError(f"Could not download generated media: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        return destination


def _origin(parsed: Any) -> tuple[str, str, int | None]:
    try:
        port = parsed.port
    except ValueError:
        return parsed.scheme.lower(), "", None
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return (
        parsed.scheme.lower(),
        str(parsed.hostname or "").rstrip(".").lower(),
        port or default_port,
    )


def _canonical_download_hostname(hostname: str | None) -> str:
    host = str(hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ConfigurationError("Generated media URL has no hostname")
    if host == "localhost" or host.endswith(".localhost"):
        raise ConfigurationError("Refusing to download generated media from localhost")
    if "%" in host:
        raise ConfigurationError("Generated media URL has an invalid hostname")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ConfigurationError("Generated media URL has an invalid hostname") from exc


def _resolve_public_download_addresses(
    hostname: str,
    port: int,
) -> list[_ResolvedAddress]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ConfigurationError(
                "Refusing to download generated media from a private address"
            )
        if literal.version == 4:
            return [
                _ResolvedAddress(
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    (str(literal), port),
                )
            ]
        return [
            _ResolvedAddress(
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                (str(literal), port, 0, 0),
            )
        ]
    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ConfigurationError(
            f"Could not resolve generated media host: {hostname}"
        ) from exc
    if not answers:
        raise ConfigurationError(f"Could not resolve generated media host: {hostname}")
    resolved_addresses: list[_ResolvedAddress] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for family, socket_type, protocol, _canonical_name, sockaddr in answers:
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise ConfigurationError(
                "Refusing an invalid address returned for generated media"
            ) from exc
        if not resolved.is_global:
            raise ConfigurationError(
                "Refusing to download generated media from a host that resolves to a private address"
            )
        normalized_sockaddr = tuple(sockaddr)
        identity = (family, normalized_sockaddr)
        if identity in seen:
            continue
        seen.add(identity)
        resolved_addresses.append(
            _ResolvedAddress(
                family,
                socket_type or socket.SOCK_STREAM,
                protocol or socket.IPPROTO_TCP,
                normalized_sockaddr,
            )
        )
    return resolved_addresses


def _open_pinned_https(
    *,
    hostname: str,
    port: int,
    parsed: Any,
    connect_timeout: float,
    read_timeout: float,
    user_agent: str,
) -> tuple[_PinnedHTTPSConnection, http.client.HTTPResponse]:
    addresses = _resolve_public_download_addresses(hostname, port)
    context = ssl.create_default_context()
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"
    host_header = _download_host_header(hostname, port)
    for address in addresses:
        connection = _PinnedHTTPSConnection(
            server_hostname=hostname,
            port=port,
            address=address,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            context=context,
        )
        try:
            connection.request(
                "GET",
                request_target,
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "Host": host_header,
                    "User-Agent": user_agent,
                },
            )
            return connection, connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
    raise TransportError(
        f"Could not establish a verified HTTPS connection to {hostname}"
    )


def _download_host_header(hostname: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        rendered = hostname
    else:
        rendered = f"[{hostname}]" if address.version == 6 else hostname
    return rendered if port == 443 else f"{rendered}:{port}"


def _enforce_download_content_length(raw_length: str | None) -> None:
    if not raw_length:
        return
    try:
        length = int(raw_length)
    except ValueError:
        return
    if length > MAX_DOWNLOAD_BYTES:
        raise ConfigurationError("Generated media exceeds the 2 GiB download limit")


def _write_download_chunk(handle: Any, chunk: bytes, *, total: int) -> int:
    next_total = total + len(chunk)
    if next_total > MAX_DOWNLOAD_BYTES:
        raise ConfigurationError("Generated media exceeds the 2 GiB download limit")
    handle.write(chunk)
    return next_total


def _external_error_response(
    response: http.client.HTTPResponse,
    *,
    target: str,
) -> httpx.Response:
    raw = response.read(1024 * 1024)
    return httpx.Response(
        response.status,
        headers=dict(response.getheaders()),
        content=raw,
        request=httpx.Request("GET", target),
    )


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("API429 base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ConfigurationError("API429 base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("API429 base URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ConfigurationError(
            "API429 base URL must be the gateway root without a path"
        )
    if parsed.scheme == "http":
        host = parsed.hostname.rstrip(".").lower()
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ConfigurationError(
                "API429 base URL must use HTTPS; HTTP is allowed only for loopback development"
            )


def _read_bounded_error_body(
    response: httpx.Response, *, limit: int = 1024 * 1024
) -> Any:
    collected = bytearray()
    for chunk in response.iter_bytes():
        remaining = limit - len(collected)
        if remaining <= 0:
            break
        collected.extend(chunk[:remaining])
    raw = bytes(collected)
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _commit_download(
    temporary_path: Path, destination: Path, *, overwrite: bool
) -> None:
    if overwrite:
        os.replace(temporary_path, destination)
        return
    try:
        os.link(temporary_path, destination)
    except FileExistsError as exc:
        raise ConfigurationError(
            f"Output file already exists: {destination}. Pass --force to replace it."
        ) from exc
    temporary_path.unlink()
