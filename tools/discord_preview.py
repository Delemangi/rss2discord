"""Render Discord Components V2 webhook payloads to HTML and screenshot them.

This is a development aid, not part of the shipped package. It takes the exact
payloads produced by ``build_components_v2_payload`` and draws them the way the
Discord client does, so component layout changes can be reviewed visually.

Usage::

    python tools/discord_preview.py --out preview
    python tools/discord_preview.py --out preview --only setec_price_drop

Screenshotting needs Chrome or Chromium on PATH; pass ``--no-screenshot`` to
emit only the HTML.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

ACTION_ROW_COMPONENT = 1
BUTTON_COMPONENT = 2
TEXT_DISPLAY_COMPONENT = 10
SECTION_COMPONENT = 9
THUMBNAIL_COMPONENT = 11
SEPARATOR_COMPONENT = 14
CONTAINER_COMPONENT = 17

BUTTON_STYLES = {1: "primary", 2: "secondary", 3: "success", 4: "danger", 5: "link"}

# Stand-in for attachment:// media, which only resolves once Discord has the
# upload. Keeps the thumbnail box occupied so spacing stays representative.
ATTACHMENT_PLACEHOLDER = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='86' height='86'>"
    "<rect width='86' height='86' fill='%231e1f22'/>"
    "<text x='43' y='40' font-family='sans-serif' font-size='9' fill='%23949ba4'"
    " text-anchor='middle'>uploaded</text>"
    "<text x='43' y='53' font-family='sans-serif' font-size='9' fill='%23949ba4'"
    " text-anchor='middle'>image</text></svg>"
)


@dataclass(frozen=True, slots=True)
class Sample:
    key: str
    caption: str
    payload: dict[str, Any]


def render_document(samples: list[Sample], *, now: datetime) -> str:
    messages = "\n".join(_render_sample(sample, now=now) for sample in samples)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>rss2discord component preview</title>
<style>{_STYLESHEET}</style>
</head><body>
<div class="chat">
{messages}
</div>
<script>
// Reported back through --dump-dom so the screenshot pass can size the window.
document.body.dataset.height = String(document.documentElement.scrollHeight);
</script>
</body></html>
"""


def _render_sample(sample: Sample, *, now: datetime) -> str:
    payload = sample.payload
    username = str(payload.get("username") or "rss2discord")
    avatar = payload.get("avatar_url")
    avatar_markup = (
        f'<img class="avatar" src="{html.escape(str(avatar), quote=True)}" alt="">'
        if avatar
        else '<div class="avatar avatar-fallback"></div>'
    )
    components = payload.get("components") or []
    body = "\n".join(_render_component(component, now=now) for component in components)
    stamp = now.strftime("%H:%M")
    return f"""<section class="sample" id="{html.escape(sample.key, quote=True)}">
  <div class="caption">{html.escape(sample.caption)}</div>
  <div class="message">
    {avatar_markup}
    <div class="message-body">
      <div class="message-head">
        <span class="author">{html.escape(username)}</span>
        <span class="app-tag">APP</span>
        <span class="stamp">Today at {stamp}</span>
      </div>
      {body}
    </div>
  </div>
</section>"""


def _render_component(component: dict[str, Any], *, now: datetime) -> str:
    kind = component.get("type")
    if kind == CONTAINER_COMPONENT:
        accent = component.get("accent_color")
        bar = ""
        if isinstance(accent, int):
            bar = f'<div class="accent" style="background:#{accent:06x}"></div>'
        children = "\n".join(
            _render_component(child, now=now)
            for child in component.get("components") or []
        )
        return f'<div class="container">{bar}<div class="container-body">{children}</div></div>'

    if kind == TEXT_DISPLAY_COMPONENT:
        return render_discord_markdown(str(component.get("content") or ""), now=now)

    if kind == SECTION_COMPONENT:
        children = "\n".join(
            _render_component(child, now=now)
            for child in component.get("components") or []
        )
        accessory = component.get("accessory") or {}
        return (
            f'<div class="section"><div class="section-text">{children}</div>'
            f"{_render_accessory(accessory)}</div>"
        )

    if kind == SEPARATOR_COMPONENT:
        spacing = "large" if component.get("spacing") == 2 else "small"
        rule = "divider" if component.get("divider", True) else "blank"
        return f'<div class="separator {spacing} {rule}"></div>'

    if kind == ACTION_ROW_COMPONENT:
        buttons = "".join(
            _render_button(child)
            for child in component.get("components") or []
            if child.get("type") == BUTTON_COMPONENT
        )
        return f'<div class="action-row">{buttons}</div>'

    return f'<div class="unknown">unsupported component type {html.escape(str(kind))}</div>'


def _render_button(button: dict[str, Any]) -> str:
    style = BUTTON_STYLES.get(button.get("style", 2), "secondary")
    label = html.escape(str(button.get("label") or ""))
    # Link buttons carry a trailing external-link glyph in the Discord client.
    icon = '<span class="ext">↗</span>' if style == "link" else ""
    return f'<span class="btn btn-{style}">{label}{icon}</span>'


