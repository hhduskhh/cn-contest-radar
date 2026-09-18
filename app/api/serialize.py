"""对外 JSON 序列化。

时间默认输出 Asia/Shanghai 的日期（用户看的是本地时间），同时保留 *_utc 原值
和 *_raw 原文 —— 用户能自己核对口径，出问题时也便于定位。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .. import config


def local_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return dt.astimezone(config.TZ).strftime("%Y-%m-%d")


def local_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return dt.astimezone(config.TZ).strftime("%Y-%m-%d %H:%M")


def days_until(value: str | None) -> int | None:
    """距今天还有几天。负数表示已过去。用于「即将截止」红榜排序。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    delta = dt - datetime.now(timezone.utc)
    return delta.days


def _is_long_running(start: str | None, end: str | None) -> bool:
    """区间跨度超过 3 年的一律视为「长期开放」。

    竞赛的报名/比赛窗口不可能有几年长。源数据里出现这种值，要么是哨兵
    （DataFountain 的 2099-12-31），要么是录入错误（和鲸某个训练营写到 2044）。
    有 142 条属于这类，占 5%。"""
    if not start or not end:
        return False
    try:
        a = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return False
    return (b - a).days > 365 * 3


def _json_field(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


# 参赛方式。用户的判断标准是「能不能在广东参加」，而不是「比赛地在不在广东」——
# CCPC 分站赛决赛在哈尔滨，但初赛（网络赛）是线上的，广东的学生照样能参加，
# 所以它属于「需出省」而不是「不能参加」，默认不被过滤掉。
_ACCESS = {
    "online": ("online", "线上参加"),
    "guangdong": ("guangdong", "广东线下"),
    "other": ("travel", "需出省"),
}


def edition_to_dict(row: Any) -> dict:
    """把 event_editions 的一行转成前端用的结构。"""
    record = dict(row)
    return {
        "id": record.get("id"),
        "event_id": record.get("event_id"),
        "event_slug": record.get("event_slug"),
        "event_name": record.get("event_name"),
        "category": record.get("category"),
        "official_url": record.get("official_url"),
        "year": record.get("year"),
        "sub_event": record.get("sub_event") or "",
        "title": record.get("title"),
        "origin": record.get("origin"),
        "source_key": record.get("source_key"),
        "source_url": record.get("source_url"),
        "source_note": record.get("source_note"),
        "status": record.get("status"),
        "time_confidence": record.get("time_confidence"),
        "region": record.get("region") or "online",
        "access": _ACCESS.get(record.get("region") or "online", _ACCESS["online"])[0],
        "access_label": _ACCESS.get(record.get("region") or "online", _ACCESS["online"])[1],
        "reward": record.get("reward"),
        "team_info": record.get("team_info"),
        "extra": _json_field(record.get("extra")),
        # 本地化的展示值
        "registration_start": local_date(record.get("registration_start")),
        "registration_end": local_date(record.get("registration_end")),
        "contest_start": local_date(record.get("contest_start")),
        "contest_end": local_date(record.get("contest_end")),
        # 原始 UTC 值，供前端做精确排序
        "registration_start_utc": record.get("registration_start"),
        "registration_end_utc": record.get("registration_end"),
        "contest_start_utc": record.get("contest_start"),
        "contest_end_utc": record.get("contest_end"),
        # 原文，悬停时展示，便于人工核对
        "registration_raw": record.get("registration_raw"),
        "contest_raw": record.get("contest_raw"),
        "days_until_deadline": days_until(record.get("registration_end")),
        "updated_at": record.get("updated_at"),
        # 有些平台用 2099-12-31 之类的哨兵值表示「长期开放」。数据照实保留，
        # 但前端要标出来，否则用户会以为真有比赛办到 2099 年。
        "is_long_running": _is_long_running(
            record.get("contest_start"), record.get("contest_end")
        ),
    }
