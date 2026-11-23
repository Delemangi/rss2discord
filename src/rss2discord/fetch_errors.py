import re


class FeedFetchError(Exception):
    def __init__(
        self,
        strategy: str,
        cause_type: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        self.strategy = strategy
        self.cause_type = cause_type
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        detail = f"HTTP {status_code}" if status_code is not None else cause_type
        super().__init__(f"{strategy} fetch failed ({detail})")


def safe_error_cause(error: Exception) -> str:
    """Return one bounded, printable cause label for persistence and logs."""
    cause = (
        error.cause_type if isinstance(error, FeedFetchError) else type(error).__name__
    )
    if not re.fullmatch(r"[A-Za-z]\w{0,63}", cause, flags=re.ASCII):
        return "FetchError"
    if (
        isinstance(error, FeedFetchError)
        and isinstance(error.status_code, int)
        and 100 <= error.status_code <= 599
    ):
        return f"{cause} HTTP {error.status_code}"
    return cause
