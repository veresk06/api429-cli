# API429 CLI

[![CI](https://github.com/veresk06/api429-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/veresk06/api429-cli/actions/workflows/ci.yml)

> Public preview. Read the paid-operation safety notes before using the CLI in
> unattended automation.

`api429` is the official command-line client for the API429 gateway. It can
inspect account state and available models, generate or edit images, submit
video jobs, wait for asynchronous results, and save returned media locally.
Its default gateway is `https://gateway.api429.com`.

## Installation

The recommended installation uses npm and does not require Python. Once the
first public release is available:

```bash
npm install --global @api429/cli
api429 --version
```

The npm package selects a native executable for macOS, GNU/Linux, or Windows
on x64 and ARM64. Alpine Linux and other musl-based distributions are not part
of the first release.

Once published, Python users can install the same CLI in an isolated
environment with Python 3.10 or newer:

```bash
pipx install api429-cli
```

Or install it into the active Python environment:

```bash
python -m pip install api429-cli
```

For an editable development installation from a repository checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
api429 --version
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1` instead.

For a regular Python installation from a checkout:

```bash
python -m pip install .
```

## Authentication

Sign in with a Client Portal email. The password is read from a hidden prompt:

```bash
api429 auth login --email user@example.com
```

If `--email` is omitted, the CLI prompts for it too. To validate and save an
existing API key without putting it in shell history or the process list, read
it from standard input:

```bash
api429 auth login --token-stdin
```

When standard input is a terminal, this uses a hidden prompt. For automation,
pipe the value rather than passing it as a command-line argument:

```bash
printf '%s\n' "$API429_API_KEY" | api429 auth login --token-stdin
```

Add `--no-save` to validate a key without persisting it. Check the configured
key and current balance with:

```bash
api429 auth status
```

`api429 auth logout` deletes only the locally stored credential. It does not
revoke or rotate the API key on the server.

Persisted credentials are stored separately from ordinary configuration. On
POSIX systems the CLI writes the credentials file with mode `0600` and its
dedicated directory with mode `0700`. The file is still a local plaintext
secret, not an operating-system keychain entry; prefer `API429_API_KEY` or
`--no-save` on shared machines.

## Configuration

The following environment variables are supported:

| Variable | Meaning |
| --- | --- |
| `API429_BASE_URL` | Gateway root; default is `https://gateway.api429.com` |
| `API429_API_KEY` | API key used for the current process |
| `API429_TIMEOUT` | Request timeout in seconds; default is `600` |
| `API429_CONFIG_FILE` | Explicit config path; `<stem>.credentials.json` is stored beside it |
| `XDG_CONFIG_HOME` | Base configuration directory on XDG systems |
| `APPDATA` | Base configuration directory on Windows |

The default POSIX files are `~/.config/api429/config.json` and
`~/.config/api429/credentials.json`. On Windows they are under
`%APPDATA%\api429`. A configuration file may contain non-secret settings:

```json
{
  "base_url": "https://gateway.api429.com",
  "timeout": 600
}
```

Precedence is: explicit command-line values, `API429_*` environment variables,
files, then built-in defaults. `--base-url` and `--timeout` are global options
and must appear before the command:

```bash
api429 --base-url https://gateway.api429.com --timeout 900 balance
```

Remote gateway URLs must use HTTPS. Plain HTTP is accepted only for loopback
development addresses such as `http://127.0.0.1:8000`; URL credentials,
queries, fragments, and path-prefixed base URLs are rejected.

`--timeout` controls an individual HTTP request. Job polling has separate
`--wait-timeout` and `--wait-interval` options.

## Models, balance, and usage

List models currently executable by the configured token:

```bash
api429 models list
api429 models help gemini-3.1-flash-image
api429 models help gemini-3.1-flash-image --markdown
```

Inspect the account balance and usage summaries:

```bash
api429 balance
api429 usage
api429 usage --daily
```

Model availability is token-specific and can change. Paid generation commands
therefore check the current balance and authenticated model list immediately
before submission.

## Image generation

An explicit model and a non-empty prompt are required. Saving to `--output`
requests base64 output by default and waits automatically if the server turns
the request into an asynchronous job:

```bash
api429 image generate \
  --model gemini-3.1-flash-image \
  --prompt "A paper-cut city at blue hour" \
  --aspect-ratio 16:9 \
  --output city.png
```

Reference images may be URLs or local files and the option is repeatable:

```bash
api429 image generate \
  --model gemini-3.1-flash-image \
  --prompt "Use the lighting and palette of these references" \
  --image reference-1.png \
  --image https://example.com/reference-2.webp \
  --output result.png
```

Other supported flags include `--n`, `--size`, `--resolution`,
`--aspect-ratio`, `--quality`, `--response-format`, `--negative-prompt`,
`--output-mime-type`, and `--person-generation`. Use `--wait` to wait for an
async response even when no output path is supplied.

Generation is a paid operation. In a terminal, the CLI shows the endpoint,
model, token suffix, available balance, and the fact that the exact price is
unknown, then asks for confirmation. Non-interactive use requires `--yes`:

```bash
api429 image generate \
  --model gemini-3.1-flash-image \
  --prompt "A minimal geometric poster" \
  --output poster.png \
  --yes
```

## Image editing

Image edit uploads local files as multipart form data. `--image` is repeatable;
`--mask` is optional:

```bash
api429 image edit \
  --model gpt-image-2 \
  --prompt "Replace the sky with a clear night sky" \
  --image source.png \
  --mask mask.png \
  --output edited.png
```

The CLI accepts at most 16 non-empty input images, each smaller than 50 MiB.
Additional options include `--size`, `--quality`, `--background`,
`--output-format`, `--output-compression`, `--n`, and `--response-format`.

## Video generation and jobs

Video generation always submits an asynchronous job:

```bash
api429 video generate \
  --model gemini-omni-1.1-flash \
  --prompt "Slow dolly through a rain-soaked neon street" \
  --duration 4 \
  --aspect-ratio 16:9
```

The CLI sends an idempotency key. Supply and retain your own stable key when
you need to identify the exact submission later:

```bash
api429 video generate \
  --model gemini-omni-1.1-flash \
  --prompt "Waves crossing black volcanic sand" \
  --idempotency-key video-2026-09-02-001 \
  --wait \
  --output waves.mp4
```

Without `--wait`, the command returns the job ID. Inspect, wait for, or request
cancellation of a job with:

```bash
api429 jobs get job_0123456789abcdef
api429 jobs wait job_0123456789abcdef --wait-timeout 1800
api429 jobs wait job_0123456789abcdef --output result.mp4
api429 jobs cancel job_0123456789abcdef
api429 jobs list
```

`--wait-interval` sets the minimum polling interval. The CLI also honors the
server's `Retry-After` guidance. A local wait timeout or `Ctrl-C` stops polling
only; it does not cancel the remote job.

`jobs list` is a local recovery journal, not a server-side account-wide list.
Before every paid POST, the CLI stores an operation ID, request hash, model,
and idempotency key when present—but not the prompt or media body. This keeps
an ambiguous network outcome diagnosable even when no job ID was returned.

Video payload flags include `--mode`, `--duration`, `--n`, `--aspect-ratio`,
`--resolution`, `--audio`/`--no-audio`, `--image`, `--last-frame`, `--video`,
`--negative-prompt`, `--seed`, `--thinking-level`, and `--previous-job-id`.
Local media supplied to the JSON video endpoint is encoded as a data URI.
The CLI limits local and inline media inputs to 100 MiB to avoid exhausting
memory. Some legacy video aliases are temporarily unavailable in the CLI when
the service cannot provide replay-safe billing for an ambiguous submission.
Other video routes still do not have a universal exactly-once guarantee.

## JSON input and parameter overrides

`image generate` and `video generate` accept a complete JSON object from a
file, or from standard input when the filename is `-`:

```bash
api429 image generate --input-json request.json
cat request.json | api429 image generate --input-json - --yes
```

Named flags override fields loaded from the file. Repeated `--param KEY=VALUE`
options are applied last, and therefore override both the file and named flags.
Values are decoded as JSON when possible; otherwise they remain strings:

```bash
api429 video generate \
  --input-json video.json \
  --param seed=42 \
  --param audio=true \
  --param 'reference_images=[{"image_url":"https://example.com/ref.png"}]'
```

After merging all inputs, `model` and `prompt` must still be present and
non-empty.

## Machine-readable output

`--json` is a global option and must precede the command:

```bash
api429 --json models list
api429 --json usage --daily
api429 --json jobs get job_0123456789abcdef
api429 --json video generate --input-json video.json --yes
```

JSON is written to stdout. Paid-request summaries, warnings, and errors go to
stderr. Secrets, large inline base64 values, and data URIs are redacted from
display output. When `--output` saves media, JSON output includes the resolved
path, MIME type, byte count, and SHA-256 digest instead of embedding media.
Existing files are never replaced unless `--force` is explicitly supplied.

Useful exit codes for automation are:

| Code | Meaning |
| ---: | --- |
| `0` | Success |
| `1` | API or runtime failure |
| `2` | Invalid usage or configuration |
| `3` | Paid request outcome is ambiguous; do not retry blindly |
| `4` | Authentication or permission failure |
| `5` | Insufficient balance |
| `6` | Rate limited |
| `130` | Interrupted locally with `Ctrl-C` |

## Safety limitations

- The CLI cannot quote the exact generation price. The displayed balance and
  model check are preflight information, not a spend cap. The server determines
  the final charge; `--yes` explicitly accepts that uncertainty.
- Paid submission requests are never retried automatically. If a connection
  fails, or a paid request returns an ambiguous timeout/server error, the
  result may still have been created and charged.
  Inspect jobs and usage before taking further action.
- Synchronous image generation has no exactly-once guarantee or usable
  idempotency key. Re-running an ambiguous image request can generate and bill
  a second result.
- A video idempotency key improves correlation and deduplication but is not a
  universal exactly-once guarantee across every server-side routing path. Do
  not replace a key automatically after an ambiguous result or conflict.
- Job cancellation is best effort. A running upstream operation may finish and
  may still be billed after a cancellation request.
- Local timeout and interruption stop only the CLI wait. They do not cancel the
  remote operation automatically.
- `auth logout` removes the local credentials file only. It does not revoke the
  server-side API key.

Run `api429 --help` and `api429 COMMAND --help` for the authoritative option
list installed with your version.

## Support and security

Use [GitHub Issues](https://github.com/veresk06/api429-cli/issues) for bugs and
feature requests. Do not include API keys, prompts, private media, or complete
gateway responses containing account data. See [SECURITY.md](SECURITY.md) for
reporting security vulnerabilities.
