from __future__ import annotations

import argparse
import base64
import getpass
import json
import mimetypes
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .client import API429Client, APIResponse
from .config import clear_credentials, load_settings, save_credentials
from .errors import (
    AmbiguousRequestError,
    APIError,
    CLIError,
    ConfigurationError,
    UsageError,
)
from .operations import begin_operation, list_operations, update_operation
from .output import (
    json_dump,
    print_balance,
    print_models,
    print_usage,
    sanitize_for_display,
    save_media,
)

ClientFactory = Callable[..., API429Client]
MAX_LOCAL_MEDIA_INPUT_BYTES = 100 * 1024 * 1024

# Some legacy aliases do not currently provide replay-safe billing for every
# ambiguous network outcome. Keep them out of the public CLI until the service
# advertises a safe submission contract for them.
BLOCKED_VIDEO_REPLAY_MODELS = frozenset(
    {
        "veo-3-fast",
        "veo-3-quality",
        "pika-2.2",
        "kling-1.6",
        "kling-2.0",
        "kling-2.5",
        "kling-3.0",
        "deepface-video",
        "upscale-video-hd",
        "upscale-video-fhd",
        "upscale-video-4k",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api429",
        description="Generate media and inspect your API429 account from the terminal.",
    )
    parser.add_argument(
        "--base-url", help="Gateway root (default: https://gateway.api429.com)"
    )
    parser.add_argument("--timeout", type=float, help="HTTP timeout in seconds")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    parser.add_argument("--version", action="version", version=f"api429 {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth", help="Manage API429 credentials")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser(
        "login", help="Sign in with email/password or an API key"
    )
    login.add_argument("--email", help="Client Portal email (prompted when omitted)")
    login.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read an existing API key from stdin instead of prompting for account credentials",
    )
    login.add_argument(
        "--no-save", action="store_true", help="Validate but do not persist the API key"
    )
    auth_commands.add_parser(
        "status", help="Validate the configured key and show balance"
    )
    auth_commands.add_parser(
        "logout", help="Delete the locally stored key (does not revoke it server-side)"
    )

    models = commands.add_parser(
        "models", help="Inspect models available to this token"
    )
    model_commands = models.add_subparsers(dest="models_command", required=True)
    model_commands.add_parser("list", help="List executable models")
    model_help = model_commands.add_parser(
        "help", help="Show integration help for one model"
    )
    model_help.add_argument("model")
    model_help.add_argument(
        "--markdown", action="store_true", help="Request Markdown instead of JSON"
    )

    commands.add_parser("balance", help="Show current balance")
    usage = commands.add_parser("usage", help="Show usage and costs")
    usage.add_argument(
        "--daily", action="store_true", help="Only show the rolling 24-hour report"
    )

    image = commands.add_parser("image", help="Generate or edit images")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    image_generate = image_commands.add_parser("generate", help="Generate an image")
    _add_payload_file_options(image_generate)
    image_generate.add_argument("--model", help="Explicit model ID")
    image_generate.add_argument("--prompt", help="Generation prompt")
    image_generate.add_argument("--n", type=int)
    image_generate.add_argument("--size")
    image_generate.add_argument("--resolution")
    image_generate.add_argument("--aspect-ratio")
    image_generate.add_argument("--quality")
    image_generate.add_argument("--response-format", choices=("url", "b64_json"))
    image_generate.add_argument(
        "--image", action="append", help="Reference URL or local image; repeatable"
    )
    image_generate.add_argument("--negative-prompt")
    image_generate.add_argument(
        "--output-mime-type", choices=("image/png", "image/jpeg", "image/webp")
    )
    image_generate.add_argument("--person-generation")
    image_generate.add_argument(
        "--wait", action="store_true", help="Wait if the request becomes an async job"
    )
    _add_wait_options(image_generate)
    _add_paid_options(image_generate)

    image_edit = image_commands.add_parser("edit", help="Edit one or more local images")
    image_edit.add_argument(
        "--model", required=True, help="Explicit edit-capable model ID"
    )
    image_edit.add_argument("--prompt", required=True)
    image_edit.add_argument("--image", action="append", required=True, type=Path)
    image_edit.add_argument("--mask", type=Path)
    image_edit.add_argument("--size")
    image_edit.add_argument("--quality")
    image_edit.add_argument("--background")
    image_edit.add_argument("--output-format", choices=("png", "jpeg", "webp"))
    image_edit.add_argument("--output-compression", type=int)
    image_edit.add_argument("--n", type=int, default=1)
    image_edit.add_argument(
        "--response-format", choices=("url", "b64_json"), default="b64_json"
    )
    _add_paid_options(image_edit)

    video = commands.add_parser("video", help="Generate video asynchronously")
    video_commands = video.add_subparsers(dest="video_command", required=True)
    video_generate = video_commands.add_parser(
        "generate", help="Submit a video generation job"
    )
    _add_payload_file_options(video_generate)
    video_generate.add_argument("--model", help="Explicit model ID")
    video_generate.add_argument("--prompt", help="Generation prompt")
    video_generate.add_argument("--mode")
    video_generate.add_argument("--duration", type=int)
    video_generate.add_argument("--n", type=int)
    video_generate.add_argument("--aspect-ratio")
    video_generate.add_argument("--resolution")
    audio = video_generate.add_mutually_exclusive_group()
    audio.add_argument("--audio", dest="audio", action="store_true")
    audio.add_argument("--no-audio", dest="audio", action="store_false")
    video_generate.set_defaults(audio=None)
    video_generate.add_argument("--image", help="First-frame URL or local image")
    video_generate.add_argument("--last-frame", help="Last-frame URL or local image")
    video_generate.add_argument("--video", help="Input video URL or local video")
    video_generate.add_argument("--negative-prompt")
    video_generate.add_argument("--seed", type=int)
    video_generate.add_argument("--thinking-level")
    video_generate.add_argument("--previous-job-id")
    video_generate.add_argument(
        "--idempotency-key", help="Stable key for this exact request"
    )
    video_generate.add_argument(
        "--wait", action="store_true", help="Wait for the completed video"
    )
    _add_wait_options(video_generate)
    _add_paid_options(video_generate)

    jobs = commands.add_parser("jobs", help="Inspect and control async jobs")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    job_list = job_commands.add_parser(
        "list", help="List paid operations submitted by this CLI"
    )
    job_list.add_argument("--limit", type=int, default=50)
    job_get = job_commands.add_parser("get", help="Get job metadata")
    job_get.add_argument("job_id")
    job_wait = job_commands.add_parser("wait", help="Wait for a job result")
    job_wait.add_argument("job_id")
    _add_wait_options(job_wait)
    job_wait.add_argument("--output", type=Path)
    job_wait.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )
    job_cancel = job_commands.add_parser("cancel", help="Request cancellation")
    job_cancel.add_argument("job_id")
    return parser


