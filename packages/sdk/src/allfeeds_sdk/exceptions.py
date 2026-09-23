from __future__ import annotations

from urllib.parse import urlsplit


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


class UpstreamHTTPError(PermanentError):
    """A concrete upstream HTTP response, distinct from policy/validation errors."""

    def __init__(self, status_code: int, url: str, *, response_text: str = ""):
        parsed = urlsplit(url)
        safe_url = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
        super().__init__(f"upstream returned HTTP {status_code} for {safe_url}")
        self.status_code = status_code
        self.url = url
        # Available for source-specific tombstone detection, never included in
        # the exception message or logs. Adapters must already bound responses.
        self.response_text = response_text[:20000]
