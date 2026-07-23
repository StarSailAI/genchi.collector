from __future__ import annotations


class FetcherError(RuntimeError):
    error_class = "fetcher_error"
    retryable = False


class TransientError(FetcherError):
    error_class = "transient"
    retryable = True


class RateLimitError(TransientError):
    error_class = "rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AuthenticationError(FetcherError):
    error_class = "authentication"


class ConfigurationError(FetcherError):
    error_class = "configuration"


class PermanentError(FetcherError):
    error_class = "permanent"