def _add_payload_file_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input-json",
        metavar="FILE",
        help="Start from a JSON object in FILE, or use - for stdin",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set an additional top-level field; VALUE accepts JSON syntax",
    )


def _add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait-timeout", type=float, default=600.0)
    parser.add_argument("--wait-interval", type=float, default=3.0)


def _add_paid_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Submit without prompting and accept the server-determined charge",
    )
    parser.add_argument("--output", type=Path, help="Save returned media to this path")
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory = API429Client,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(base_url=args.base_url, timeout=args.timeout)
        with client_factory(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
        ) as client:
            return _dispatch(args, client)
    except KeyboardInterrupt:
        print("Interrupted. Remote jobs were not cancelled.", file=sys.stderr)
        return 130
    except AmbiguousRequestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (APIError, CLIError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)


def _dispatch(args: argparse.Namespace, client: API429Client) -> int:
    if args.command == "auth":
        return _auth_command(args, client)
    if args.command == "models":
        if args.models_command == "list":
            result = client.models().data
            json_dump(result) if args.json else print_models(result)
            return 0
        result = client.model_help(args.model, markdown=args.markdown)
        if args.json:
            json_dump(
                {"markdown": result.data}
                if isinstance(result.data, str)
                else result.data
            )
        elif args.markdown and isinstance(result.data, str):
            print(result.data)
        else:
            json_dump(result.data)
        return 0
    if args.command == "balance":
        result = client.balance().data
        json_dump(result) if args.json else print_balance(result)
        return 0
    if args.command == "usage":
        result = client.usage(daily=args.daily).data
        json_dump(result) if args.json else print_usage(result)
        return 0
    if args.command == "image":
        return _image_command(args, client)
    if args.command == "video":
        return _video_command(args, client)
    if args.command == "jobs":
        return _jobs_command(args, client)
    raise UsageError("Unknown command")


