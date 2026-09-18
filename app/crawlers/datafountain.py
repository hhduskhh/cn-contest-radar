"""DataFountain 数据竞技平台（含 CCF BDCI）。

站点是 Nuxt SSR，赛事数据内嵌在 HTML 的 window.__NUXT__ 里，不需要浏览器。
解析靠 app/nuxt.py 的 IIFE 回填器。

实测要点：
  - 分页参数是 ?page=N，每页 30 条，共约 370 条 / 13 页
  - **时间是 UTC 'Z' 结尾**（'2026-04-06T16:00:00.000Z' 实为北京时间 4/7 00:00）。
    同一份 HTML 里 race.startTime 却是 '+08:00'，两者混用会整体偏一天，
    所以这里只取 competitions[] 自己的 startTime/endTime，统一按 UTC 解释。
  - isOpenSignup 是 1/0，可直接判断报名是否开放
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..nuxt import parse_nuxt_iife
from ..pipeline.normalize import parse_any_datetime
from .base import BaseCrawler

_ENDPOINT = "https://www.datafountain.cn/competitions"
_MAX_PAGES = 15
_IDLE_PAGE_LIMIT = 3


class DataFountainCrawler(BaseCrawler):
    key = "datafountain"
    name = "DataFountain / CCF BDCI"
    kind = "structured"
    base_url = _ENDPOINT
    rate_limit_seconds = 2.0
    max_pages = _MAX_PAGES

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        known = self.known_external_ids() if mode == "incremental" else set()
        idle_pages = 0

        for page in range(1, self.max_pages + 1):
            html = self.http.get_text(_ENDPOINT, params={"page": page})
            try:
                payload = parse_nuxt_iife(html)
                block = payload["data"][0]
                items = block.get("competitions") or []
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                # Nuxt 结构变了就报错，让该源标红，而不是静默返回空
                raise RuntimeError(f"DataFountain 页面结构解析失败（可能已改版）: {exc}") from exc

            if not items:
                break

            fresh_on_page = 0
            for item in items:
                if self.external_id_of(item) not in known:
                    fresh_on_page += 1
                yield item, item.get("title") or "", self.url_of(item)

            if mode == "incremental":
                idle_pages = idle_pages + 1 if fresh_on_page == 0 else 0
                if idle_pages >= _IDLE_PAGE_LIMIT:
                    break

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("id"))

    def url_of(self, payload: Any) -> str | None:
        comp_id = payload.get("id")
        return f"{_ENDPOINT}/{comp_id}" if comp_id else None

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("startTime"))
        return parsed.utc if parsed else None

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        start = parse_any_datetime(payload.get("startTime"))
        end = parse_any_datetime(payload.get("endTime"))
        if start is None and end is None:
            return []

        year = start.utc.astimezone(_tz()).year if start else None

        organizers = payload.get("organizers") or []
        organizer_names = "、".join(
            o.get("name", "") for o in organizers if isinstance(o, dict) and o.get("name")
        )

        # isOpenSignup=1 且比赛尚未结束时，视为正在报名
        open_signup = bool(payload.get("isOpenSignup"))
        status = "registering" if open_signup else None

        return [
            EditionDraft(
                event_slug="datafountain",
                year=year,
                # 平台型：用比赛名做子赛道，否则同一年上百场比赛会被唯一键折叠成一行
                sub_event=title,
                title=title,
                contest_start=start.utc if start else None,
                contest_end=end.utc if end else None,
                contest_raw=f"{payload.get('startTime')} ~ {payload.get('endTime')}",
                # 时间来自 Nuxt payload 的结构化字段，但站点本身是 SSR 页面
                time_confidence="high",
                time_precision="second",
                status=status,
                reward=payload.get("reward") or None,
                team_info=(
                    f"{payload.get('teams')} 支队伍" if payload.get("teams") else None
                ),
                extra={
                    "organizers": organizer_names[:200],
                    "tags": [
                        t.get("nameCn") for t in (payload.get("tags") or [])
                        if isinstance(t, dict) and t.get("nameCn")
                    ],
                    "maxTeamSize": payload.get("maxTeamSize"),
                    "isOpenSignup": open_signup,
                },
            )
        ]


def _tz():
    from .. import config

    return config.TZ
