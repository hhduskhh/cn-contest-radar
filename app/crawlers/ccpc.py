"""CCPC 中国大学生程序设计竞赛。

接口：https://ccpc.io/api/archive?page=N&pageSize=50  （列表）
      https://ccpc.io/api/archive/{id}               （详情）

实测要点：
  - 列表只有极少数条目带 content（实测 100 条里仅 1 条），**必须回退到详情接口**
  - 详情返回的是嵌套结构 data.archivesInfo.content —— 取错层会得到 None 而不报错
  - fullurl 直接给出公开页地址（如 https://ccpc.io/a/388.html）
  - createtime 是 Unix 秒
  - 「各场比赛安排」通知里是结构化赛程表，用 ccpc_schedule 解析成每站一条

CCPC 是**有线下举办地**的赛事，赛站要判地域（用户只看广东省内的）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline import extract, region
from ..pipeline.ccpc_schedule import parse_schedule
from ..pipeline.normalize import infer_year, parse_any_datetime, year_from_edition
from ..pipeline.textutil import clean_text, strip_html
from .base import BaseCrawler

log = logging.getLogger(__name__)

# CCPC「第 N 届」= N + 2015 年（实测：第 11 届 = 2026，第 10 届 = 2025）
_EDITION_OFFSET = 2015

_PAGE_SIZE = 50
_MAX_PAGES = 20
_DETAIL_BUDGET = 400        # 单次执行最多取多少次详情，防止失控
_RECENT_DETAIL_WINDOW = 15  # 增量模式下，最近这么多条总是重取详情（防漏更新）


class CCPCCrawler(BaseCrawler):
    key = "ccpc"
    name = "CCPC 中国大学生程序设计竞赛"
    kind = "news"
    base_url = "https://ccpc.io/"
    rate_limit_seconds = 1.0
    max_pages = _MAX_PAGES
    has_location = True

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        known = self.known_external_ids() if mode == "incremental" else set()
        items: list[dict] = []

        for page in range(1, self.max_pages + 1):
            data = self.http.get_json(
                f"{self.base_url}api/archive",
                params={"page": page, "pageSize": _PAGE_SIZE},
            )
            batch = data.get("data") or []
            if not batch:
                break
            items.extend(batch)

        detail_budget = _DETAIL_BUDGET
        for index, item in enumerate(items):
            title = item.get("title") or ""
            # 归档里混着「组委会成员」「获奖名单」这类内容，先过闸门再花请求取详情
            if not extract.is_competition_notice(title):
                continue

            is_new = str(item.get("id")) not in known
            is_recent = index < _RECENT_DETAIL_WINDOW
            if item.get("content") is None and (is_new or is_recent or mode == "backfill"):
                if detail_budget > 0:
                    detail_budget -= 1
                    content = self._fetch_detail(item.get("id"))
                    if content:
                        item = {**item, "content": content}

            yield item, title, self.url_of(item)

    def _fetch_detail(self, archive_id: Any) -> str | None:
        """取详情正文。结构是 data.archivesInfo.content，不是 data.content。"""
        if archive_id is None:
            return None
        try:
            data = self.http.get_json(f"{self.base_url}api/archive/{archive_id}")
        except Exception as exc:  # noqa: BLE001 —— 单条详情失败不该中断整个源
            log.info("CCPC 详情 %s 获取失败：%s", archive_id, exc)
            return None
        info = (data.get("data") or {}).get("archivesInfo") or {}
        return info.get("content")

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("id"))

    def title_of(self, payload: Any) -> str:
        return payload.get("title") or ""

    def url_of(self, payload: Any) -> str | None:
        return payload.get("fullurl") or None

    def published_at_of(self, payload: Any) -> datetime | None:
        # createtime 是 Unix 秒；parse_any_datetime 会按位数判别秒/毫秒
        parsed = parse_any_datetime(payload.get("createtime"))
        return parsed.utc if parsed else None

    def text_blob_of(self, payload: Any) -> str | None:
        return strip_html(payload.get("content"))

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        if not extract.is_competition_notice(title):
            return []

        body = strip_html(payload.get("content"))
        published = self.published_at_of(payload)
        # 标题常写「第 11 届…重庆站」而不写年份。按届次号换算更可靠。
        year = year_from_edition(title, _EDITION_OFFSET) or infer_year(title, body, published)
        if year is None:
            return []

        # 「各场比赛安排」是结构化赛程表，一站一条，价值最高
        if "安排" in title or "赛程" in title:
            stations = parse_schedule(body, year)
            if stations:
                return [self._draft_for_station(s, title, url, published) for s in stations]

        blob = f"{title}\n{body}"
        reg_start, reg_end, reg_raw = extract.extract_registration(
            blob, default_year=year, published_at=published
        )
        con_start, con_end, con_raw = extract.extract_contest(
            blob, default_year=year, published_at=published
        )
        sub_event = self._sub_event(title)

        return [
            EditionDraft(
                event_slug="ccpc",
                year=year,
                sub_event=sub_event,
                registration_start=reg_start.utc if reg_start else None,
                registration_end=reg_end.utc if reg_end else None,
                contest_start=con_start.utc if con_start else None,
                contest_end=con_end.utc if con_end else None,
                registration_raw=reg_raw,
                contest_raw=con_raw,
                time_confidence="low",  # 新闻正则抽取
                time_precision="date",
                region=region.classify(True, sub_event, title),
                extra={"channel": (payload.get("channel") or {}).get("name")},
            )
        ]

    def _draft_for_station(
        self, station, title: str, url: str | None, published: datetime | None
    ) -> EditionDraft:
        return EditionDraft(
            event_slug="ccpc",
            year=station.start.year,
            sub_event=station.station,
            contest_start=station.start,
            contest_end=station.end,
            contest_raw=station.raw,
            # 赛程表是官方发布的确定性信息，置信度给 high
            time_confidence="high",
            time_precision="date",
            region=region.classify(True, station.station, title),
            extra={"from_schedule": True, "notice": title},
        )

    @staticmethod
    def _sub_event(title: str) -> str:
        """从标题里提赛站名，作为子赛道区分同一届的多场比赛。"""
        if "网络赛" in title or "网络预选赛" in title:
            return "网络赛"
        match = re.search(r"([一-龥]{2,4}站)", title)
        if match:
            return match.group(1)
        if "总决赛" in title:
            return "总决赛"
        if "女生" in title:
            return "女生专场"
        if "高职" in title:
            return "高职专场"
        return ""
