from typing import Final, assert_never
from urllib.parse import urlsplit

from rss2discord.configuration import FeedConfig

SOURCE_LABEL_FORUM: Final = "Forum"
SOURCE_LABEL_GITHUB: Final = "GitHub"
SOURCE_LABEL_ANHOCH: Final = "Anhoch"
SOURCE_LABEL_DDSTORE: Final = "DDStore"
SOURCE_LABEL_HIVETEC: Final = "Hivetec"
SOURCE_LABEL_GJIRAFA50: Final = "Gjirafa50"
SOURCE_LABEL_SETEC: Final = "Setec"
SOURCE_LABEL_NEKSIO: Final = "Neksio"
SOURCE_LABEL_NEPTUN: Final = "Neptun"
SOURCE_LABEL_PAZAR3: Final = "Pazar3"
SOURCE_LABEL_REKLAMA5: Final = "Reklama5"
SOURCE_LABEL_ITMK_OGLASNIK: Final = "IT.mk Oglasnik"
SOURCE_LABEL_REDDIT: Final = "Reddit"
SOURCE_LABEL_HACKER_NEWS: Final = "Hacker News"
SOURCE_LABEL_RSS: Final = "RSS"


def source_label(feed: FeedConfig) -> str:
    if feed.adapter is not None:
        match feed.adapter:
            case "hackernews":
                return SOURCE_LABEL_HACKER_NEWS
            case "reddit":
                return SOURCE_LABEL_REDDIT
            case unreachable:
                assert_never(unreachable)
    match feed.strategy:
        case "anhoch":
            return SOURCE_LABEL_ANHOCH
        case "ddstore":
            return SOURCE_LABEL_DDSTORE
        case "hivetec":
            return SOURCE_LABEL_HIVETEC
        case "gjirafa50":
            return SOURCE_LABEL_GJIRAFA50
        case "setec":
            return SOURCE_LABEL_SETEC
        case "neksio":
            return SOURCE_LABEL_NEKSIO
        case "neptun":
            return SOURCE_LABEL_NEPTUN
        case "pazar3":
            return SOURCE_LABEL_PAZAR3
        case "reklama5":
            return SOURCE_LABEL_REKLAMA5
        case "xenforo":
            return SOURCE_LABEL_FORUM
        case "itmk_oglasnik":
            return SOURCE_LABEL_ITMK_OGLASNIK
        case "rss":
            return _rss_source_label(feed.url)
        case unreachable_strategy:
            assert_never(unreachable_strategy)


def _rss_source_label(url: str) -> str:
    try:
        parsed_url = urlsplit(url)
        hostname = parsed_url.hostname
    except ValueError:
        return SOURCE_LABEL_RSS
    if hostname is None:
        return SOURCE_LABEL_RSS
    hostname_lower = hostname.lower()
    path_segments = tuple(segment for segment in parsed_url.path.split("/") if segment)
    if (
        hostname_lower == "github.com"
        and len(path_segments) == 3
        and path_segments[-1] == "releases.atom"
    ):
        return SOURCE_LABEL_GITHUB
    if hostname_lower == "news.ycombinator.com":
        return SOURCE_LABEL_HACKER_NEWS
    if hostname_lower == "reddit.com" or hostname_lower.endswith(".reddit.com"):
        return SOURCE_LABEL_REDDIT
    return SOURCE_LABEL_RSS