def _render_accessory(accessory: dict[str, Any]) -> str:
    if accessory.get("type") != THUMBNAIL_COMPONENT:
        return ""
    url = str((accessory.get("media") or {}).get("url") or "")
    if url.startswith("attachment://"):
        url = ATTACHMENT_PLACEHOLDER
    return f'<img class="thumb" src="{html.escape(url, quote=True)}" alt="">'


def render_discord_markdown(text: str, *, now: datetime) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            joined = "<br>".join(paragraph)
            blocks.append(f'<div class="para">{joined}</div>')
            paragraph.clear()

    for line in text.split("\n"):
        if line.startswith("## "):
            flush()
            blocks.append(f"<h2>{_render_inline(line[3:], now=now)}</h2>")
        elif line.startswith("### "):
            flush()
            blocks.append(f"<h3>{_render_inline(line[4:], now=now)}</h3>")
        elif line.startswith("# "):
            flush()
            blocks.append(f"<h1>{_render_inline(line[2:], now=now)}</h1>")
        elif line.startswith("-# "):
            flush()
            blocks.append(f'<div class="subtext">{_render_inline(line[3:], now=now)}</div>')
        else:
            paragraph.append(_render_inline(line, now=now))
    flush()
    return "\n".join(blocks)


_TIMESTAMP = re.compile(r"<t:(-?\d+)(?::([tTdDfFR]))?>")
_LINK = re.compile(r"\[((?:[^\[\]\\]|\\.)*)\]\(([^()\s]+)\)")


def _render_inline(text: str, *, now: datetime) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]

        if char == "\\" and index + 1 < length:
            out.append(html.escape(text[index + 1]))
            index += 2
            continue

        if char == "<":
            match = _TIMESTAMP.match(text, index)
            if match:
                out.append(_render_timestamp(match, now=now))
                index = match.end()
                continue

        if char == "[":
            match = _LINK.match(text, index)
            if match:
                label = _render_inline(match.group(1), now=now)
                href = html.escape(match.group(2), quote=True)
                out.append(f'<a href="{href}">{label}</a>')
                index = match.end()
                continue

        if text.startswith("**", index):
            closing = text.find("**", index + 2)
            if closing != -1:
                inner = _render_inline(text[index + 2 : closing], now=now)
                out.append(f"<strong>{inner}</strong>")
                index = closing + 2
                continue

        if text.startswith("~~", index):
            closing = text.find("~~", index + 2)
            if closing != -1:
                inner = _render_inline(text[index + 2 : closing], now=now)
                out.append(f"<s>{inner}</s>")
                index = closing + 2
                continue

        if char == "*":
            closing = text.find("*", index + 1)
            if closing != -1:
                inner = _render_inline(text[index + 1 : closing], now=now)
                out.append(f"<em>{inner}</em>")
                index = closing + 1
                continue

        if char == "`":
            closing = text.find("`", index + 1)
            if closing != -1:
                inner = html.escape(text[index + 1 : closing])
                out.append(f"<code>{inner}</code>")
                index = closing + 1
                continue

        out.append(html.escape(char))
        index += 1
    return "".join(out)


def _render_timestamp(match: re.Match[str], *, now: datetime) -> str:
    epoch = int(match.group(1))
    style = match.group(2) or "f"
    moment = datetime.fromtimestamp(epoch, tz=UTC)
    if style == "R":
        label = _relative(moment, now)
    elif style in {"t", "T"}:
        label = moment.strftime("%H:%M")
    elif style in {"d", "D"}:
        label = moment.strftime("%d/%m/%Y")
    else:
        label = moment.strftime("%d %B %Y %H:%M")
    return f'<span class="timestamp">{html.escape(label)}</span>'


