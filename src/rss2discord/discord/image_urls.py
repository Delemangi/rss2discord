from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ANHOCH_IMAGE_HOST: Final = "www.anhoch.com"
ANHOCH_IMAGE_PATH_PREFIXES: Final = ("/images/", "/storage/media/")
DDSTORE_IMAGE_HOST: Final = "ddstore.mk"
DDSTORE_IMAGE_PATH_PREFIXES: Final = ("/media/",)
type ImageSource = Literal["anhoch", "ddstore"]
IMAGE_POLICY_BY_SOURCE: Final[Mapping[ImageSource, tuple[str, tuple[str, ...]]]] = {
    "anhoch": (ANHOCH_IMAGE_HOST, ANHOCH_IMAGE_PATH_PREFIXES),
    "ddstore": (DDSTORE_IMAGE_HOST, DDSTORE_IMAGE_PATH_PREFIXES),
}
IMAGE_SOURCE_BY_STRATEGY: Final[Mapping[str, ImageSource]] = {
    "anhoch": "anhoch",
    "ddstore": "ddstore",
}


def canonical_product_image_url(url: str, source: ImageSource) -> str | None:
    """Return a canonical URL only for an approved first-party image path."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    hostname, path_prefixes = IMAGE_POLICY_BY_SOURCE[source]
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
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
