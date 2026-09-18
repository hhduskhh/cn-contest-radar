"""赛事目录：把标题匹配到 event_slug。

匹配规则：别名逐个子串匹配，多命中取**最长别名** —— 否则「蓝桥杯」会被
「蓝桥」抢先匹配，进而把「蓝桥HarmonyOS培训」这类非赛事通知也归进去。
"""

from __future__ import annotations

import json
import logging

from ..db import get_conn

log = logging.getLogger(__name__)


class EventsCatalog:
    """从 DB 加载赛事别名表并缓存。种子导入后调用 reload()。"""

    def __init__(self) -> None:
        self._by_slug: dict[str, dict] = {}
        self._alias_index: list[tuple[str, str]] = []  # (alias, slug)，按别名长度降序
        self.reload()

    def reload(self) -> None:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM events WHERE active = 1").fetchall()

        self._by_slug = {}
        alias_pairs: list[tuple[str, str]] = []
        for row in rows:
            record = dict(row)
            try:
                aliases = json.loads(record.get("aliases") or "[]")
            except json.JSONDecodeError:
                aliases = []
            record["aliases"] = aliases
            self._by_slug[record["slug"]] = record
            for alias in aliases:
                if alias:
                    alias_pairs.append((alias, record["slug"]))

        # 长别名优先，保证「蓝桥杯」比「蓝桥」先被匹配到
        alias_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        self._alias_index = alias_pairs
        log.debug("赛事目录已加载：%d 个赛事，%d 条别名", len(self._by_slug), len(alias_pairs))

    def match(self, text: str | None) -> str | None:
        """返回匹配到的 slug，匹配不到返回 None。"""
        if not text:
            return None
        for alias, slug in self._alias_index:
            if alias in text:
                return slug
        return None

    def get(self, slug: str) -> dict | None:
        return self._by_slug.get(slug)

    @property
    def all_events(self) -> list[dict]:
        return list(self._by_slug.values())

    def id_of(self, slug: str) -> int | None:
        record = self._by_slug.get(slug)
        return record["id"] if record else None


_catalog: EventsCatalog | None = None


def get_catalog() -> EventsCatalog:
    global _catalog
    if _catalog is None:
        _catalog = EventsCatalog()
    return _catalog


def reload_catalog() -> EventsCatalog:
    global _catalog
    _catalog = EventsCatalog()
    return _catalog
