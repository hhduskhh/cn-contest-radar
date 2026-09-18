"""和鲸 Heywhale 数据科学竞赛。

接口：https://www.heywhale.com/v2/api/competitions?page=N

实测要点：
  - **分页只接受 page 参数**，传 pageSize/size/limit/pageNum/offset 一律 400
    {"message":"未知字段: xxx"}
  - 共 528 条，每页 10 条
  - StartDate / EndDate 是 ISO8601 UTC（'...T16:00:00.000Z'），需转北京时间展示
  - 页面加载了阿里云验证码，但**列表 API 完全公开**，验证码只挂在登录/注册上

该平台承办「中国高校计算机大赛-大数据挑战赛」，是覆盖该赛事的主要来源。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline.normalize import parse_any_datetime
from .base import BaseCrawler

_ENDPOINT = "https://www.heywhale.com/v2/api/competitions"
_MAX_PAGES = 55
_IDLE_PAGE_LIMIT = 3


class HeywhaleCrawler(BaseCrawler):
    key = "heywhale"
    name = "和鲸 Heywhale 竞赛"
    kind = "structured"
    base_url = "https://www.heywhale.com/home/competition"
    rate_limit_seconds = 1.5
    max_pages = _MAX_PAGES

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        known = self.known_external_ids() if mode == "incremental" else set()
        idle_pages = 0

        for page in range(1, self.max_pages + 1):
            data = self.http.get_json(_ENDPOINT, params={"page": page})
            items = data.get("data") or []
            if not items:
                break

            fresh_on_page = 0
            for item in items:
                if self.external_id_of(item) not in known:
                    fresh_on_page += 1
                yield item, item.get("Name") or "", self.url_of(item)

            if mode == "incremental":
                idle_pages = idle_pages + 1 if fresh_on_page == 0 else 0
                if idle_pages >= _IDLE_PAGE_LIMIT:
                    break

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("id") or payload.get("_id"))

    def url_of(self, payload: Any) -> str | None:
        comp_id = payload.get("id") or payload.get("_id")
        return f"https://www.heywhale.com/home/competition/{comp_id}" if comp_id else None

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("StartDate"))
        return parsed.utc if parsed else None

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        start = parse_any_datetime(payload.get("StartDate"))
        end = parse_any_datetime(payload.get("EndDate"))
        if start is None and end is None:
            return []

        year = start.utc.astimezone(_tz()).year if start else None
        # DetailType: LIVE=线上赛；Status: 1 通常表示进行中
        status = "running" if payload.get("Status") == 1 else None

        return [
            EditionDraft(
                event_slug="heywhale",
                year=year,
                # 平台型：用比赛名做子赛道，否则同一年上百场比赛会被唯一键折叠成一行
                sub_event=title,
                title=title,
                contest_start=start.utc if start else None,
                contest_end=end.utc if end else None,
                contest_raw=f"{payload.get('StartDate')} ~ {payload.get('EndDate')}",
                time_confidence="high",
                time_precision="second",
                status=status,
                reward=payload.get("Award") or None,
                team_info=(
                    f"{payload.get('TeamsNumber')} 支队伍"
                    if payload.get("TeamsNumber")
                    else None
                ),
                extra={
                    "detailType": payload.get("DetailType"),
                    "abstract": (payload.get("ShortDescription") or "")[:300],
                    "users": payload.get("UsersNumber"),
                },
            )
        ]


def _tz():
    from .. import config

    return config.TZ
