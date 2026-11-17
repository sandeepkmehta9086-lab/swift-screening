from __future__ import annotations


class ParseError(Exception):
    """Raised when a message cannot be parsed reliably. Always maps to a fail-closed REVIEW."""

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path