def _auth_command(args: argparse.Namespace, client: API429Client) -> int:
    if args.auth_command == "logout":
        clear_credentials()
        message = {
            "ok": True,
            "message": "Local credential removed; the server-side API key was not revoked.",
        }
        json_dump(message) if args.json else print(message["message"])
        return 0
    if args.auth_command == "status":
        result = client.balance().data
        json_dump(result) if args.json else print_balance(result)
        return 0

    if args.token_stdin:
        if sys.stdin.isatty():
            token = getpass.getpass("API key: ").strip()
        else:
            token = sys.stdin.readline().strip()
        if not token:
            raise UsageError("No API key received on stdin")
        balance = client.validate_key(token)
        api_key = token
        account = None
    else:
        email = (args.email or _prompt_stderr("Email: ")).strip()
        password = getpass.getpass("Password: ")
        login = client.login(email=email, password=password)
        api_key = str(login["api_key"])
        account = login.get("account")
        balance = {
            "token_last4": login.get("token_last4"),
            "balance": {"balance_usd": login.get("balance_usd")},
        }
    if not args.no_save:
        save_credentials(api_key)
    output = {
        "authenticated": True,
        "saved": not args.no_save,
        "account": account,
        "token_last4": balance.get("token_last4")
        if isinstance(balance, dict)
        else api_key[-4:],
        "balance": balance.get("balance") if isinstance(balance, dict) else None,
    }
    if args.json:
        json_dump(output)
    else:
        suffix = output.get("token_last4") or api_key[-4:]
        print(f"Authenticated as token …{suffix}.")
        if args.no_save:
            print("Credential was not saved.")
        elif sys.platform == "win32":
            print("Credential saved as plaintext using the current directory ACL.")
        else:
            print("Credential saved as plaintext in a mode-0600 file.")
    return 0


def _image_command(args: argparse.Namespace, client: API429Client) -> int:
    if args.image_command == "edit":
        _validate_local_files(args.image, mask=args.mask)
        preflight = _paid_preflight(client, args.model)
        _confirm_paid(args, preflight, model=args.model, endpoint="images.edits")
        fields = {
            "size": args.size,
            "quality": args.quality,
            "background": args.background,
            "output_format": args.output_format,
            "output_compression": args.output_compression,
            "n": args.n,
            "response_format": args.response_format,
        }
        operation = begin_operation(
            endpoint="images.edits",
            model=args.model,
            request_payload={
                "model": args.model,
                "prompt": args.prompt,
                "images": [str(path) for path in args.image],
                "mask": str(args.mask) if args.mask else None,
                **fields,
            },
            base_url=client.base_url,
        )
        try:
            response = client.edit_image(
                model=args.model,
                prompt=args.prompt,
                image_paths=args.image,
                mask_path=args.mask,
                fields=fields,
            )
        except AmbiguousRequestError as exc:
            update_operation(operation["id"], status="unknown", error=str(exc))
            raise AmbiguousRequestError(
                f"{exc} Local operation: {operation['id']}.",
                idempotency_key=exc.idempotency_key,
            ) from exc
        except APIError as exc:
            update_operation(operation["id"], status="failed", error=str(exc))
            raise
        update_operation(operation["id"], status="completed")
        return _present_generation(
            args, client, response, extra={"operation_id": operation["id"]}
        )

    payload = _load_payload(args.input_json)
    _overlay(payload, "model", args.model)
    _overlay(payload, "prompt", args.prompt)
    _overlay(payload, "n", args.n)
    _overlay(payload, "size", args.size)
    _overlay(payload, "resolution", args.resolution)
    _overlay(payload, "aspect_ratio", args.aspect_ratio)
    _overlay(payload, "quality", args.quality)
    _overlay(payload, "response_format", args.response_format)
    _overlay(payload, "negative_prompt", args.negative_prompt)
    _overlay(payload, "output_mime_type", args.output_mime_type)
    _overlay(payload, "person_generation", args.person_generation)
    if args.image:
        references = [_media_reference(value) for value in args.image]
        payload["image"] = references[0] if len(references) == 1 else references
    _apply_params(payload, args.param)
    model, _ = _require_model_prompt(payload)
    if args.output and "response_format" not in payload:
        payload["response_format"] = "b64_json"
    preflight = _paid_preflight(client, model)
    _confirm_paid(args, preflight, model=model, endpoint="images.generations")
    operation = begin_operation(
        endpoint="images.generations",
        model=model,
        request_payload=payload,
        base_url=client.base_url,
    )
    try:
        response = client.generate_image(payload)
    except AmbiguousRequestError as exc:
        update_operation(operation["id"], status="unknown", error=str(exc))
        raise AmbiguousRequestError(
            f"{exc} Local operation: {operation['id']}.",
            idempotency_key=exc.idempotency_key,
        ) from exc
    except APIError as exc:
        update_operation(operation["id"], status="failed", error=str(exc))
        raise
    job_id: str | None = None
    if response.status_code == 202 and (args.wait or args.output):
        job_id = _job_id(response.data)
        update_operation(operation["id"], status="accepted", job_id=job_id)
        try:
            response = client.wait_for_job(
                job_id,
                timeout=args.wait_timeout,
                interval=args.wait_interval,
            )
        except (APIError, ConfigurationError) as exc:
            _record_wait_failure(operation["id"], job_id, exc)
            raise
        update_operation(operation["id"], status="completed", job_id=job_id)
    elif response.status_code == 202:
        job_id = _job_id(response.data)
        update_operation(operation["id"], status="accepted", job_id=job_id)
    else:
        update_operation(operation["id"], status="completed")
    return _present_generation(
        args,
        client,
        response,
        extra={
            "operation_id": operation["id"],
            **({"job_id": job_id} if job_id else {}),
        },
    )


