from __future__ import annotations


class TranslationError(RuntimeError):
    """A segment/file could not be translated."""


class UnsupportedPair(TranslationError):
    """No offline language package can translate this pair."""


class AIConfigError(TranslationError):
    """The configured AI endpoint is missing or rejected the request."""


class ControlInterrupted(RuntimeError):
    """Raised internally when a task is paused or cancelled."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(action)


class SkipFile(RuntimeError):
    """Raised when a file does not need translation (language filter, etc)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
