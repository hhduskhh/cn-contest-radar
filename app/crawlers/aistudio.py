"""百度飞桨 AI Studio 竞赛。

接口：https://aistudio.baidu.com/studio/match/list?p=N

实测要点：
  - **分页参数是 p，不是 page / pageNum**（传错了它会一直返回第一页，且不报错）
  - 列表在 result.data（不是 result.matchList）
  - 共 640 条 / 64 页，每页 10 条
  - processList[] 是关键：含每个阶段的时间与 signupDeadline（报名截止）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline.normalize import parse_any_datetime
from .base import BaseCrawler

_ENDPOINT = "https://aistudio.baidu.com/studio/match/list"
_MAX_PAGES = 66
_IDLE_PAGE_LIMIT = 3


class AIStudioCrawler(BaseCrawler):
    key = "aistudio"
    name = "百度飞桨 AI Studio 竞赛"
    kind = "structured"
    base_url = "https://aistudio.baidu.com/competition"
    rate_limit_seconds = 1.5
    max_pages = _MAX_PAGES

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        known = self.known_external_ids() if mode == "incremental" else set()
        idle_pages = 0

        for page in range(1, self.max_pages + 1):
            data = self.http.get_json(_ENDPOINT, params={"p": page})
            result = data.get("result") or {}
            items = result.get("data") or []
            if not items:
                break

            fresh_on_page = 0
            for item in items:
                if self.external_id_of(item) not in known:
                    fresh_on_page += 1
                yield item, item.get("matchName") or "", self.url_of(item)

            if page >= int(result.get("totalPage") or self.max_pages):
                break
            if mode == "incremental":
                idle_pages = idle_pages + 1 if fresh_on_page == 0 else 0
                if idle_pages >= _IDLE_PAGE_LIMIT:
                    break

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("id"))

    def url_of(self, payload: Any) -> str | None:
        match_id = payload.get("id")
        if not match_id:
            return None
        return f"https://aistudio.baidu.com/competition/detail/{match_id}/0/introduction"

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("startTime"))
        return parsed.utc if parsed else None

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        processes = payload.get("processList") or []

        # 报名截止取首个阶段的 signupDeadline —— 这是最精确的报名时点
        reg_end = None
        reg_raw = None
        for proc in processes:
            deadline = parse_any_datetime(proc.get("signupDeadline"))
            if deadline:
                reg_end = deadline.utc
                reg_raw = f"报名截止 {proc.get('signupDeadline')}"
                break

        # 比赛区间：优先用 processList 的首尾（精确到秒），退回列表页的日期
        start = end = None
        if processes:
            first = parse_any_datetime(processes[0].get("startTime"))
            last = parse_any_datetime(processes[-1].get("endTime"))
            start = first.utc if first else None
            end = last.utc if last else None
        if start is None:
            parsed = parse_any_datetime(payload.get("startTime"))
            start = parsed.utc if parsed else None
        if end is None:
            parsed = parse_any_datetime(payload.get("endTime"))
            end = parsed.utc if parsed else None

        if start is None and end is None and reg_end is None:
            return []

        year = start.astimezone(_tz()).year if start else None
        process_text = payload.get("processText") or ""
        status = {
            "进行中": "running",
            "已结束": "finished",
            "未开始": "upcoming",
            "报名中": "registering",
        }.get(process_text)

        return [
            EditionDraft(
                event_slug="aistudio",
                year=year,
                # 平台型：用比赛名做子赛道，否则同一年上百场比赛会被唯一键折叠成一行
                sub_event=title,
                title=title,
                registration_end=reg_end,
                registration_raw=reg_raw,
                contest_start=start,
                contest_end=end,
                contest_raw=f"{payload.get('startTime')} ~ {payload.get('endTime')}",
                time_confidence="high",
                time_precision="second",
                status=status,
                reward=payload.get("reward") or None,
                team_info=(
                    f"{payload.get('signupCount')} 人参与"
                    if payload.get("signupCount")
                    else None
                ),
                extra={
                    "processText": process_text,
                    "tags": payload.get("tags"),
                    "teamUserLimit": payload.get("teamUserLimit"),
                    "abstract": (payload.get("matchAbs") or "")[:300],
                },
            )
        ]


def _tz():
    from .. import config

    return config.TZ