def _video_command(args: argparse.Namespace, client: API429Client) -> int:
    payload = _load_payload(args.input_json)
    _overlay(payload, "model", args.model)
    _overlay(payload, "prompt", args.prompt)
    _overlay(payload, "mode", args.mode)
    _overlay(payload, "duration", args.duration)
    _overlay(payload, "n", args.n)
    _overlay(payload, "aspect_ratio", args.aspect_ratio)
    _overlay(payload, "resolution", args.resolution)
    _overlay(payload, "audio", args.audio)
    _overlay(payload, "negative_prompt", args.negative_prompt)
    _overlay(payload, "seed", args.seed)
    _overlay(payload, "thinking_level", args.thinking_level)
    _overlay(payload, "previous_job_id", args.previous_job_id)
    for field, value in (
        ("image", args.image),
        ("last_frame", args.last_frame),
        ("video", args.video),
    ):
        if value:
            payload[field] = _media_reference(value)
    _apply_params(payload, args.param)
    model, _ = _require_model_prompt(payload)
    if model.lower() in BLOCKED_VIDEO_REPLAY_MODELS:
        raise UsageError(
            f"Model {model!r} is temporarily unavailable in the CLI because "
            "replay-safe billing is not available for ambiguous submissions. "
            "Choose another model from `api429 models list`."
        )
    idempotency_key = args.idempotency_key or f"cli-{uuid.uuid4().hex}"
    preflight = _paid_preflight(client, model)
    _confirm_paid(
        args,
        preflight,
        model=model,
        endpoint="videos.generations",
        idempotency_key=idempotency_key,
    )
    operation = begin_operation(
        endpoint="videos.generations",
        model=model,
        request_payload=payload,
        base_url=client.base_url,
        idempotency_key=idempotency_key,
    )
    try:
        response = client.generate_video(payload, idempotency_key=idempotency_key)
    except AmbiguousRequestError as exc:
        update_operation(operation["id"], status="unknown", error=str(exc))
        raise AmbiguousRequestError(
            f"{exc} Local operation: {operation['id']}.",
            idempotency_key=exc.idempotency_key,
        ) from exc
    except APIError as exc:
        update_operation(operation["id"], status="failed", error=str(exc))
        raise
    job_id = _job_id(response.data)
    update_operation(operation["id"], status="accepted", job_id=job_id)
    if args.wait or args.output:
        try:
            response = client.wait_for_job(
                job_id,
                timeout=args.wait_timeout,
                interval=args.wait_interval,
            )
        except (APIError, ConfigurationError) as exc:
            _record_wait_failure(operation["id"], job_id, exc)
            raise
        update_operation(operation["id"], status="completed", job_id=job_id)
    return _present_generation(
        args,
        client,
        response,
        extra={
            "operation_id": operation["id"],
            "job_id": job_id,
            "idempotency_key": idempotency_key,
        },
    )


