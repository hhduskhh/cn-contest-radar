"""落库：原始记录去重 + 届次幂等 upsert。

两级去重：
  1. source_items 的 (source_key, external_id) 唯一约束挡住重复条目；
     再比对 content_hash，未变则跳过全部后续解析与写库 —— 这省掉绝大部分无谓写入。
  2. event_editions 的 (event_id, year, sub_event, source_key, origin) 唯一约束
     让重复抓取变成幂等 upsert。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from ..db import get_conn
from ..models import EditionDraft, RawItem
from .events_catalog import get_catalog
from .normalize import to_utc_iso, utcnow_iso
from .status import derive_status_from_iso

log = logging.getLogger(__name__)

# 置信度只升不降。它描述的是库里那几个时间是"可靠到什么程度"来的，
# 所以必须跟时间一起合并，不能单独被一次更差的解析改写。
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _rank(confidence: str | None) -> int:
    return _CONFIDENCE_RANK.get(confidence or "", 0)


def content_hash(item: RawItem) -> str:
    """对影响展示的关键字段做哈希。raw_json 里的噪音字段（pv、点击量）不参与。"""
    material = json.dumps(
        {
            "title": item.title,
            "url": item.url,
            "text": (item.text_blob or "")[:500],
            "payload": item.payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def upsert_source_item(conn, item: RawItem) -> tuple[int, bool, bool]:
    """写入/刷新原始记录。

    返回 (row_id, is_new, changed)。changed=False 表示内容哈希未变，
    调用方可以据此跳过后续解析与写库 —— 这是增量模式省下绝大部分开销的地方。
    """
    digest = content_hash(item)
    now = utcnow_iso()
    published = to_utc_iso(item.published_at) if item.published_at else None

    existing = conn.execute(
        "SELECT id, content_hash FROM source_items WHERE source_key = ? AND external_id = ?",
        (item.source_key, item.external_id),
    ).fetchone()

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO source_items
              (source_key, external_id, title, url, published_at, content_hash,
               raw_json, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.source_key,
                item.external_id,
                item.title,
                item.url,
                published,
                digest,
                json.dumps(item.payload, ensure_ascii=False, default=str),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid), True, True

    row_id = int(existing["id"])
    if existing["content_hash"] != digest:
        conn.execute(
            """
            UPDATE source_items
               SET title = ?, url = ?, published_at = ?, content_hash = ?,
                   raw_json = ?, last_seen_at = ?
             WHERE id = ?
            """,
            (
                item.title,
                item.url,
                published,
                digest,
                json.dumps(item.payload, ensure_ascii=False, default=str),
                now,
                row_id,
            ),
        )
        return row_id, False, True

    # 内容没变：只刷新 last_seen_at，让"这个条目还在"这件事可观测
    conn.execute("UPDATE source_items SET last_seen_at = ? WHERE id = ?", (now, row_id))
    return row_id, False, False


def upsert_edition(
    conn,
    draft: EditionDraft,
    *,
    source_key: str,
    source_item_id: int | None,
    source_url: str | None,
    origin: str = "crawl",
    source_note: str | None = None,
) -> bool:
    """写入届次。返回 True 表示新增，False 表示更新。"""
    catalog = get_catalog()
    event_id = catalog.id_of(draft.event_slug) if draft.event_slug else None
    if event_id is None or draft.year is None:
        # 匹配不到赛事或推不出年份的，不写进届次表（原始记录已保留，可日后重解析）
        return False

    # 爬取来的、四个时间全空、且属于往年的记录不进时间线。
    # 往年赛事"时间待公布"是没有意义的展示；今年的空记录才有价值
    # （表示该届已启动但赛程未出）。原始记录仍然保留在 source_items 里。
    if origin == "crawl":
        has_any_time = any(
            (draft.registration_start, draft.registration_end,
             draft.contest_start, draft.contest_end)
        )
        if not has_any_time and draft.year < datetime.now(_tz()).year:
            return False

    now = utcnow_iso()
    # 状态由时间推导，供总览页徽章与「即将截止」红榜使用
    derived_status = draft.status or derive_status(draft)
    values = {
        "event_id": event_id,
        "year": draft.year,
        "sub_event": draft.sub_event or "",
        "title": draft.title or (draft.sub_event or None),
        "origin": origin,
        "source_key": source_key,
        "source_item_id": source_item_id,
        "source_url": source_url,
        "source_note": source_note,
        "registration_start": to_utc_iso(draft.registration_start) if draft.registration_start else None,
        "registration_end": to_utc_iso(draft.registration_end) if draft.registration_end else None,
        "contest_start": to_utc_iso(draft.contest_start) if draft.contest_start else None,
        "contest_end": to_utc_iso(draft.contest_end) if draft.contest_end else None,
        "registration_raw": draft.registration_raw,
        "contest_raw": draft.contest_raw,
        "time_confidence": draft.time_confidence,
        "time_precision": draft.time_precision,
        "region": draft.region,
        "status": derived_status,
        "reward": draft.reward,
        "team_info": draft.team_info,
        "extra": json.dumps(draft.extra, ensure_ascii=False, default=str),
        "updated_at": now,
    }

    existing = conn.execute(
        """
        SELECT id, registration_start, registration_end, contest_start, contest_end,
               time_confidence, time_precision, region, status, reward, team_info
          FROM event_editions
         WHERE event_id = ? AND year = ? AND sub_event = ? AND source_key = ? AND origin = ?
        """,
        (event_id, draft.year, draft.sub_event or "", source_key, origin),
    ).fetchone()

    if existing is None:
        cols = ["first_seen_at", *values.keys()]
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO event_editions ({', '.join(cols)}) VALUES ({placeholders})",
            [now, *values.values()],
        )
        return True

    # 时间字段逐个合并：新值非空才覆盖，避免一次解析失败把已有时间抹掉
    merged = dict(values)
    provided_time = False
    for field in ("registration_start", "registration_end", "contest_start", "contest_end"):
        if merged[field] is None:
            merged[field] = existing[field]
        else:
            provided_time = True

    # 置信度与精度跟着时间走，同样要合并：
    #   - 这次一条时间都没提供 → 没资格改写已有时间的置信度标注
    #   - 这次置信度更低 → 不能把已有的 high 降级
    #     （同一条届次可能先由官方赛程表写 high，后被某条通知的正则抽成 low）
    if not provided_time or _rank(merged["time_confidence"]) < _rank(existing["time_confidence"]):
        merged["time_confidence"] = existing["time_confidence"]
        merged["time_precision"] = existing["time_precision"]

    # 地域同理：online 的含义是"这次没找到举办地"，不是新信息，
    # 不该冲掉已经判出来的线下赛站（guangdong/other）。
    if merged["region"] == "online" and existing["region"] != "online":
        merged["region"] = existing["region"]

    # 人工条目的置信度不应被爬取结果拉低
    if origin == "manual":
        merged["time_confidence"] = draft.time_confidence

    for field in ("status", "reward", "team_info"):
        if merged[field] is None:
            merged[field] = existing[field]

    assignments = ", ".join(f"{col} = ?" for col in merged)
    conn.execute(
        f"UPDATE event_editions SET {assignments} WHERE id = ?",
        [*merged.values(), existing["id"]],
    )
    return False


def derive_status(draft: EditionDraft, now: datetime | None = None) -> str | None:
    """由时间推导赛事状态，入库时算一次。

    读路径不认这个值（它会冻住），另行按当前时间纠正 —— 见 status.effective_status。
    """
    if draft.status:
        return draft.status
    return derive_status_from_iso(
        registration_start=_iso(draft.registration_start),
        registration_end=_iso(draft.registration_end),
        contest_start=_iso(draft.contest_start),
        contest_end=_iso(draft.contest_end),
        now=to_utc_iso(now) if now is not None else None,
    )


def _iso(value: datetime | None) -> str | None:
    return to_utc_iso(value) if value else None


def _tz():
    from .. import config

    return config.TZ
