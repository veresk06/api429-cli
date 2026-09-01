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
mypy src
pytest
python -m build
```

The production package is checked with strict mypy settings. Tests use typed
fixtures where practical but are not part of the strict public API check.

Never commit API keys, account data, prompts, generated media, or production
responses. Tests must use obviously synthetic credentials and mocked HTTP
transports.
