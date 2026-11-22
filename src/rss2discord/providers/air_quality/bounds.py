"""Request and response bounds for the Open-Meteo air-quality feed."""

from typing import Final

AIR_QUALITY_LABEL: Final = "Air Quality"
AIR_QUALITY_HOST: Final = "air-quality-api.open-meteo.com"
AIR_QUALITY_PATH: Final = "/v1/air-quality"
AIR_QUALITY_URL: Final = (
    f"https://{AIR_QUALITY_HOST}{AIR_QUALITY_PATH}"
    "?latitude=41.9981&longitude=21.4254"
    "&current=european_aqi,pm10,pm2_5,us_aqi"
    "&timezone=Europe%2FSkopje"
)
AIR_QUALITY_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
AIR_QUALITY_REQUEST_SECONDS: Final = 15
MAX_AIR_QUALITY_RESPONSE_BYTES: Final = 1_048_576
AIR_QUALITY_STREAM_CHUNK_BYTES: Final = 65_536
