from rss2discord.retries import parse_retry_after


def test_parse_retry_after_rejects_overflowing_http_date() -> None:
    # Given
    retry_after = "Sun, 06 Nov 9999999999 08:49:37 GMT"

    # When
    delay = parse_retry_after(retry_after)

    # Then
    assert delay is None
