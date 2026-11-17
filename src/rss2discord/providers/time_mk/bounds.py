"""Immutable request and response limits for Time.mk category scans."""

from typing import Final

TIME_MK_LABEL: Final = "Time.mk"
TIME_MK_ORIGIN: Final = "https://time.mk"
TIME_MK_HOSTS: Final = frozenset({"time.mk", "www.time.mk"})
TIME_MK_RSS_PATH: Final = "/rss/"
TIME_MK_DEFAULT_CATEGORIES: Final = ("tehnologija", "makedonija", "ekonomija")
TIME_MK_ALL_CATEGORIES: Final = (
    "vesti",
    "sport",
    "magazin",
    "makedonija",
    "fudbal",
    "fun",
    "ekonomija",
    "kosarka",
    "avto",
    "balkan",
    "rakomet",
    "life",
    "svet",
    "tenis",
    "patuvanja",
    "skopje",
    "avtomoto",
    "zdravje",
    "hronika",
    "sportdrugo",
    "hrana",
    "republika",
    "style",
    "kultura",
    "sex",
    "tehnologija",
    "scena",
)
TIME_MK_ALL_FEED: Final = "all"
MAX_TIME_MK_CATEGORIES: Final = len(TIME_MK_ALL_CATEGORIES) + 1
MAX_TIME_MK_RESPONSE_BYTES: Final = 1_048_576
MAX_TIME_MK_SCAN_BYTES: Final = 4 * 1024 * 1024
MAX_TIME_MK_SCAN_SECONDS: Final = 120.0
TIME_MK_REQUEST_SECONDS: Final = 30
TIME_MK_STREAM_CHUNK_BYTES: Final = 65_536
TIME_MK_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