def _jobs_command(args: argparse.Namespace, client: API429Client) -> int:
    if args.jobs_command == "list":
        records = list_operations(limit=args.limit)
        if args.json:
            json_dump({"object": "local.operation.list", "data": records})
        elif not records:
            print("No locally recorded operations.")
        else:
            print(f"{'OPERATION':<36} {'STATUS':<11} {'MODEL':<36} JOB")
            for item in records:
                print(
                    f"{item.get('id') or ''!s:<36} "
                    f"{item.get('status') or ''!s:<11} "
                    f"{item.get('model') or ''!s:<36} "
                    f"{item.get('job_id') or '-'!s}"
                )
        return 0
    if args.jobs_command == "get":
        response = client.job(args.job_id)
    elif args.jobs_command == "cancel":
        response = client.cancel_job(args.job_id)
        remote_status = (
            str(response.data.get("status") or "cancel_requested")
            if isinstance(response.data, dict)
            else "cancel_requested"
        )
        _update_operations_for_job(args.job_id, status=remote_status)
    else:
        try:
            response = client.wait_for_job(
                args.job_id,
                timeout=args.wait_timeout,
                interval=args.wait_interval,
            )
        except (APIError, ConfigurationError) as exc:
            status = "wait_stopped"
            if isinstance(exc, APIError):
                payload_status = (
                    exc.payload.get("status") if isinstance(exc.payload, dict) else None
                )
                status = str(payload_status or "failed")
            _update_operations_for_job(args.job_id, status=status, error=str(exc))
            raise
        _update_operations_for_job(args.job_id, status="completed")
    return _present_generation(args, client, response)


def _present_generation(
    args: argparse.Namespace,
    client: API429Client,
    response: APIResponse,
    *,
    extra: Mapping[str, Any] | None = None,
) -> int:
    files = (
        save_media(
            client,
            response.data,
            args.output,
            overwrite=bool(getattr(args, "force", False)),
        )
        if getattr(args, "output", None)
        else []
    )
    if args.json:
        response_value = response.data
        if files and isinstance(response_value, dict):
            response_value = {
                key: value
                for key, value in response_value.items()
                if key not in {"data", "videos", "images", "outputs", "delete_url"}
            }
        result: dict[str, Any] = {"response": sanitize_for_display(response_value)}
        if files:
            result["files"] = files
        if extra:
            result.update(extra)
        json_dump(result)
        return 0
    if files:
        for item in files:
            print(
                f"Saved {item['path']} ({item['bytes']} bytes, sha256 {item['sha256']})"
            )
        if extra and extra.get("operation_id"):
            print(f"Operation: {extra['operation_id']}")
        return 0
    if (
        isinstance(response.data, dict)
        and response.data.get("id")
        and response.data.get("status")
    ):
        print(f"Job {response.data['id']}: {response.data['status']}")
        if extra and extra.get("operation_id"):
            print(f"Operation: {extra['operation_id']}")
        if extra and extra.get("idempotency_key"):
            print(f"Idempotency key: {extra['idempotency_key']}")
    else:
        json_dump(response.data)
    return 0


def _paid_preflight(client: API429Client, model: str) -> dict[str, Any]:
    balance = client.balance().data
    models = client.models().data
    available = {
        str(item.get("id"))
        for item in (models.get("data") if isinstance(models, dict) else []) or []
        if isinstance(item, dict) and item.get("id")
    }
    if model not in available:
        raise UsageError(
            f"Model {model!r} is not currently executable for this token. "
            "Run `api429 models list` to choose an available model."
        )
    return {"balance": balance, "model": model}


