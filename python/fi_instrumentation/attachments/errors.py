"""Safe transfer errors. Light module: no network, TLS or schema imports."""

from .types import SAFE_REASONS


class TransferError(Exception):
    """A safe reason only. Never preserves a server body, URL, token or cause."""

    def __init__(self, reason: str, retryable: bool = False, retry_after: float = 0, http_status: int = 0):
        reason = reason if reason in SAFE_REASONS else "incomplete"
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = min(max(float(retry_after or 0), 0.0), 5.0)
        self.http_status = http_status
