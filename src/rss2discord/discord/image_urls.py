from collections.abc import Mapping
from typing import Final
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ANHOCH_IMAGE_HOST: Final = "www.anhoch.com"
ANHOCH_IMAGE_PATH_PREFIXES: Final = ("/images/", "/storage/media/")
DDSTORE_IMAGE_HOST: Final = "ddstore.mk"
DDSTORE_IMAGE_PATH_PREFIXES: Final = ("/media/catalog/product/",)
IMAGE_PATH_PREFIXES_BY_HOST: Final[Mapping[str, tuple[str, ...]]] = {
    ANHOCH_IMAGE_HOST: ANHOCH_IMAGE_PATH_PREFIXES,
    DDSTORE_IMAGE_HOST: DDSTORE_IMAGE_PATH_PREFIXES,
}


def canonical_product_image_url(url: str) -> str | None:
    """Return a canonical URL only for an approved first-party image path."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname or ""
    path_prefixes = IMAGE_PATH_PREFIXES_BY_HOST.get(hostname)
    if (
        parsed.scheme != "https"
        or path_prefixes is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return None
    decoded_path = unquote(parsed.path)
    if (
        "\\" in parsed.path
        or "\\" in decoded_path
        or decoded_path.count("/") != parsed.path.count("/")
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or not decoded_path.startswith(path_prefixes)
    ):
        return None
    canonical_path = quote(decoded_path, safe="/-._~")
    return urlunsplit(("https", hostname, canonical_path, parsed.query, ""))
