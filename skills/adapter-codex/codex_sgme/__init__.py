"""Thin Codex integration layer for the SGME memory engine."""

__version__ = "0.1.2"

from .adapter import SgmeAdapter
from .config import Settings
from .lifecycle import JsonCursorStore, SessionBridge

__all__ = ["JsonCursorStore", "SessionBridge", "Settings", "SgmeAdapter", "__version__"]
