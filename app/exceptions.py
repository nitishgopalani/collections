"""Application exceptions."""


class StaleStateError(Exception):
    """Raised when optimistic concurrency detects a stale conversation state version."""

    def __init__(self, expected_previous: int, actual: int) -> None:
        self.expected_previous = expected_previous
        self.actual = actual
        super().__init__(
            f"Stale conversation state: expected version {expected_previous}, found {actual}"
        )


class ToolInvocationError(Exception):
    """Raised when a governed tool call fails or times out."""
