from ipaddress import ip_address
from socket import inet_aton
from typing import Final
from urllib.parse import urlsplit

MAX_HTTP_URL_LENGTH: Final = 2_048


def normalize_http_url(value: str) -> str | None:
    normalized = value.strip()
    if (
        len(normalized) > MAX_HTTP_URL_LENGTH
        or not normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
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


def normalize_remote_media_url(value: str) -> str | None:
    normalized = normalize_http_url(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() != "https":
        return None
    hostname = parsed.hostname
    if hostname is None or not hostname.isascii():
        return None
    policy_hostname = hostname.casefold().rstrip(".")
    if (
        not policy_hostname
        or "%" in policy_hostname
        or policy_hostname == "localhost"
        or policy_hostname.endswith(".localhost")
    ):
        return None
    try:
        address = ip_address(policy_hostname)
    except ValueError:
        try:
            inet_aton(policy_hostname)
        except OSError:
            return normalized
        return None
    return normalized if address.is_global and not address.is_multicast else None