def _relative(moment: datetime, now: datetime) -> str:
    delta = int((now - moment).total_seconds())
    past = delta >= 0
    delta = abs(delta)
    for seconds, unit in (
        (60, "second"),
        (3600, "minute"),
        (86400, "hour"),
        (2592000, "day"),
        (31536000, "month"),
    ):
        if delta < seconds:
            step = seconds // 60 if unit != "second" else 1
            value = max(1, delta // step) if unit != "second" else max(1, delta)
            plural = "" if value == 1 else "s"
            return f"{value} {unit}{plural} ago" if past else f"in {value} {unit}{plural}"
    years = max(1, delta // 31536000)
    plural = "" if years == 1 else "s"
    return f"{years} year{plural} ago" if past else f"in {years} year{plural}"


_STYLESHEET = """
:root {
  --chat-bg: #313338;
  --container-bg: #2b2d31;
  --container-border: rgba(255, 255, 255, 0.06);
  --text: #dbdee1;
  --header: #f2f3f5;
  --muted: #949ba4;
  --link: #00a8fc;
  --rule: #3f4147;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--chat-bg);
  font-family: "gg sans", "Noto Sans", "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: var(--text);
  -webkit-font-smoothing: antialiased;
}
.chat { padding: 24px 24px 32px; width: 860px; }
.sample + .sample { margin-top: 34px; }
.caption {
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #6d6f78;
  margin-bottom: 10px;
  font-weight: 600;
}
.message { display: flex; gap: 16px; }
.avatar { width: 40px; height: 40px; border-radius: 50%; flex: none; object-fit: cover; }
.avatar-fallback { background: #5865f2; }
.message-body { min-width: 0; flex: 1; }
.message-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.author { color: var(--header); font-weight: 500; font-size: 16px; }
.app-tag {
  background: #5865f2; color: #fff; font-size: 10px; font-weight: 600;
  border-radius: 3px; padding: 1px 4px; line-height: 14px; text-transform: uppercase;
}
.stamp { color: var(--muted); font-size: 12px; }

/* Components V2 container */
.container {
  display: flex;
  background: var(--container-bg);
  border: 1px solid var(--container-border);
  border-radius: 8px;
  overflow: hidden;
  max-width: 600px;
}
.accent { width: 4px; flex: none; align-self: stretch; }
.container-body { padding: 16px; min-width: 0; flex: 1; }

.section { display: flex; gap: 16px; align-items: flex-start; }
.section-text { min-width: 0; flex: 1; }
.thumb {
  width: 86px; height: 86px; flex: none;
  border-radius: 8px; object-fit: cover; background: #1e1f22;
}

.separator { border: 0; }
.separator.small { margin: 8px 0; }
.separator.large { margin: 16px 0; }
.separator.divider { border-top: 1px solid var(--rule); }

h1, h2, h3 { color: var(--header); margin: 8px 0 4px; line-height: 1.375; }
h1 { font-size: 24px; font-weight: 700; }
h2 { font-size: 20px; font-weight: 700; }
h3 { font-size: 16px; font-weight: 700; }
.container-body > h1:first-child,
.container-body > h2:first-child,
.container-body > h3:first-child,
.section-text > *:first-child { margin-top: 0; }
.container-body > *:last-child, .section-text > *:last-child { margin-bottom: 0; }

.para { font-size: 16px; line-height: 1.375; margin: 4px 0; white-space: pre-wrap; }
.subtext { font-size: 12.8px; line-height: 1.3; color: var(--muted); margin: 4px 0; }
.subtext + .subtext { margin-top: 2px; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
h2 a { color: var(--link); }
.timestamp { background: rgba(88, 101, 242, 0.15); border-radius: 3px; padding: 0 2px; }
code {
  background: #1e1f22; border-radius: 4px; padding: 1px 3px;
  font-family: Consolas, "Andale Mono WT", monospace; font-size: 13px;
}
.unknown { color: #f23f43; font-size: 13px; }

.action-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 14px; font-weight: 500; line-height: 16px;
  padding: 8px 16px; border-radius: 8px; color: #fff;
}
.btn-link, .btn-secondary { background: #4e5058; }
.btn-primary { background: #5865f2; }
.btn-success { background: #248046; }
.btn-danger { background: #da373c; }
.ext { font-size: 12px; opacity: 0.75; }
"""


def screenshot(html_path: Path, png_path: Path, *, width: int) -> bool:
    chrome = next((c for c in CHROME_CANDIDATES if shutil.which(c)), None)
    if chrome is None:
        print("no chrome/chromium on PATH; skipping screenshots", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory() as profile:
        base = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
        ]
        # Pass 1: measure the rendered height that the document reports.
        dump = subprocess.run(  # noqa: S603
            [*base, f"--window-size={width},1000", "--dump-dom", html_path.as_uri()],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        match = re.search(r'data-height="(\d+)"', dump.stdout)
        height = int(match.group(1)) if match else 2000

        # Pass 2: capture at the measured size so there is no dead space.
        subprocess.run(  # noqa: S603
            [
                *base,
                f"--window-size={width},{height}",
                f"--screenshot={png_path}",
                html_path.as_uri(),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
    return png_path.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("preview"))
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--width", type=int, default=908)
    parser.add_argument("--no-screenshot", action="store_true")
    parser.add_argument(
        "--payloads",
        type=Path,
        default=None,
        help="JSON object of {name: payload}; defaults to the built-in samples",
    )
    args = parser.parse_args()

    if args.payloads is not None:
        raw = json.loads(args.payloads.read_text(encoding="utf-8"))
        samples = [Sample(key, key, payload) for key, payload in raw.items()]
    else:
        from preview_samples import build_samples  # noqa: PLC0415

        samples = build_samples()

    if args.only:
        wanted = set(args.only)
        samples = [sample for sample in samples if sample.key in wanted]
        if not samples:
            print(f"no samples matched {sorted(wanted)}", file=sys.stderr)
            return 1

    args.out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    html_path = args.out / "preview.html"
    html_path.write_text(render_document(samples, now=now), encoding="utf-8")
    print(f"wrote {html_path}")

    if not args.no_screenshot:
        png_path = args.out / "preview.png"
        if screenshot(html_path, png_path, width=args.width):
            print(f"wrote {png_path}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())
