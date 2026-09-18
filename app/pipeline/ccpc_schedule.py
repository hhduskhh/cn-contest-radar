"""CCPC「各场比赛安排」通知的专用解析器。

这是 CCPC 最有价值的一处数据：每年一份赛程表，列出全部赛站的比赛日期。
但官网四年里有四种排版（实测）：

  2026  长春国赛（东北师范大学）10.17-18          —— 名称在前，括号里是学校
  2025  国赛（哈尔滨）11月8日~9日哈尔滨工业大学…    —— 表格被拍平，含中文月日
  2024  10.19-20，哈尔滨，东北林业大学承办，…      —— 日期在前
  2023  国赛（秦皇岛）- 东北大学秦皇岛分校：10.14-15 —— 名称前、日期后

所以不能用一个正则打天下。做法是：按行切分 → 每行独立找日期 → 再从行内
按优先级推赛站名。找不到日期的行跳过（例如 2023 的「总决赛（长春）：待定」）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import NamedTuple

from .normalize import TZ

# 日期：支持 9.19 / 10.17-18 / 11月8日~9日 / 09.07-08 / 10.14-15
_RE_DATE_RANGE = re.compile(
    r"(\d{1,2})\s*[.．月]\s*(\d{1,2})\s*日?"
    r"(?:\s*[-–—~～至到]\s*(\d{1,2})\s*日?)?"
)

# 赛站名的关键词优先级（高 → 低）
_KEYWORD_STATIONS = (
    ("网络预选赛", "网络赛"),
    ("网络赛", "网络赛"),
    ("女生专场", "女生专场"),
    ("女生赛", "女生专场"),
    ("高职专场", "高职专场"),
    ("高职赛", "高职专场"),
    ("总决赛", "总决赛"),
)

# 「国赛（哈尔滨）」「国赛(哈尔滨)」
_RE_GUOSAI_PAREN = re.compile(r"国赛\s*[（(]\s*([^）)]{2,12})\s*[）)]")
# 中文城市名（2-4 字），用于 2024 的「10.19-20，哈尔滨，…」格式
_RE_CITY = re.compile(r"[，,]\s*([一-龥]{2,4})\s*[，,]")
# 「国赛 - 东北大学秦皇岛分校」「国赛（秦皇岛）- 东北大学秦皇岛分校」
_RE_DASH_SCHOOL = re.compile(r"国赛\s*(?:[（(][^）)]*[）)])?\s*[-—–]\s*([^\s：:，,]{4,20})")


class StationSchedule(NamedTuple):
    station: str
    start: datetime
    end: datetime
    raw: str


def parse_schedule(text: str, year: int) -> list[StationSchedule]:
    """从赛程表正文里解析出各赛站的比赛日期。"""
    if not text:
        return []

    normalized = unicodedata.normalize("NFKC", text)
    results: list[StationSchedule] = []
    seen: set[tuple[str, str]] = set()

    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            continue

        matches = list(_RE_DATE_RANGE.finditer(line))
        for index, date_match in enumerate(matches):
            month = int(date_match.group(1))
            day = int(date_match.group(2))
            end_day = int(date_match.group(3)) if date_match.group(3) else day
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue

            try:
                start = datetime(year, month, day, tzinfo=TZ)
            except ValueError:
                continue
            try:
                end = datetime(year, month, end_day, tzinfo=TZ)
            except ValueError:
                end = start
            # 结束日小于起始日说明跨月（如 10.30-2），保守起见退回同日
            if end < start:
                end = start

            station = _station_of(_context(line, matches, index))
            key = (station, start.strftime("%Y-%m-%d"))
            if key in seen:
                continue
            seen.add(key)

            results.append(
                StationSchedule(
                    station=station,
                    start=start,
                    end=end,
                    raw=line[:120],
                )
            )

    return results


def _context(line: str, matches: list[re.Match], index: int) -> str:
    """取某个日期前后、到相邻日期为止的文本，用于推断这段属于哪个赛站。

    先看日期之后的片段（2024 是「11.02-03，女生专场，重庆…」），
    之后太短再看之前的片段（2023 是「国赛（秦皇岛）- …：10.14-15」）。
    """
    current = matches[index]
    after_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
    before_start = matches[index - 1].end() if index > 0 else 0

    after = line[current.end() : after_end].strip(" -—–~～，,：:、")
    before = line[before_start : current.start()].strip(" -—–~～，,：:、")

    # 取信息量更大的一侧；两侧都有内容时合起来判断（关键词可能在任一侧）
    if len(after) >= 3 and len(before) >= 3:
        return f"{before} {after}"
    return after if len(after) >= len(before) else before


def _station_of(line: str) -> str:
    """按优先级从行内推断赛站名。"""
    for keyword, label in _KEYWORD_STATIONS:
        if keyword in line:
            # 「国赛（哈尔滨）」优先用括号里的城市，比笼统的"国赛"更有信息量
            paren = _RE_GUOSAI_PAREN.search(line)
            if paren and label == "总决赛":
                return label
            return label

    paren = _RE_GUOSAI_PAREN.search(line)
    if paren:
        return f"国赛（{paren.group(1)}）"

    school = _RE_DASH_SCHOOL.search(line)
    if school:
        return school.group(1)[:20]

    city = _RE_CITY.search(line)
    if city:
        return city.group(1)

    # 兜底：整行去掉日期后的前几个中文字
    stripped = _RE_DATE_RANGE.sub("", line).strip(" -—–~～，,：:")
    chinese = re.findall(r"[一-龥]{2,}", stripped)
    return chinese[0][:20] if chinese else "赛站"
