from rss2discord.discovery_builders import build_discovery_strategies
from rss2discord.providers.anhoch.strategy import AnhochStrategy
from rss2discord.providers.cccenter.strategy import CCCenterStrategy
from rss2discord.providers.ddstore.strategy import DDStoreStrategy
from rss2discord.providers.gjirafa50.strategy import Gjirafa50Strategy
from rss2discord.providers.hivetec.strategy import HivetecStrategy
from rss2discord.providers.itmk_oglasnik.strategy import ITMkOglasnikStrategy
from rss2discord.providers.neksio.strategy import NeksioStrategy
from rss2discord.providers.neptun.strategy import NeptunStrategy
from rss2discord.providers.setec.strategy import SetecStrategy
from rss2discord.providers.technomarket.strategy import TechnomarketStrategy
from rss2discord.transports import (
    Pazar3Strategy,
    Reklama5Strategy,
    RSSStrategy,
    XenForoStrategy,
)
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer


def test_build_discovery_strategies_eagerly_constructs_all_native_strategies() -> None:
    def shutdown_requested() -> bool:
        return False

    def sleep(_seconds: float) -> bool:
        return True

    pacer = Pazar3RequestPacer(lambda: 0)

    strategies = build_discovery_strategies(
        is_shutdown_requested=shutdown_requested,
        sleep=sleep,
        pazar3_pacer=pacer,
    )

    assert {key: type(strategy) for key, strategy in strategies.items()} == {
        "anhoch": AnhochStrategy,
        "cccenter": CCCenterStrategy,
        "ddstore": DDStoreStrategy,
        "gjirafa50": Gjirafa50Strategy,
        "hivetec": HivetecStrategy,
        "itmk_oglasnik": ITMkOglasnikStrategy,
        "neksio": NeksioStrategy,
        "neptun": NeptunStrategy,
        "pazar3": Pazar3Strategy,
        "reklama5": Reklama5Strategy,
        "rss": RSSStrategy,
        "setec": SetecStrategy,
        "technomarket": TechnomarketStrategy,
        "xenforo": XenForoStrategy,
    }
    assert len(strategies) == 14
    pazar3 = strategies["pazar3"]
    assert isinstance(pazar3, Pazar3Strategy)
    assert pazar3._pacer is pacer
    assert pazar3._sleep is sleep
    assert pazar3._is_shutdown_requested is shutdown_requested
