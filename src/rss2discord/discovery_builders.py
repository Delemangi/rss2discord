"""Compose native strategies for the configured feed providers."""

from collections.abc import Callable

from .discord.client import SleepCallback
from .providers.anhoch.strategy import AnhochStrategy
from .providers.cccenter.strategy import CCCenterStrategy
from .providers.ddstore.strategy import DDStoreStrategy
from .providers.gjirafa50.strategy import Gjirafa50Strategy
from .providers.hivetec.strategy import HivetecStrategy
from .providers.itmk_oglasnik.strategy import ITMkOglasnikStrategy
from .providers.neksio.strategy import NeksioStrategy
from .providers.neptun.strategy import NeptunStrategy
from .providers.pazar3.pacing import Pazar3RequestPacer
from .providers.pazar3.strategy import Pazar3Strategy
from .providers.reklama5.strategy import Reklama5Strategy
from .providers.setec.strategy import SetecStrategy
from .providers.technomarket.strategy import TechnomarketStrategy
from .transports import (
    RSSStrategy,
    ScraperStrategy,
    XenForoStrategy,
)


def build_discovery_strategies(
    *,
    is_shutdown_requested: Callable[[], bool],
    sleep: SleepCallback,
    pazar3_pacer: Pazar3RequestPacer,
) -> dict[str, ScraperStrategy]:
    return {
        "anhoch": AnhochStrategy(),
        "cccenter": CCCenterStrategy(is_shutdown_requested),
        "ddstore": DDStoreStrategy(is_shutdown_requested),
        "gjirafa50": Gjirafa50Strategy(is_shutdown_requested),
        "hivetec": HivetecStrategy(is_shutdown_requested),
        "itmk_oglasnik": ITMkOglasnikStrategy(),
        "neksio": NeksioStrategy(is_shutdown_requested),
        "neptun": NeptunStrategy(),
        "pazar3": Pazar3Strategy(
            pacer=pazar3_pacer,
            sleep=sleep,
            is_shutdown_requested=is_shutdown_requested,
        ),
        "reklama5": Reklama5Strategy(is_shutdown_requested=is_shutdown_requested),
        "rss": RSSStrategy(),
        "setec": SetecStrategy(),
        "technomarket": TechnomarketStrategy(is_shutdown_requested),
        "xenforo": XenForoStrategy(),
    }
