import json
from dataclasses import dataclass

import requests

from rss2discord.discord.components import JSONValue
from rss2discord.discord.images import DownloadedImage


@dataclass(frozen=True, slots=True)
class DiscordRequest:
    webhook: str
    payload: dict[str, JSONValue]
    image: DownloadedImage | None

    def post(self, session: requests.Session) -> requests.Response:
        params = {"wait": "true", "with_components": "true"}
        if self.image is None:
            return session.post(
                self.webhook,
                json=self.payload,
                headers={"Content-Type": "application/json"},
                params=params,
                timeout=10,
            )
        return session.post(
            self.webhook,
            data={"payload_json": json.dumps(self.payload)},
            files={
                "files[0]": (
                    self.image.filename,
                    self.image.content,
                    self.image.content_type,
                ),
            },
            params=params,
            timeout=10,
        )
