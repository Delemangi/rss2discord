"""Representative entries for the component preview.

``build_samples`` runs them through the real ``build_components_v2_payload`` so
the preview always reflects what the webhook would actually send. ``CASES``
exposes the underlying feed/entry pairs so alternative layouts can be
prototyped against the same material before any of them lands in ``src``.
"""

from __future__ import annotations

from dataclasses import dataclass

from discord_preview import Sample

from rss2discord.configuration import FeedConfig
from rss2discord.discord.components import build_components_v2_payload
from rss2discord.models import EntryData, PriceDirection, SourceMetric

WEBHOOK = "https://discord.com/api/webhooks/1/preview"

# Stable stand-ins for product photography, so previews render identically on
# every run without depending on a retailer CDN.
LAPTOP_IMAGE = "https://picsum.photos/seed/rss2discord-laptop/400/400"
PSU_IMAGE = "https://picsum.photos/seed/rss2discord-psu/400/400"
GPU_IMAGE = "https://picsum.photos/seed/rss2discord-gpu/400/400"
RAM_IMAGE = "https://picsum.photos/seed/rss2discord-ram/400/400"


@dataclass(frozen=True, slots=True)
class Case:
    key: str
    caption: str
    feed: FeedConfig
    entry: EntryData
    source_title: str


CASES: tuple[Case, ...] = (
    Case(
        key="catalog_product",
        caption="New catalog product — Anhoch (image, discount, stock, installments)",
        feed=FeedConfig(
            id="anhoch-laptops",
            url="https://www.anhoch.com/categories/laptop-computers",
            webhook=WEBHOOK,
            strategy="anhoch",
            name="Anhoch Laptops",
        ),
        entry=EntryData(
            title=(
                "Lenovo LOQ 15IRX9 Gaming Laptop, Intel Core i7-13650HX, 16GB DDR5, "
                '1TB SSD, RTX 4060 8GB, 15.6" FHD 144Hz'
            ),
            link="https://www.anhoch.com/products/lenovo-loq-15irx9-83dv00k9ya",
            description="",
            author="",
            timestamp="2026-08-11T06:44:00+00:00",
            image_url=LAPTOP_IMAGE,
            source_metrics=(
                SourceMetric("Price", "68.990 ден"),
                SourceMetric("Original", "79.990 ден"),
                SourceMetric("Stock", "3"),
                SourceMetric("Installments", "12 × 5.749 ден"),
            ),
        ),
        source_title="Anhoch",
    ),
    Case(
        key="price_drop",
        caption="Price drop alert — Setec (previous price, categories)",
        feed=FeedConfig(
            id="setec-psu",
            url="https://setec.mk/search?q=psu",
            webhook=WEBHOOK,
            strategy="setec",
            name="Setec PSU watch",
        ),
        entry=EntryData(
            title="Seasonic FOCUS GX-850 850W 80+ Gold Full Modular ATX 3.0",
            link="https://setec.mk/products/seasonic-focus-gx-850",
            description="",
            author="",
            timestamp=None,
            image_url=PSU_IMAGE,
            categories=("Компјутерски компоненти", "Напојувања", "Seasonic"),
            source_metrics=(
                SourceMetric("Price", "7.990 ден"),
                SourceMetric("Previous", "9.490 ден", prior=True),
                SourceMetric("Original", "10.990 ден"),
            ),
            price_direction=PriceDirection.DECREASE,
        ),
        source_title="Setec",
    ),
    Case(
        key="price_increase",
        caption="Price increase alert — Neksio (prior price was lower)",
        feed=FeedConfig(
            id="neksio-ram",
            url="https://neksio.mk/memorija",
            webhook=WEBHOOK,
            strategy="neksio",
            name="Neksio RAM",
        ),
        entry=EntryData(
            title="Kingston Fury Beast 32GB (2x16GB) DDR5 6000MHz CL36",
            link="https://neksio.mk/proizvod/kingston-fury-beast-32gb-ddr5",
            description="",
            author="",
            timestamp="2026-08-11T05:20:00+00:00",
            image_url=RAM_IMAGE,
            source_metrics=(
                SourceMetric("Price", "11.450 ден"),
                SourceMetric("Previous", "9.990 ден", prior=True),
                SourceMetric("Manufacturer", "Kingston"),
                SourceMetric("Stock", "7"),
            ),
            price_direction=PriceDirection.INCREASE,
        ),
        source_title="Neksio",
    ),
    Case(
        key="hacker_news",
        caption="Hacker News story — no image, author, discussion link",
        feed=FeedConfig(
            id="hn",
            url="https://hacker-news.firebaseio.com/v0/topstories.json",
            webhook=WEBHOOK,
            strategy="rss",
            adapter="hackernews",
        ),
        entry=EntryData(
            title="Show HN: I built a search engine for my own notes",
            link="https://example.com/blog/notes-search-engine",
            description="",
            author="tptacek",
            timestamp="2026-08-11T04:32:00+00:00",
            discussion_url="https://news.ycombinator.com/item?id=41234567",
            source_metrics=(
                SourceMetric("Points", "482"),
                SourceMetric("Comments", "213"),
                SourceMetric("", "example.com"),
            ),
        ),
        source_title="Hacker News",
    ),
    Case(
        key="classified_ad",
        caption="Classified ad — Pazar3 (description, price, location)",
        feed=FeedConfig(
            id="pazar3-gpu",
            url="https://www.pazar3.mk/oglasi/kompjuteri",
            webhook=WEBHOOK,
            strategy="pazar3",
            name="Pazar3 GPUs",
        ),
        entry=EntryData(
            title="RTX 3080 10GB Gaming OC",
            link="https://www.pazar3.mk/oglas/rtx-3080-10gb/12345",
            description=(
                "Продавам RTX 3080 во одлична состојба, користена само за gaming, "
                "без mining. Гаранција до 2027, комплетно со кутија и кабли."
            ),
            author="",
            timestamp="2026-08-11T03:10:00+00:00",
            image_url=GPU_IMAGE,
            categories=("Компјутери",),
            source_metrics=(
                SourceMetric("Price", "24.000 ден"),
                SourceMetric("Location", "Скопје, Аеродром"),
            ),
        ),
        source_title="Pazar3",
    ),
    Case(
        key="plain_rss",
        caption="Plain RSS entry — long description, no image, categories",
        feed=FeedConfig(
            id="blog",
            url="https://simonwillison.net/atom/everything/",
            webhook=WEBHOOK,
            strategy="rss",
            name="Simon Willison",
        ),
        entry=EntryData(
            title="Everything I know about prompt injection, mid-2026 edition",
            link="https://simonwillison.net/2026/Aug/11/prompt-injection/",
            description=(
                "A long-overdue update on the state of prompt injection defenses. "
                "Short version: the lethal trifecta still applies, and most of the "
                "mitigations shipped this year address symptoms rather than causes."
            ),
            author="Simon Willison",
            timestamp="2026-08-10T18:05:00+00:00",
            categories=("ai", "security", "llms"),
        ),
        source_title="Simon Willison's Weblog",
    ),
)


def build_samples() -> list[Sample]:
    return [
        Sample(
            key=case.key,
            caption=case.caption,
            payload=build_components_v2_payload(case.feed, case.entry, case.source_title),
        )
        for case in CASES
    ]
