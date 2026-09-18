"""从历年日期推算「今年大概几号报名」。

只有三十行，但要避开两个坑：

  1. **必须用北京时间的日期。** 库里存的是 UTC，蓝桥杯 2025 的报名开始是
     北京时间 10-01，存成 UTC 是 09-30 —— 直接取 UTC 的月份会把「10 月」推成「9 月」。
  2. **用中位数而不是平均值。** 天梯赛 2021 年是 2 月、其余三年是 11 月，
     平均值会被这个离群值带到 9 月，中位数稳稳落在 11 月。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .. import config


def month_day(utc_iso: str | None) -> tuple[int, int, int] | None:
    """UTC 时间串 → 北京时间的 (年, 月, 日)。"""
    if not utc_iso:
        return None
    try:
        dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    local = dt.astimezone(config.TZ)
    return (local.year, local.month, local.day)


def _period(day: int) -> str:
    return "上旬" if day <= 10 else ("中旬" if day <= 20 else "下旬")


def estimate(samples: list[tuple[int, int, int]]) -> dict | None:
    """历年的 (年, 月, 日) → {'label': '10 月上旬~下旬', 'based_on': 6}。

    给区间而不是一个点：蓝桥杯历年的开始日是 10-01 到 10-20，说「10 月中旬」
    或「10 月上旬」都只对了三分之一，说「10 月上旬~下旬」才是实情。
    跨度小的时候（都在同一旬）自然退化成一个点。
    """
    if not samples:
        return None
    months = sorted(m for _, m, _ in samples)
    month = months[len(months) // 2]
    # 只统计落在中位月份里的日子；一个都没有就退回全部
    days = sorted(d for _, m, d in samples if m == month) or sorted(d for _, _, d in samples)
    lo, hi = _period(days[0]), _period(days[-1])
    label = f"{month} 月{lo}" if lo == hi else f"{month} 月{lo}~{hi}"
    # 按**年份**去重计数：CCPC 一届有 6 个赛站，按条数会报成「28 年」
    years = len({y for y, _, _ in samples})
    return {
        "month": month,
        "label": label,
        "based_on": years,
        # 少于 3 年样本的推算是猜测，前端要标出来
        "reliable": years >= 3,
    }


def forecast_event(rows: list[dict]) -> dict | None:
    """给一个赛事的历年行，推报名与比赛时间。

    rows 需含 registration_start / contest_start（UTC 串）。
    报名日期优先；没有报名数据的赛事（CCPC、ICPC、4C）用比赛日期兜底，
    否则它们永远算不出预计值。
    """
    reg = estimate([p for p in (month_day(r.get("registration_start")) for r in rows) if p])
    con = estimate([p for p in (month_day(r.get("contest_start")) for r in rows) if p])

    # 报名样本不足 3 年就不可信（CCPC 只有 1 条，推出来是「3 月中旬」，
    # 但它的比赛日期有 4 年赛程表数据）。这种情况宁可退回比赛时间。
    if reg and reg["based_on"] < 3 and con:
        reg = None

    if not reg and not con:
        return None
    return {
        "registration": reg,
        "contest": con,
        # True 表示没有可靠的报名数据，前端显示的其实是比赛时间
        "fallback": reg is None,
        "based_on": (reg or con)["based_on"],
    }
