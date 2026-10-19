"""Stable errors shared by browser actions and private storage."""

from .service import BrowserServiceError


class AgentBrowserError(BrowserServiceError):
    """An actionable error with a stable code for the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
