"""Minimal entry point used by the standalone PyInstaller build."""

from api429_cli.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
