from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

ANHOCH_IMAGE_HOST: Final = "www.anhoch.com"
ANHOCH_IMAGE_PATH_PREFIX: Final = "/storage/media/"
MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
MAX_IMAGE_REDIRECTS: Final = 3
IMAGE_TIMEOUT_MS: Final = 30_000
IMAGE_EXTENSIONS: Final[Mapping[str, str]] = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
IMAGE_SIGNATURES: Final[Mapping[str, tuple[bytes, ...]]] = {
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}
type BrowserImpersonation = Literal["chrome"]
type ContentCallback = Callable[[bytes], int]


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    filename: str
    content_type: str
    content: bytes


class ImageResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def url(self) -> str: ...


class ImageSession(Protocol):
    def get(
        self,
        url: str,
        *,
        impersonate: BrowserImpersonation,
        timeout: int,
        allow_redirects: bool,
        content_callback: ContentCallback,
        curl_options: Mapping[CurlOpt, int],
    ) -> ImageResponse: ...


class ImageDownloader(Protocol):
    def download(self, url: str) -> DownloadedImage | None: ...


class _BoundedImageContent:
    """Accumulate one response while enforcing the upload-size boundary."""

    def __init__(self) -> None:
        self.content = bytearray()
        self.exceeded_limit = False

    def write(self, chunk: bytes) -> int:
        if len(self.content) + len(chunk) > MAX_IMAGE_BYTES:
            self.exceeded_limit = True
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


class _CurlCffiImageResponse:
    def __init__(self, response: curl_requests.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            name: value
            for name, value in self._response.headers.items()
            if value is not None
        }

    @property
    def url(self) -> str:
        return self._response.url


class _CurlCffiImageSession:
    def get(
        self,
        url: str,
        *,
        impersonate: BrowserImpersonation,
        timeout: int,
        allow_redirects: bool,
        content_callback: ContentCallback,
        curl_options: Mapping[CurlOpt, int],
    ) -> ImageResponse:
        return _CurlCffiImageResponse(
            curl_requests.get(
                url,
                impersonate=impersonate,
                timeout=timeout,
                allow_redirects=allow_redirects,
                content_callback=content_callback,
                curl_options=dict(curl_options),
            ),
        )


class AnhochImageDownloader:
    def __init__(self, session: ImageSession | None = None) -> None:
        self._session: ImageSession = (
            session if session is not None else _CurlCffiImageSession()
        )

    def download(self, url: str) -> DownloadedImage | None:
        current_url = _canonical_anhoch_image_url(url)
        if current_url is None:
            return None

        for _ in range(MAX_IMAGE_REDIRECTS + 1):
            content = _BoundedImageContent()

            try:
                response = self._session.get(
                    current_url,
                    impersonate="chrome",
                    timeout=30,
                    allow_redirects=False,
                    content_callback=content.write,
                    curl_options={CurlOpt.TIMEOUT_MS: IMAGE_TIMEOUT_MS},
                )
            except curl_requests.RequestsError:
                return None
            if content.exceeded_limit:
                return None
            response_url = _canonical_anhoch_image_url(response.url)
            if response_url != current_url:
                return None
            if not 300 <= response.status_code < 400:
                return _read_image(response, bytes(content.content))
            location = _header(response.headers, "location")
            if location is None:
                return None
            redirected_url = _canonical_anhoch_image_url(
                urljoin(current_url, location),
            )
            if redirected_url is None:
                return None
            current_url = redirected_url
        return None


def _read_image(response: ImageResponse, content: bytes) -> DownloadedImage | None:
    if response.status_code != 200 or _canonical_anhoch_image_url(response.url) is None:
        return None
    content_type = (
        (_header(response.headers, "content-type") or "").partition(";")[0].lower()
    )
    extension = IMAGE_EXTENSIONS.get(content_type)
    if extension is None:
        return None
    declared_length = _header(response.headers, "content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_IMAGE_BYTES:
                return None
        except ValueError:
            return None

    if not content or not _has_image_signature(content_type, content):
        return None
    return DownloadedImage(
        filename=f"product-image.{extension}",
        content_type=content_type,
        content=content,
    )


def _canonical_anhoch_image_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != ANHOCH_IMAGE_HOST
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
        or not decoded_path.startswith(ANHOCH_IMAGE_PATH_PREFIX)
    ):
        return None
    canonical_path = quote(decoded_path, safe="/-._~")
    return urlunsplit(("https", ANHOCH_IMAGE_HOST, canonical_path, parsed.query, ""))


def _has_image_signature(content_type: str, content: bytes) -> bool:
    if content_type == "image/webp":
        return content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    signatures = IMAGE_SIGNATURES.get(content_type, ())
    return any(content.startswith(signature) for signature in signatures)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded_name = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == folded_name),
        None,
    )
