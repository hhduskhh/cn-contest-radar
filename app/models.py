"""爬取管道内部流转的数据结构。

A 类（结构化接口）与 B 类（新闻列表）在 fetch 层产出同样的 RawItem，
在 derive 层产出同样的 EditionDraft —— 差异被收敛进 time_confidence，
所以 persist 层完全不需要知道数据来自结构化字段还是正则抽取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Kind = Literal["structured", "news"]
Confidence = Literal["high", "medium", "low"]


@dataclass
class ParsedTime:
    """归一化后的时间。raw 保留原始文本，便于人工核对口径。"""

    utc: datetime
    raw: str
    precision: Literal["date", "second"] = "date"


@dataclass
class RawItem:
    """一条原始抓取记录。"""

    source_key: str
    external_id: str
    title: str
    url: str | None = None
    published_at: datetime | None = None  # 已归一化为 UTC
    payload: Any = None                   # 原始记录（dict/list）
    text_blob: str | None = None          # B 类：正文/摘要纯文本，供关键词抽取


@dataclass
class EditionDraft:
    """一条待落库的赛事届次。

    B 类源抽不到时间时仍然产出 draft，四个时间字段为 None。
    价值在于把「该届已启动，时间待公布」这个信号留在库里 —— 这正是备赛用户需要知道的。
    """

    event_slug: str | None
    year: int | None
    sub_event: str = ""
    title: str | None = None
    registration_start: datetime | None = None
    registration_end: datetime | None = None
    contest_start: datetime | None = None
    contest_end: datetime | None = None
    registration_raw: str | None = None
    contest_raw: str | None = None
    time_confidence: Confidence = "low"
    time_precision: Literal["date", "second"] = "date"
    # guangdong / other / online。线上赛事没有举办地，不会被地域筛选影响。
    region: str = "online"
    status: str | None = None
    reward: str | None = None
    team_info: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class CrawlStats:
    """单源一次执行的统计，写入 crawl_run_items。"""

    source_key: str
    items_found: int = 0
    items_new: int = 0
    items_updated: int = 0
    editions_written: int = 0
    status: str = "success"
    error: str | None = None
    duration_ms: int = 0
