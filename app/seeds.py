"""种子数据导入：赛事目录 + 人工整理的历年届次。

人工条目以 origin='manual' 落库，与爬取结果**共存而非覆盖**。
读取时由 v_edition_best 视图仲裁（manual 优先），所以人工修正永远不会
被下一次爬取冲掉。
"""

from __future__ import annotations

import json
import logging

from . import config
from .db import get_conn
from .models import EditionDraft
from .pipeline.normalize import parse_any_datetime, utcnow_iso
from .pipeline.persist import upsert_edition

log = logging.getLogger(__name__)

EVENTS_FILE = config.SEED_DIR / "events.json"
EDITIONS_FILE = config.SEED_DIR / "editions.json"


def load_events() -> int:
    if not EVENTS_FILE.exists():
        log.warning("赛事目录种子不存在：%s", EVENTS_FILE)
        return 0
    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    with get_conn() as conn:
        for event in events:
            conn.execute(
                """
                INSERT INTO events (slug, name, category, organizer, official_url,
                                    description, aliases, series, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (slug) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    organizer = excluded.organizer,
                    official_url = excluded.official_url,
                    description = excluded.description,
                    aliases = excluded.aliases,
                    series = excluded.series
                """,
                (
                    event["slug"],
                    event["name"],
                    event["category"],
                    event.get("organizer"),
                    event.get("official_url"),
                    event.get("description"),
                    json.dumps(event.get("aliases", []), ensure_ascii=False),
                    1 if event.get("series", True) else 0,
                ),
            )
    log.info("已导入 %d 个赛事目录", len(events))
    return len(events)


def load_editions() -> int:
    """导入人工整理的历年数据。

    每条必须带 source_url 与 source_note —— 用户需要知道哪些数字是官网权威的、
    哪些只是参考性的。没有来源的条目会在导入时被拒绝并告警。
    """
    if not EDITIONS_FILE.exists():
        log.info("未找到历年种子文件，跳过：%s", EDITIONS_FILE)
        return 0

    editions = json.loads(EDITIONS_FILE.read_text(encoding="utf-8"))
    imported = 0
    skipped = 0

    with get_conn() as conn:
        for entry in editions:
            slug = entry.get("event_slug")
            source_url = entry.get("source_url")
            if not slug or entry.get("year") is None:
                log.warning("历年种子缺少 event_slug/year，已跳过：%s", entry)
                skipped += 1
                continue
            if not source_url:
                # 宁可少一条数据，也不要出现无法追溯来源的日期
                log.warning("历年种子 %s %s 缺少 source_url，已跳过", slug, entry.get("year"))
                skipped += 1
                continue

            row = conn.execute("SELECT id FROM events WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                log.warning("历年种子引用了未知赛事 %s，已跳过", slug)
                skipped += 1
                continue
            event_id = int(row["id"])

            def _dt(value):
                parsed = parse_any_datetime(value)
                return parsed.utc if parsed else None

            reg_start = _dt(entry.get("registration_start"))
            reg_end = _dt(entry.get("registration_end"))
            con_start = _dt(entry.get("contest_start"))
            con_end = _dt(entry.get("contest_end"))

            draft = EditionDraft(
                event_slug=slug,
                year=int(entry["year"]),
                sub_event=entry.get("sub_event", ""),
                registration_start=reg_start,
                registration_end=reg_end,
                contest_start=con_start,
                contest_end=con_end,
                registration_raw=entry.get("registration_raw"),
                contest_raw=entry.get("contest_raw"),
                time_confidence=entry.get("time_confidence", "medium"),
                time_precision=entry.get("time_precision", "date"),
                reward=entry.get("reward"),
                team_info=entry.get("team_info"),
                extra={"manual": True},
            )

            # 人工条目走独立 upsert：source_key 固定为 'seed'，与爬取行不冲突
            now = utcnow_iso()
            existing = conn.execute(
                """SELECT id FROM event_editions
                    WHERE event_id = ? AND year = ? AND sub_event = ? AND origin = 'manual'""",
                (event_id, draft.year, draft.sub_event or ""),
            ).fetchone()

            values = {
                "registration_start": reg_start,
                "registration_end": reg_end,
                "contest_start": con_start,
                "contest_end": con_end,
                "registration_raw": draft.registration_raw,
                "contest_raw": draft.contest_raw,
                "time_confidence": draft.time_confidence,
                "time_precision": draft.time_precision,
                "reward": draft.reward,
                "team_info": draft.team_info,
                "source_url": source_url,
                "source_note": entry.get("source_note"),
                "updated_at": now,
            }
            values = {
                k: (v.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(v, "strftime") else v)
                for k, v in values.items()
            }

            if existing:
                assignments = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE event_editions SET {assignments} WHERE id = ?",
                    [*values.values(), existing["id"]],
                )
            else:
                cols = [
                    "event_id", "year", "sub_event", "origin", "source_key",
                    "first_seen_at", *values.keys(),
                ]
                conn.execute(
                    f"INSERT INTO event_editions ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    [
                        event_id, draft.year, draft.sub_event or "", "manual", "seed",
                        now, *values.values(),
                    ],
                )
            imported += 1

    log.info("已导入 %d 条历年数据（跳过 %d 条）", imported, skipped)
    return imported


def sync_sources() -> int:
    """把注册表中的数据源同步到 sources 表，保留已有健康状态与用户自填的说明。"""
    from .crawlers import REGISTRY, ensure_registered

    ensure_registered()
    with get_conn() as conn:
        for key, crawler in REGISTRY.items():
            conn.execute(
                """
                INSERT INTO sources (key, name, kind, base_url, note, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT (key) DO UPDATE SET
                    name = excluded.name,
                    kind = excluded.kind,
                    base_url = excluded.base_url,
                    note = excluded.note
                """,
                (key, crawler.name, crawler.kind, crawler.base_url, getattr(crawler, "note", None)),
            )
    return len(REGISTRY)


def load_all() -> dict[str, int]:
    events = load_events()
    editions = load_editions()
    sources = sync_sources()
    return {"events": events, "editions": editions, "sources": sources}
