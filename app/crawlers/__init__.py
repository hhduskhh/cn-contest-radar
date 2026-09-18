"""爬虫注册表。调度器与手动触发都通过 REGISTRY 枚举数据源。"""

from __future__ import annotations

from .base import BaseCrawler

REGISTRY: dict[str, BaseCrawler] = {}
_registered = False


def ensure_registered() -> dict[str, BaseCrawler]:
    """惰性注册。延迟导入避免模块级循环依赖。"""
    global _registered
    if _registered:
        return REGISTRY

    from .aistudio import AIStudioCrawler
    from .ccpc import CCPCCrawler
    from .datafountain import DataFountainCrawler
    from .heywhale import HeywhaleCrawler
    from .icpc import ICPCCrawler
    from .lanqiao import LanqiaoCrawler
    from .raicom import RaicomCrawler
    from .tianchi import TianchiCrawler
    from .xfyun import XfyunCrawler

    for cls in (
        LanqiaoCrawler,
        CCPCCrawler,
        ICPCCrawler,
        TianchiCrawler,
        AIStudioCrawler,
        DataFountainCrawler,
        HeywhaleCrawler,
        RaicomCrawler,
        XfyunCrawler,
    ):
        REGISTRY[cls.key] = cls()

    _registered = True
    return REGISTRY


def get_crawler(key: str) -> BaseCrawler | None:
    ensure_registered()
    return REGISTRY.get(key)


__all__ = ["REGISTRY", "BaseCrawler", "ensure_registered", "get_crawler"]
