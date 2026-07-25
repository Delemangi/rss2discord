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
