from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

from rss2discord.discord.components import JSONValue
from rss2discord.discord.images import DownloadedImage

if TYPE_CHECKING:
    from rss2discord.discord.message import ReportFile


@dataclass(frozen=True, slots=True)
class DiscordRequest:
    webhook: str
    payload: dict[str, JSONValue]
    image: DownloadedImage | None = None
    files: tuple[ReportFile, ...] = ()

    def post(self, session: requests.Session) -> requests.Response:
        params = {"wait": "true", "with_components": "true"}
        if self.image is None and not self.files:
            return session.post(
                self.webhook,
                json=self.payload,
                headers={"Content-Type": "application/json"},
                params=params,
                timeout=10,
            )
        files: dict[str, tuple[str, bytes, str]] = {}
        if self.image is not None:
            files["files[0]"] = (
                self.image.filename,
                self.image.content,
                self.image.content_type,
            )
        for index, report in enumerate(self.files, start=len(files)):
            files[f"files[{index}]"] = (
                report.filename,
                report.content,
                report.content_type,
            )
        return session.post(
            self.webhook,
            data={"payload_json": json.dumps(self.payload)},
            files=files,
            params=params,
            timeout=10,
        )