def _confirm_paid(
    args: argparse.Namespace,
    preflight: Mapping[str, Any],
    *,
    model: str,
    endpoint: str,
    idempotency_key: str | None = None,
) -> None:
    raw_balance = preflight.get("balance")
    balance = raw_balance.get("balance") if isinstance(raw_balance, dict) else {}
    available = (
        balance.get("available_balance_usd", balance.get("balance_usd"))
        if isinstance(balance, dict)
        else None
    )
    last4 = raw_balance.get("token_last4") if isinstance(raw_balance, dict) else None
    lines = [
        "Paid API429 request",
        f"  endpoint: {endpoint}",
        f"  model: {model}",
        f"  token: …{last4}" if last4 else "  token: configured",
        f"  available balance: ${float(available):.4f}"
        if available is not None
        else "  available balance: unknown",
        "  exact price: unavailable; final charge is determined by the server",
    ]
    if idempotency_key:
        lines.append(f"  idempotency key: {idempotency_key}")
    print("\n".join(lines), file=sys.stderr)
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise UsageError("Non-interactive paid requests require --yes")
    answer = (
        _prompt_stderr("Submit and accept the server-determined charge? [y/N] ")
        .strip()
        .lower()
    )
    if answer not in {"y", "yes"}:
        raise UsageError("Request cancelled before submission")


def _load_payload(filename: str | None) -> dict[str, Any]:
    if not filename:
        return {}
    try:
        if filename == "-":
            value = json.load(sys.stdin)
        else:
            with Path(filename).open("r", encoding="utf-8") as handle:
                value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UsageError(f"Could not read JSON payload from {filename}") from exc
    if not isinstance(value, dict):
        raise UsageError("Input JSON must contain an object")
    return value


def _apply_params(payload: dict[str, Any], params: Sequence[str]) -> None:
    for raw in params:
        key, separator, value = raw.partition("=")
        if not separator or not key.strip():
            raise UsageError(f"Invalid --param {raw!r}; expected KEY=VALUE")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
        payload[key.strip()] = decoded


def _overlay(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value


def _require_model_prompt(payload: Mapping[str, Any]) -> tuple[str, str]:
    model = str(payload.get("model") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    if not model:
        raise UsageError("An explicit model is required (`--model` or input JSON)")
    if not prompt:
        raise UsageError("A non-empty prompt is required (`--prompt` or input JSON)")
    return model, prompt


def _media_reference(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https", "gs"}:
        return value
    if parsed.scheme == "data":
        if len(value) > (MAX_LOCAL_MEDIA_INPUT_BYTES * 4 // 3) + 1024:
            raise UsageError("Inline media input exceeds the 100 MiB safety limit")
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise UsageError(f"Media input does not exist or is not a file: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise UsageError(f"Media input is empty: {path}")
    if size > MAX_LOCAL_MEDIA_INPUT_BYTES:
        raise UsageError(f"Media input must not exceed 100 MiB: {path}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _validate_local_files(paths: Sequence[Path], *, mask: Path | None = None) -> None:
    candidates = [*paths, *([mask] if mask else [])]
    if len(paths) > 16:
        raise UsageError("Image edit accepts at most 16 input images")
    for path in candidates:
        if not path.is_file():
            raise UsageError(f"Input file does not exist: {path}")
        if path.stat().st_size <= 0:
            raise UsageError(f"Input file is empty: {path}")
        if path.stat().st_size >= 50 * 1024 * 1024:
            raise UsageError(f"Input file must be smaller than 50 MiB: {path}")


def _job_id(payload: Any) -> str:
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ConfigurationError("API429 returned a job response without an id")
    return str(payload["id"])


def _record_wait_failure(operation_id: str, job_id: str, exc: Exception) -> None:
    status = "wait_stopped"
    if isinstance(exc, APIError):
        payload_status = (
            exc.payload.get("status") if isinstance(exc.payload, dict) else None
        )
        if payload_status in {"failed", "cancelled", "ambiguous"}:
            status = str(payload_status)
        else:
            status = "failed"
    update_operation(operation_id, status=status, job_id=job_id, error=str(exc))


def _update_operations_for_job(
    job_id: str, *, status: str, error: str | None = None
) -> None:
    for item in list_operations(limit=1000):
        if str(item.get("job_id") or "") == job_id:
            update_operation(str(item["id"]), status=status, job_id=job_id, error=error)


def _prompt_stderr(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline()


if __name__ == "__main__":
    raise SystemExit(main())
