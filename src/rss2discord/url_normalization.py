from urllib.parse import urlsplit


def normalize_http_url(value: str) -> str | None:
    normalized = value.strip()
    if not normalized or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        return None
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return normalized
