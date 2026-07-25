from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests

ANHOCH_IMAGE_HOST: Final = "www.anhoch.com"
ANHOCH_IMAGE_PATH_PREFIX: Final = "/storage/media/"
IMAGE_CHUNK_BYTES: Final = 65_536
MAX_IMAGE_BYTES: Final = 8 * 1024 * 1024
IMAGE_EXTENSIONS: Final[Mapping[str, str]] = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
type BrowserImpersonation = Literal["chrome"]


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

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class ImageSession(Protocol):
    def get(
        self,
        url: str,
        *,
        impersonate: BrowserImpersonation,
        timeout: int,
        allow_redirects: bool,
        stream: bool,
    ) -> ImageResponse: ...


class ImageDownloader(Protocol):
    def download(self, url: str) -> DownloadedImage | None: ...


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

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield from self._response.iter_content(chunk_size=chunk_size)

    def close(self) -> None:
        self._response.close()


class _CurlCffiImageSession:
    def get(
        self,
        url: str,
        *,
        impersonate: BrowserImpersonation,
        timeout: int,
        allow_redirects: bool,
        stream: bool,
    ) -> ImageResponse:
        return _CurlCffiImageResponse(
            curl_requests.get(
                url,
                impersonate=impersonate,
                timeout=timeout,
                allow_redirects=allow_redirects,
                stream=stream,
            ),
        )


class AnhochImageDownloader:
    def __init__(self, session: ImageSession | None = None) -> None:
        self._session: ImageSession = (
            session if session is not None else _CurlCffiImageSession()
        )

    def download(self, url: str) -> DownloadedImage | None:
        if not _is_anhoch_image_url(url):
            return None
        try:
            response = self._session.get(
                url,
                impersonate="chrome",
                timeout=30,
                allow_redirects=False,
                stream=True,
            )
            try:
                return _read_image(response)
            finally:
                response.close()
        except curl_requests.RequestsError:
            return None


def _read_image(response: ImageResponse) -> DownloadedImage | None:
    if response.status_code != 200 or not _is_anhoch_image_url(response.url):
        return None
    content_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
    extension = IMAGE_EXTENSIONS.get(content_type)
    if extension is None:
        return None
    declared_length = response.headers.get("Content-Length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_IMAGE_BYTES:
                return None
        except ValueError:
            return None

    content = bytearray()
    for chunk in response.iter_content(IMAGE_CHUNK_BYTES):
        if len(content) + len(chunk) > MAX_IMAGE_BYTES:
            return None
        content.extend(chunk)
    if not content:
        return None
    return DownloadedImage(
        filename=f"product-image.{extension}",
        content_type=content_type,
        content=bytes(content),
    )


def _is_anhoch_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == ANHOCH_IMAGE_HOST
        and parsed.path.startswith(ANHOCH_IMAGE_PATH_PREFIX)
    )
