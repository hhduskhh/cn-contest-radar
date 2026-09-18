"""赛事、届次、时间线的读接口。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from ..db import get_conn
from ..pipeline.forecast import forecast_event
from .serialize import edition_to_dict, local_date

router = APIRouter(prefix="/api", tags=["events"])

# v_edition_best 已经仲裁好「人工种子 vs 爬取数据」的优先级，读接口一律走它
_BEST_JOIN = """
    SELECT b.*, e.slug AS event_slug, e.name AS event_name,
           e.category AS category, e.official_url AS official_url,
           e.series AS series
      FROM v_edition_best b
      JOIN events e ON e.id = b.event_id
"""


@router.get("/events")
def list_events(category: str | None = None, q: str | None = None):
    sql = "SELECT * FROM events WHERE active = 1"
    params: list = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if q:
        sql += " AND (name LIKE ? OR description LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY category, name"

    with get_conn() as conn:
        events = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for event in events:
            counts = conn.execute(
                """SELECT
                     COUNT(*) AS editions,
                     MIN(year) AS first_year,
                     MAX(year) AS last_year
                   FROM v_edition_best WHERE event_id = ?""",
                (event["id"],),
            ).fetchone()
            event.update(dict(counts))
            event["aliases"] = json.loads(event.get("aliases") or "[]")
    return {"events": events}


def _region_clause(region_filter: str | None, alias: str = "b") -> str:
    """「只看广东」：保留广东线下赛站与无举办地的线上赛事，只排除外省赛站。

    只排除 other 而不是只保留 guangdong —— 否则天池、DataFountain 这些
    没有举办地的平台赛事会被一并误删。
    """
    if region_filter in ("guangdong", "gd"):
        return f" AND {alias}.region != 'other'"
    return ""


@router.get("/events/{slug}")
def get_event(slug: str, from_year: int | None = None, region_filter: str | None = None):
    with get_conn() as conn:
        event = conn.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail=f"未找到赛事 {slug}")

        sql = _BEST_JOIN + " WHERE e.slug = ?"
        params: list = [slug]
        if from_year:
            sql += " AND b.year >= ?"
            params.append(from_year)
        sql += _region_clause(region_filter)
        sql += " ORDER BY b.year DESC, b.sub_event"

        editions = [edition_to_dict(r) for r in conn.execute(sql, params).fetchall()]

        # 相关通知：用该赛事的全部别名去匹配标题。
        # 不用 event.name 截断来做 LIKE —— 像「蓝桥杯全国软件和信息技术专业人才大赛」
        # 截前 6 个字是「蓝桥杯全国软」，匹配不到任何通知标题。
        aliases = json.loads(event["aliases"] or "[]") or [event["name"]]
        clauses = " OR ".join("title LIKE ?" for _ in aliases)
        notices = [
            dict(r)
            for r in conn.execute(
                f"""SELECT id, source_key, title, url, published_at, first_seen_at
                      FROM source_items
                     WHERE {clauses}
                     ORDER BY published_at DESC LIMIT 50""",
                [f"%{alias}%" for alias in aliases],
            ).fetchall()
        ]

    result = dict(event)
    result["aliases"] = aliases
    return {"event": result, "editions": editions, "notices": notices}


@router.get("/timeline")
def timeline(
    slugs: str | None = Query(None, description="逗号分隔的赛事 slug，缺省返回全部"),
    from_year: int = 2020,
    to_year: int = 2030,
    region_filter: str | None = Query(None, description="传 guangdong 只看广东线下赛站"),
):
    """时间线聚合：按 赛事 × 年份 输出报名与比赛区间。"""
    sql = _BEST_JOIN + " WHERE b.year BETWEEN ? AND ?"
    params: list = [from_year, to_year]
    if slugs:
        keys = [s.strip() for s in slugs.split(",") if s.strip()]
        if keys:
            sql += f" AND e.slug IN ({', '.join('?' for _ in keys)})"
            params.extend(keys)
    sql += _region_clause(region_filter)
    sql += " ORDER BY e.name, b.year"

    with get_conn() as conn:
        raw = [
            (dict(r), r["series"] if "series" in r.keys() else 1)
            for r in conn.execute(sql, params).fetchall()
        ]

    series_rows = [edition_to_dict(r) for r, is_series in raw if is_series]
    platform_rows = [r for r, is_series in raw if not is_series]

    # 系列赛事：一届一条泳道，用赛站/赛道区分同届的多场比赛
    for row in series_rows:
        label = row["event_name"] or row["event_slug"]
        row["lane_label"] = f"{label} · {row['sub_event']}" if row["sub_event"] else label

    return {
        "editions": series_rows + _aggregate_platform(platform_rows),
        "from_year": from_year,
        "to_year": to_year,
    }


def _aggregate_platform(rows: list[dict]) -> list[dict]:
    """把平台型赛事（天池、讯飞这类一年上百场独立比赛的）按年聚合成一条。

    不聚合的话时间线上会出现几百条泳道，完全没法看。
    聚合后的一条表达的是「这一年这个平台上有多少场比赛、大致横跨什么时候」——
    这才是看节奏时需要的信息。
    """
    buckets: dict[tuple[int, int], list[dict]] = {}
    for row in rows:
        buckets.setdefault((row["event_id"], row["year"]), []).append(row)

    # 聚合区间要裁到这一年之内。源数据里有「长期开放」的哨兵值
    # （DataFountain 用 2099-12-31，和鲸有写到 2044 的），不裁的话
    # 从该年起每一年都会出现一条横贯全图的色条。
    out: list[dict] = []
    for (_, year), group in buckets.items():
        # 边界要用北京时间换算成 UTC：北京 1/1 00:00 = UTC 上年 12/31 16:00。
        # 直接写 {year}-01-01T00:00:00Z 会偏 8 小时，展示时变成次年 1 月 1 日。
        lower = f"{year - 1}-12-31T16:00:00Z"
        upper = f"{year}-12-31T15:59:59Z"

        def _min(key: str):
            vals = [max(g[key], lower) for g in group if g.get(key)]
            return min(vals) if vals else None

        def _max(key: str):
            vals = [min(g[key], upper) for g in group if g.get(key)]
            return max(vals) if vals else None

        sample = edition_to_dict(group[0])
        sample.update(
            {
                "sub_event": "",
                "title": None,
                "registration_start": local_date(_min("registration_start")),
                "registration_end": local_date(_max("registration_end")),
                "contest_start": local_date(_min("contest_start")),
                "contest_end": local_date(_max("contest_end")),
                "registration_raw": f"{year} 年共 {len(group)} 场比赛",
                "contest_raw": None,
                "source_url": None,
                "event_count": len(group),
                "lane_label": f"{sample.get('event_name') or sample.get('event_slug')}（{len(group)} 场）",
            }
        )
        out.append(sample)
    return out


@router.get("/editions/upcoming")
def upcoming(
    days: int = 90,
    limit: int = 30,
    region_filter: str | None = Query(None, description="传 guangdong 只看广东线下赛站"),
):
    """即将截止报名与即将开赛的赛事。总览页红榜的数据源。"""
    sql = (
        _BEST_JOIN
        + """
        WHERE ((b.registration_end IS NOT NULL
                AND b.registration_end >= datetime('now')
                AND b.registration_end <= datetime('now', ?))
            OR (b.contest_start IS NOT NULL
                AND b.contest_start >= datetime('now')
                AND b.contest_start <= datetime('now', ?)))
        """
        + _region_clause(region_filter)
        + " ORDER BY COALESCE(b.registration_end, b.contest_start) LIMIT ?"
    )
    with get_conn() as conn:
        rows = [
            edition_to_dict(r)
            for r in conn.execute(sql, (f"+{days} days", f"+{days} days", limit)).fetchall()
        ]
    return {"editions": rows, "days": days}


@router.get("/editions/{edition_id}")
def get_edition(edition_id: int):
    with get_conn() as conn:
        row = conn.execute(_BEST_JOIN + " WHERE b.id = ?", (edition_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="未找到该届次")
    return edition_to_dict(row)


CATEGORY_LABEL = {
    "algorithm": "程序设计",
    "ai": "人工智能",
    "bigdata": "大数据",
    "robotics": "机器人",
    "design": "设计",
}


def _mmdd(utc: str | None) -> str | None:
    """UTC 串 → 北京时间的 MM-DD。首页表格要的就是「几号到几号」。"""
    day = local_date(utc)
    return day[5:] if day else None


def _span(start: str | None, end: str | None) -> str | None:
    a, b = _mmdd(start), _mmdd(end)
    if not a and not b:
        return None
    return f"{a} ~ {b}" if a != b else a


@router.get("/dashboard")
def dashboard():
    """首页数据：系列赛事的历年报名/比赛日期 + 推算的今年大致时间。

    系列赛事（蓝桥杯、CCPC 这类一年一届）按类型分组，附历年对照；
    平台赛事（天池、讯飞这类一年上百场独立比赛）单独一块，只列当前能报名的。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        series = conn.execute(
            _BEST_JOIN + " WHERE e.series = 1 ORDER BY e.name, b.year DESC"
        ).fetchall()
        open_now = conn.execute(
            _BEST_JOIN
            + """ WHERE e.series = 0
                    AND b.registration_end >= ? AND b.registration_start <= ?
                  ORDER BY b.registration_end LIMIT 30""",
            (now, now),
        ).fetchall()

    # 按赛事归拢，再按年份压成一年一行。
    # CCPC 一届有 6 个赛站、蓝桥杯有多个专项赛，不压的话同一年会重复出现好几行。
    grouped: dict[int, dict] = {}
    for row in series:
        item = grouped.setdefault(
            row["event_id"],
            {
                "slug": row["event_slug"],
                "name": row["event_name"],
                "category": row["category"],
                "official_url": row["official_url"],
                "raw": [],      # 给 forecast 用，需要原始的 *_start 字段
                "years": {},    # 给展示用，一年一条
            },
        )
        item["raw"].append(dict(row))  # sqlite3.Row 没有 .get()，forecast 要用

        bucket = item["years"].setdefault(
            row["year"], {"reg": [], "con": [], "subs": 0, "url": None}
        )
        bucket["reg"] += [v for v in (row["registration_start"], row["registration_end"]) if v]
        bucket["con"] += [v for v in (row["contest_start"], row["contest_end"]) if v]
        bucket["subs"] += 1
        bucket["url"] = bucket["url"] or row["source_url"]

    for item in grouped.values():
        item["forecast"] = forecast_event(item["raw"])
        item["history"] = [
            {
                "year": year,
                "registration": _span(min(b["reg"]), max(b["reg"])) if b["reg"] else None,
                "contest": _span(min(b["con"]), max(b["con"])) if b["con"] else None,
                # 同年有多场（分站赛/专项赛）时标出来，避免用户以为只有一场
                "subs": b["subs"] if b["subs"] > 1 else 0,
                "source_url": b["url"],
            }
            for year, b in sorted(item["years"].items(), reverse=True)
            # 这是日期对照表，两年都没日期的那一行没有信息量（ICPC 全是这种）
            if b["reg"] or b["con"]
        ][:6]
        del item["raw"], item["years"]

    groups: dict[str, list] = {}
    for item in grouped.values():
        groups.setdefault(item["category"], []).append(item)

    return {
        "groups": [
            {"category": cat, "label": CATEGORY_LABEL.get(cat, cat), "events": events}
            for cat, events in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ],
        "platform": [
            {
                "event_id": r["event_id"],
                "title": r["title"] or r["sub_event"],
                "event_name": r["event_name"],
                "registration_end": local_date(r["registration_end"]),
                "days_left": edition_to_dict(r)["days_until_deadline"],
                "url": r["source_url"],
            }
            for r in open_now
        ],
    }


@router.get("/stats")
def stats():
    """首页概览数字。"""
    with get_conn() as conn:
        # 所有查询都必须在 with 块内完成 —— 出了块连接就关了
        def scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        result = {
            "events": scalar("SELECT COUNT(*) FROM events WHERE active = 1"),
            "editions": scalar("SELECT COUNT(*) FROM v_edition_best"),
            "all_editions": scalar("SELECT COUNT(*) FROM event_editions"),
            "source_items": scalar("SELECT COUNT(*) FROM source_items"),
            "manual_editions": scalar(
                "SELECT COUNT(*) FROM v_edition_best WHERE origin = 'manual'"
            ),
            "guangdong_editions": scalar(
                "SELECT COUNT(*) FROM v_edition_best WHERE region = 'guangdong'"
            ),
            "by_category": [
                dict(r)
                for r in conn.execute(
                    """SELECT e.category, COUNT(DISTINCT e.id) AS events,
                              COUNT(b.id) AS editions
                         FROM events e
                         LEFT JOIN v_edition_best b ON b.event_id = e.id
                        WHERE e.active = 1
                        GROUP BY e.category ORDER BY editions DESC"""
                ).fetchall()
            ],
        }
    return result
