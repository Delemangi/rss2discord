"""Small, source-specific presentation helpers for the pwn.watch RSS feed."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_SEVERITY_PREFIX = re.compile(
    r"\[(critical|high|medium|low|unknown)\]\s*",
    re.IGNORECASE,
)
_SEVERITY_COLORS = {
    "critical": 0xD83C3E,
    "high": 0xE67E22,
    "medium": 0xD4A017,
    "low": 0x3B82A0,
    "unknown": 0x68707A,
}


@dataclass(frozen=True)
class PwnWatchPresentation:
    title: str
    description: str
    severity: str | None


def is_pwn_watch_feed(strategy: str, feed_url: str) -> bool:
    """Match only the verified HTTPS all-incidents RSS endpoint."""
    if strategy != "rss":
        return False
    try:
        parsed = urlsplit(feed_url)
        return (
            feed_url == feed_url.strip()
            and not any(ord(character) <= 0x20 for character in feed_url)
            and parsed.scheme.casefold() == "https"
            and parsed.hostname is not None
            and parsed.hostname.casefold() == "pwn.watch"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/all/feed.xml"
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def present_pwn_watch(title: str, description: str) -> PwnWatchPresentation:
    match = _SEVERITY_PREFIX.match(title)
    severity = match.group(1).casefold() if match else None
    visible_title = title[match.end() :] if match else title
    # Drop only a complete first line/paragraph which equals a non-empty
    # headline. Never strip a matching prefix from a factual sentence.
    body = description
    if visible_title:
        lines = body.splitlines(keepends=True)
        if lines:
            first_line = lines[0].rstrip("\r\n").strip()
            duplicates = {visible_title}
            if match:
                duplicates.add(title)
            if first_line in duplicates:
                body = "".join(lines[1:])
                body = body.lstrip("\r\n")
    # The shared RSS cleaner removes source-link markup; restore a readable
    # boundary where its exact marker otherwise runs into an ISO date.
    body = re.sub(r"\(source\)(?=\d{4}-\d{2}-\d{2}:)", "(source)\n", body)
    return PwnWatchPresentation(visible_title, body, severity)


def severity_color(severity: str | None) -> int | None:
    return _SEVERITY_COLORS.get(severity) if severity is not None else None
