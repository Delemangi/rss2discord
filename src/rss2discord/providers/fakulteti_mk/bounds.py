"""Immutable request and response limits for Fakulteti.mk category scans."""

from typing import Final

FAKULTETI_MK_LABEL: Final = "Fakulteti.mk"
FAKULTETI_MK_ORIGIN: Final = "https://fakulteti.mk"
FAKULTETI_MK_HOSTS: Final = frozenset({"fakulteti.mk", "www.fakulteti.mk"})
FAKULTETI_MK_API_PATH: Final = "/webui/api/v1/PostManagementHandler.ashx"
FAKULTETI_MK_DEFAULT_CATEGORIES: Final = (
    "obrazovanie",
    "nauka",
    "tehnologija",
    "kariera",
)
FAKULTETI_MK_ALL_CATEGORIES: Final = (
    "nasha-tema",
    "obrazovanie",
    "nauka",
    "kultura",
    "zdravje",
    "tehnologija",
    "kolumni",
    "zhivot",
    "kariera",
    "ostanato",
)
MAX_FAKULTETI_MK_CATEGORIES: Final = len(FAKULTETI_MK_ALL_CATEGORIES)
MAX_FAKULTETI_MK_POSTS: Final = 200
FAKULTETI_MK_PAGE_LIMIT: Final = 50
MAX_FAKULTETI_MK_RESPONSE_BYTES: Final = 1_048_576
MAX_FAKULTETI_MK_SCAN_BYTES: Final = 4 * 1024 * 1024
MAX_FAKULTETI_MK_SCAN_SECONDS: Final = 120.0
FAKULTETI_MK_REQUEST_SECONDS: Final = 30
MAX_FAKULTETI_MK_TITLE_LENGTH: Final = 500
MAX_FAKULTETI_MK_DESCRIPTION_LENGTH: Final = 1_000
MAX_FAKULTETI_MK_IMAGE_URL_LENGTH: Final = 2_048
MAX_FAKULTETI_MK_CATEGORY_LENGTH: Final = 256
FAKULTETI_MK_STREAM_CHUNK_BYTES: Final = 65_536
FAKULTETI_MK_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
