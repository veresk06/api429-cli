# Contributing

Thank you for helping improve API429 CLI.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`.

Run the required checks before opening a pull request:

```bash
ruff check .
mypy --strict src scripts
pytest
python -m build
cd npm
npm ci --ignore-scripts
npm run check
```

The production package is checked with strict mypy settings. Tests use typed
fixtures where practical but are not part of the strict public API check.

Standalone executables are native builds, not cross-compiled artifacts. Build
one for the current machine with:

```bash
python -m pip install . -r scripts/requirements-standalone.txt
python scripts/build_standalone.py --output-dir dist/standalone
```

The npm platform packages intentionally contain no download-on-install script.
Release automation verifies all six native archives before staging their
payloads and refuses to publish a partial platform set.

Never commit API keys, account data, prompts, generated media, or production
responses. Tests must use obviously synthetic credentials and mocked HTTP
transports.
