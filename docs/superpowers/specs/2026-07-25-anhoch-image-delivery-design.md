# Anhoch Image Delivery Design

## Goal

Deliver Anhoch product thumbnails and the configured Anhoch webhook avatar reliably in Discord while preserving existing delivery and price-monitor state.

## Architecture

The Anhoch feed runs on `debian`; all other feeds remain on `oracle`. Before the split, the current SQLite state is copied while the Oracle service is stopped so the Anhoch deployment retains delivery and price history without duplicate notifications.

For an Anhoch message with an image URL, the Debian deployment downloads the image with browser-compatible TLS impersonation under strict response-size and content-type limits. The Discord request is sent as multipart form data with the image in `files[0]`, attachment metadata in `payload_json.attachments`, and the Components v2 thumbnail media URL set to `attachment://product-image.<ext>`.

Messages without an image continue to use JSON delivery. If an image cannot be downloaded or is rejected at the media boundary, the update is delivered without a thumbnail; image failure must not suppress a product or price notification.

## Boundaries

- Only `https` Anhoch media URLs are eligible for downloading.
- Redirects must remain on the Anhoch domain.
- Accepted media types are JPEG, PNG, GIF, and WebP.
- Downloaded media is bounded below Discord's upload limit.
- Discord retries reuse the already-downloaded bytes and rebuild multipart fields for each request.
- Webhook URLs and tokens are never logged.

## Deployment

1. Stop `rss2discord` on `oracle`.
2. Copy the active configuration and SQLite state to `debian`.
3. Create an Oracle configuration without the Anhoch feed.
4. Create a Debian configuration containing only the Anhoch feed.
5. Start both deployments and confirm exactly one active Anhoch scheduler.

## Verification

- Regression tests prove multipart attachment construction, thumbnail reference, retry behavior, and no-image fallback.
- Full tests, Ruff, and Mypy pass.
- A live Debian scrape downloads an Anhoch image.
- A temporary Discord delivery is read back with the configured avatar and an attachment CDN URL returning HTTP 200 image content, then deleted.
- Both deployments remain healthy after restart and their feed sets are disjoint.
