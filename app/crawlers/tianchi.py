"""天池大数据竞赛（阿里）。

接口：https://tianchi.aliyun.com/competition/proxy/api/competition/api/race/listBrief

实测要点（三个坑，全部已验证）：
  1. **必须带 /competition/proxy/api/ 前缀**，否则被阿里云 CSRF 网关 302 重定向回首页
  2. **pageSize 参数被忽略**，固定每页 10 条，只能靠 pageNum 递增翻页（共 58 页）
  3. **season 字段是赛季序号（实测值 = 1），不是年份** —— 年份要从 currentSeasonStart 推
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline.normalize import parse_any_datetime
from .base import BaseCrawler

_ENDPOINT = "https://tianchi.aliyun.com/competition/proxy/api/competition/api/race/listBrief"
_PAGE_SIZE = 10          # 服务端固定值，传别的也没用
_MAX_PAGES = 60          # 576 条 / 10 = 58 页
_IDLE_PAGE_LIMIT = 3     # 增量模式下连续这么多页全是已知条目就停


class TianchiCrawler(BaseCrawler):
    key = "tianchi"
    name = "天池大数据竞赛"
    kind = "structured"
    base_url = "https://tianchi.aliyun.com/competition/gameList/activeList"
    rate_limit_seconds = 1.5
    max_pages = _MAX_PAGES

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        known = self.known_external_ids() if mode == "incremental" else set()
        idle_pages = 0

        for page in range(1, self.max_pages + 1):
            data = self.http.get_json(
                _ENDPOINT,
                params={"pageNum": page, "pageSize": _PAGE_SIZE, "type": 2},
            )
            items = (data.get("data") or {}).get("list") or []
            if not items:
                break

            fresh_on_page = 0
            for item in items:
                if self.external_id_of(item) not in known:
                    fresh_on_page += 1
                yield item, item.get("raceName") or "", self.url_of(item)

            if mode == "incremental":
                idle_pages = idle_pages + 1 if fresh_on_page == 0 else 0
                if idle_pages >= _IDLE_PAGE_LIMIT:
                    break

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("raceId"))

    def url_of(self, payload: Any) -> str | None:
        race_id = payload.get("raceId")
        if not race_id:
            return payload.get("website") or None
        return f"https://tianchi.aliyun.com/competition/entrance/{race_id}/introduction"

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("currentSeasonStart"))
        return parsed.utc if parsed else None

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        start = parse_any_datetime(payload.get("currentSeasonStart"))
        end = parse_any_datetime(payload.get("currentSeasonEnd"))
        # 天池是"赛季"制，没有独立的报名阶段：赛季区间即参赛区间
        if start is None and end is None:
            return []

        year = start.utc.astimezone(_tz()).year if start else None
        state = (payload.get("raceState") or "").upper()
        status = {
            "ONGOING": "running",
            "FINISHED": "finished",
            "NOT_START": "upcoming",
            "UPCOMING": "upcoming",
        }.get(state)

        return [
            EditionDraft(
                event_slug="tianchi",
                year=year,
                # 平台型：用比赛名做子赛道，否则同一年上百场比赛会被唯一键折叠成一行
                sub_event=title,
                title=title,
                contest_start=start.utc if start else None,
                contest_end=end.utc if end else None,
                contest_raw=f"{payload.get('currentSeasonStart')} ~ {payload.get('currentSeasonEnd')}",
                time_confidence="high",   # 结构化 API 字段
                time_precision="second",
                status=status,
                reward=str(payload.get("bonus")) if payload.get("bonus") else None,
                team_info=f"{payload.get('teamNum')} 支队伍" if payload.get("teamNum") else None,
                extra={
                    "track": self._track(payload),
                    "raceType": payload.get("raceType"),
                    "visualTab": payload.get("visualTab"),
                    "needStudent": payload.get("needStudent"),
                    "brief": (payload.get("brief") or "")[:300],
                },
            )
        ]

    @staticmethod
    def _track(payload: Any) -> str:
        tab = payload.get("visualTab")
        return {"AILLM": "AI 大模型赛道"}.get(tab, "")


def _tz():
    from .. import config

    return config.TZ
