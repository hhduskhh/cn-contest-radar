"""讯飞 AI 开发者大赛。

接口：https://challenge.xfyun.cn/2020/ai-contest/api/contests/contests-list

**无需登录**，一次返回全部 667 条赛事（响应约 1MB），且带完整的分阶段时间：
报名开始/截止、初赛开始/结束、复赛结束、决赛开始/结束 —— 这是所有数据源里
时间信息最全的一个，直接覆盖 2019–2026 每年约 100 条赛事。

踩过的坑：
  1. **路径必须有 `/contests/` 这一段**。少了它（/api/contests-list）会返回
     {"flag":false,"code":10008,"desc":"请求方法或类型不支持"}，看起来像是接口废弃了，
     实际只是路径不对。这个误区导致最初误判讯飞需要登录。
  2. **`contest_name` 其实是数字 ID**（如 "822"），真正的名称在 `name_basic_problem`。
  3. **`1999-09-09` 是哨兵值**，表示「该阶段不存在」：519 条用它表示无复赛，
     16 条表示决赛未定。不过滤的话时间线上会凭空多出 1999 年的赛事。
  4. 加 `?typeBasicProblem=...&curPage=...` 会走分页，返回结构变成
     {totalPages, content, totalElements}；不加参数才是一次性全量。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft, ParsedTime
from ..pipeline.normalize import parse_any_datetime
from .base import BaseCrawler

log = logging.getLogger(__name__)

_ENDPOINT = "https://challenge.xfyun.cn/2020/ai-contest/api/contests/contests-list"

# 「该阶段不存在」的哨兵值。讯飞用它填充不存在的赛段，必须当作空值处理。
_SENTINEL_PREFIX = "1999-09-09"

# 赛道代码 → 中文名
_TRACKS = {
    "algorithem": "算法赛",
    "application": "应用赛",
    "train": "AI 实践",
    "public": "公开赛",
    "school": "高校赛",
    "children": "青少年赛",
    "xinghuo": "星火杯",
}


class XfyunCrawler(BaseCrawler):
    key = "xfyun"
    name = "讯飞 AI 开发者大赛"
    kind = "structured"
    base_url = "https://challenge.xfyun.cn/"
    rate_limit_seconds = 2.0
    max_pages = 1

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        # 一次性拉全量。不加任何查询参数 —— 加了会切成分页结构。
        data = self.http.get_json(_ENDPOINT)
        items = data.get("data") or []
        if not isinstance(items, list):
            raise RuntimeError(f"讯飞接口返回结构异常：{str(data)[:200]}")

        for item in items:
            yield item, self.title_of(item), self.url_of(item)

    def external_id_of(self, payload: Any) -> str:
        # contest_name 实际存的是数字 ID
        for key in ("contest_name", "contest_flag"):
            if payload.get(key):
                return str(payload[key])
        return str(payload.get("name_basic_problem") or "")

    def title_of(self, payload: Any) -> str:
        return payload.get("name_basic_problem") or ""

    def url_of(self, payload: Any) -> str | None:
        slug = payload.get("contest_flag")
        if slug:
            return f"https://challenge.xfyun.cn/topic/info?type={slug}"
        return payload.get("external_link") or self.base_url

    def published_at_of(self, payload: Any) -> datetime | None:
        for key in ("publish_time", "update_time"):
            parsed = parse_any_datetime(payload.get(key))
            if parsed:
                return parsed.utc
        return None

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        reg_start = self._date(payload, "registerBegin_basic_problem")
        reg_end = self._date(payload, "registerEnd_basic_problem")
        prelim_start = self._date(payload, "prelimBegin_basic_problem")
        prelim_end = self._date(payload, "prelimEnd_basic_problem")
        semi_end = self._date(payload, "semiFinalEnd_basic_problem")
        final_start = self._date(payload, "finalBegin_basic_problem")
        final_end = self._date(payload, "finalEnd_basic_problem")

        if reg_end is None and prelim_end is None and final_end is None:
            return []

        # 比赛区间 = 初赛开始 → 决赛结束（没有决赛就用初赛结束兜底）
        contest_start = prelim_start or final_start
        contest_end = final_end or semi_end or prelim_end
        # 注意比较的是 .utc 而不是 ParsedTime 本身 —— 它没有定义排序
        if (
            contest_start and contest_end
            and contest_end.utc < contest_start.utc
        ):
            contest_end = prelim_end or contest_start

        year = None
        for parsed in (reg_start, prelim_start, final_start, reg_end):
            if parsed:
                year = parsed.utc.astimezone(_tz()).year
                break
        if year is None:
            return []

        return [
            EditionDraft(
                event_slug="xfyun",
                year=year,
                # 平台型赛事：用比赛名做子赛道，否则同一年上百场比赛会被唯一键折叠成一行
                sub_event=title,
                title=title,
                registration_start=reg_start.utc if reg_start else None,
                registration_end=reg_end.utc if reg_end else None,
                contest_start=contest_start.utc if contest_start else None,
                contest_end=contest_end.utc if contest_end else None,
                registration_raw=_fmt_range(reg_start, reg_end),
                contest_raw=_fmt_stages(prelim_start, prelim_end, semi_end,
                                        final_start, final_end),
                # 全部来自接口的结构化日期字段，且逐条校验过顺序
                time_confidence="high",
                time_precision="date",
                reward=payload.get("bonus_basic_problem") or None,
                team_info=(
                    f"{payload.get('team_count')} 支队伍"
                    if payload.get("team_count")
                    else None
                ),
                extra={
                    "track": _TRACKS.get(payload.get("type_basic_problem") or "", ""),
                    "sponsor": payload.get("sponsorName_basic_problem"),
                    "industry": payload.get("industry"),
                    "status": payload.get("status"),
                    "desc": (payload.get("desc_basic_problem") or "")[:300],
                },
            )
        ]

    @staticmethod
    def _date(payload: Any, key: str) -> ParsedTime | None:
        """取日期，并把 1999-09-09 这个哨兵值当作空。"""
        raw = payload.get(key)
        if not raw or str(raw).startswith(_SENTINEL_PREFIX):
            return None
        return parse_any_datetime(raw)


def _d(parsed: ParsedTime | None) -> str | None:
    if not parsed:
        return None
    from .. import config

    return parsed.utc.astimezone(config.TZ).strftime("%Y-%m-%d")


def _fmt_range(start: ParsedTime | None, end: ParsedTime | None) -> str | None:
    a, b = _d(start), _d(end)
    if not a and not b:
        return None
    return f"报名 {a or '—'} ~ {b or '—'}"


def _fmt_stages(prelim_start, prelim_end, semi_end, final_start, final_end) -> str | None:
    parts = []
    if prelim_start or prelim_end:
        parts.append(f"初赛 {_d(prelim_start) or '—'} ~ {_d(prelim_end) or '—'}")
    if semi_end:
        parts.append(f"复赛结束 {_d(semi_end)}")
    if final_start or final_end:
        parts.append(f"决赛 {_d(final_start) or '—'} ~ {_d(final_end) or '—'}")
    return "；".join(parts) or None


def _tz():
    from .. import config

    return config.TZ
