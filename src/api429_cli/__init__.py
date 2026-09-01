"""API429 command-line client."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("api429-cli")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0+unknown"

from .client import API429Client

__all__ = ["API429Client"]
