

from __future__ import annotations

from typing import Any


class ResumeMatcherError(Exception):
    """Base class for all controlled failures."""

    exit_code = 1
    error_type = "unexpected_error"
    retryable = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        """Serializable form used on stderr."""
        return {
            "error": {
                "type": self.error_type,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


class InputError(ResumeMatcherError):
    """Bad resume / job description input (empty, wrong type, too large, unreadable)."""

    exit_code = 2
    error_type = "input_error"


class ConfigError(ResumeMatcherError):
    """Missing or invalid configuration (API key, model name, numeric settings)."""

    exit_code = 3
    error_type = "config_error"


class AuthError(ResumeMatcherError):
    """Credentials rejected by the API. Never retried."""

    exit_code = 3
    error_type = "auth_error"


class APIRequestError(ResumeMatcherError):
    """Permanent non-auth API rejection (e.g. HTTP 400). Never retried."""

    exit_code = 4
    error_type = "api_request_error"


class RateLimitError(ResumeMatcherError):
    """HTTP 429. Retried with backoff, honouring server retry guidance."""

    exit_code = 4
    error_type = "rate_limited"
    retryable = True


class ServerError(ResumeMatcherError):
    """HTTP 5xx from the API. Retried with backoff."""

    exit_code = 4
    error_type = "server_error"
    retryable = True


class ConnectionFailure(ResumeMatcherError):
    """Network-level failure. Retried with backoff."""

    exit_code = 4
    error_type = "connection_error"
    retryable = True


class RequestTimeoutError(ResumeMatcherError):
    """Request or overall deadline exceeded. Retried while time remains."""

    exit_code = 4
    error_type = "timeout"
    retryable = True


class ModelOutputError(ResumeMatcherError):
    """Model output could not be parsed or validated, even after one correction."""

    exit_code = 5
    error_type = "invalid_model_output"
