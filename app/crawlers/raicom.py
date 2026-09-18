"""睿抗机器人开发者大赛（RAICOM）。

接口：https://service.raicom.com.cn/api/matches

实测要点：
  - **只返回 1 条**（当前赛季），历年信息靠人工种子数据补
  - enrollstartdate / enrollenddate 是 **13 位毫秒时间戳**，不是秒
    （parse_any_datetime 会按位数判别，别自己写 int(x) 当秒用）
  - detail 是 HTML 富文本，赛程细节藏在里面，交给 B 类抽取器补
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline import extract
from ..pipeline.normalize import infer_year, parse_any_datetime
from ..pipeline.textutil import strip_html
from .base import BaseCrawler

_ENDPOINT = "https://service.raicom.com.cn/api/matches"


class RaicomCrawler(BaseCrawler):
    key = "raicom"
    name = "睿抗机器人开发者大赛"
    kind = "structured"
    base_url = "https://www.raicom.com.cn/"
    rate_limit_seconds = 1.5
    max_pages = 1

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        data = self.http.get_json(_ENDPOINT)
        for item in data.get("data") or []:
            yield item, item.get("matchname") or "", self.url_of(item)

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("id"))

    def url_of(self, payload: Any) -> str | None:
        match_id = payload.get("id")
        return f"https://www.raicom.com.cn/match-item.html?id={match_id}" if match_id else None

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("created"))
        return parsed.utc if parsed else None

    def text_blob_of(self, payload: Any) -> str | None:
        return strip_html(payload.get("detail"))

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        # 报名时间来自结构化字段（13 位毫秒时间戳）
        reg_start = parse_any_datetime(payload.get("enrollstartdate"))
        reg_end = parse_any_datetime(payload.get("enrollenddate"))

        body = strip_html(payload.get("detail"))
        year = infer_year(title, body, self.published_at_of(payload))
        # 比赛时间只能从 detail 正文抽取
        con_start, con_end, con_raw = extract.extract_contest(
            f"{title}\n{body}", default_year=year, published_at=self.published_at_of(payload)
        )

        reg_raw = None
        if reg_start or reg_end:
            reg_raw = f"{_fmt(reg_start)} ~ {_fmt(reg_end)}"

        return [
            EditionDraft(
                event_slug="raicom",
                year=year,
                sub_event=self._sub_event(title),
                registration_start=reg_start.utc if reg_start else None,
                registration_end=reg_end.utc if reg_end else None,
                contest_start=con_start.utc if con_start else None,
                contest_end=con_end.utc if con_end else None,
                registration_raw=reg_raw,
                contest_raw=con_raw,
                # 报名是结构化字段（high），比赛靠正文抽取（low）—— 取中
                time_confidence="medium",
                time_precision="second",
                extra={"detail_excerpt": body[:300]},
            )
        ]

    @staticmethod
    def _sub_event(title: str) -> str:
        match = re.search(r"（(\w+)）", title)
        return match.group(1) if match else ""


def _fmt(parsed) -> str:
    if not parsed:
        return "—"
    from .. import config

    return parsed.utc.astimezone(config.TZ).strftime("%Y-%m-%d %H:%M")
