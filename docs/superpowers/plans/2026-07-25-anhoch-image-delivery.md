# Anhoch Image Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Anhoch thumbnails as Discord attachments from the Debian deployment while keeping the working Anhoch avatar override.

**Architecture:** Add a bounded Anhoch image downloader that uses browser-compatible TLS only for the designated Anhoch source. The Discord client turns a downloaded image into a multipart attachment and replaces the Components v2 thumbnail URL with an `attachment://` reference. Oracle retains every non-Anhoch feed; Debian runs only Anhoch and receives a copied delivery-state database.

**Tech Stack:** Python 3.12, curl-cffi, requests multipart forms, Discord Components v2, pytest, Docker Compose.

## Global Constraints

- Download only HTTPS `www.anhoch.com` media URLs.
- Allow JPEG, PNG, GIF, and WebP images no larger than 8 MiB.
- Use `attachment://product-image.<ext>` only when a valid image was downloaded.
- Deliver the message without a thumbnail when the image fetch fails.
- Do not log webhook URLs or credentials.

---

### Task 1: Model downloaded image attachments

**Files:**
- Create: `src/rss2discord/discord/images.py`
- Create: `tests/test_discord_images.py`

**Interfaces:**
- Produces `DownloadedImage(filename: str, content_type: str, content: bytes)`.
- Produces `AnhochImageDownloader.download(url: str) -> DownloadedImage | None`.

- [ ] **Step 1: Write failing image-boundary tests**

```python
def test_anhoch_image_downloader_returns_valid_jpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a browser-compatible response with an Anhoch JPEG
    # When downloading the image
    # Then return a product-image.jpg attachment
    assert downloader.download(image_url) == DownloadedImage(
        filename="product-image.jpg", content_type="image/jpeg", content=b"image",
    )

def test_anhoch_image_downloader_omits_non_image_response(...) -> None:
    # Given an HTML/403 response
    # When downloading
    # Then no attachment is returned
    assert downloader.download(image_url) is None
```

- [ ] **Step 2: Run tests to verify red**

Run: `uv run pytest tests/test_discord_images.py -q`

- [ ] **Step 3: Implement bounded, typed image download**

```python
@dataclass(frozen=True, slots=True)
class DownloadedImage:
    filename: str
    content_type: str
    content: bytes


class AnhochImageDownloader:
    def download(self, url: str) -> DownloadedImage | None: ...
```

- [ ] **Step 4: Run the image tests**

Run: `uv run pytest tests/test_discord_images.py -q`

### Task 2: Send thumbnails as Discord attachments

**Files:**
- Modify: `src/rss2discord/discord/client.py`
- Modify: `src/rss2discord/discord/components.py`
- Modify: `tests/test_discord_client.py`
- Modify: `tests/test_discord_components.py`

**Interfaces:**
- `build_components_v2_payload(..., image_url: str | None = None)` uses the supplied attachment URL when present.
- `DiscordWebhookClient.send()` posts multipart `payload_json` and `files` for a downloaded Anhoch image.

- [ ] **Step 1: Write failing multipart delivery test**

```python
def test_delivery_uploads_anhoch_image_as_thumbnail_attachment(...) -> None:
    # Given an Anhoch message and a downloaded JPEG
    # When the client sends it
    # Then multipart payload_json references attachment://product-image.jpg
    assert arguments["files"]["files[0]"][0] == "product-image.jpg"
    assert attachment_url == "attachment://product-image.jpg"
```

- [ ] **Step 2: Run targeted client test to verify red**

Run: `uv run pytest tests/test_discord_client.py::test_delivery_uploads_anhoch_image_as_thumbnail_attachment -q`

- [ ] **Step 3: Implement multipart delivery with no-image fallback**

```python
if downloaded_image is None:
    return self._session.post(..., json=payload, ...)
return self._session.post(..., data={"payload_json": json.dumps(payload)}, files={...}, ...)
```

- [ ] **Step 4: Run client and component tests**

Run: `uv run pytest tests/test_discord_client.py tests/test_discord_components.py -q`

### Task 3: Package and deploy split feed configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md`

- [ ] **Step 1: Add runtime curl-cffi dependency**

Run: `uv add curl-cffi`

- [ ] **Step 2: Build and test the container**

Run: `docker build -t rss2discord:anhoch-images .`

- [ ] **Step 3: Transfer and activate deployment state**

Stop Oracle, copy `config/config.yaml` and `data/state.db` to Debian, remove Anhoch from Oracle config, retain only Anhoch on Debian, then start both Compose projects.

- [ ] **Step 4: Verify live delivery**

Send a temporary Anhoch message from Debian and assert its Discord attachment CDN URL returns HTTP 200 image content and its author avatar is non-null; delete it afterward.
