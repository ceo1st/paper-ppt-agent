from __future__ import annotations

from backend.llm.retry import _is_retryable


class APIError(RuntimeError):
    pass


def test_socket_closed_api_error_is_retryable() -> None:
    exc = APIError(
        "API Error: The socket connection was closed unexpectedly. "
        "For more information, pass `verbose: true` in the second argument to fetch()"
    )

    assert _is_retryable(exc)


def test_nested_connection_reset_is_retryable() -> None:
    exc = RuntimeError("wrapper")
    exc.__cause__ = ConnectionResetError("read ECONNRESET")

    assert _is_retryable(exc)
