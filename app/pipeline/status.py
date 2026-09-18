"""赛事状态推导。

状态原本只在爬取时算一次，写进 event_editions.status 就再也不动了。这不行：
算出来的那一刻是对的，之后一直冻在那儿 —— 报名截止过去、比赛打完，那条记录
还挂着「报名中」，要等下一次爬取（默认一周后）才有机会更新；而若那一届已经
没有新通知（源不会再产出这条 draft），它就永远是「报名中」。实测库里 68 条
contest_end 已过、状态却是 registering 或 running。

所以状态分两层：
  - **入库时**算一次（persist.derive_status），作为兜底，也是站点自带状态的落点；
  - **读取时**再按当前时间纠正一次（effective_status）。

只用字符串比较：库里时间一律是 'YYYY-MM-DDTHH:MM:SSZ'（UTC、定长），
字典序即时间序，不必解析成 datetime。
"""

from __future__ import annotations

from typing import Mapping

from .normalize import utcnow_iso


def derive_status_from_iso(
    *,
    registration_start: str | None = None,
    registration_end: str | None = None,
    contest_start: str | None = None,
    contest_end: str | None = None,
    now: str | None = None,
) -> str | None:
    """按时间推状态。推不出返回 None（即「时间待公布」）。

    入参与 persist.derive_status 一一对应，只是换成 ISO 串。
    """
    now = now or utcnow_iso()

    if registration_end and registration_end < now and (
        contest_end is None or contest_end < now
    ):
        return "finished"
    if contest_start and contest_start < now and (
        contest_end is None or now < contest_end
    ):
        return "running"
    if (
        registration_start
        and registration_start < now
        and registration_end
        and now < registration_end
    ):
        return "registering"
    if contest_start and now < contest_start:
        return "upcoming"
    if registration_end and now < registration_end:
        return "upcoming"
    return None


def has_ended(
    *,
    registration_end: str | None = None,
    contest_end: str | None = None,
    now: str | None = None,
) -> bool:
    """该届是不是已经打完了。

    比赛结束时刻过了就算完 —— 不要求报名截止也有值（平台型源常常没有独立的
    报名阶段，只有赛季区间），也不要求原来那个状态是什么。
    """
    now = now or utcnow_iso()
    if contest_end:
        return contest_end < now
    return bool(registration_end) and registration_end < now


def effective_status(record: Mapping, *, now: str | None = None) -> str | None:
    """读路径用的状态：时间上明摆着结束了，就别再显示「报名中」。

    record 是 event_editions（或 v_edition_best）的一行。

    **只在算得 finished 时覆盖**：站点自带的 running / registering 比按日期
    猜的准，不能反过来把它们改掉；这里纠正的只是"早就该翻篇却没人翻"的那批。
    """
    stored = record.get("status")
    if stored == "finished":
        return stored
    if has_ended(
        registration_end=record.get("registration_end"),
        contest_end=record.get("contest_end"),
        now=now,
    ):
        return "finished"
    return stored
