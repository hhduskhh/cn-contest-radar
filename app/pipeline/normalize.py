"""时间归一化：各源五花八门的时间格式 → 统一的 UTC。

判别顺序至关重要，写错会静默产生错误日期（而不是报错）：

  1. 13 位纯数字 → Unix 毫秒。必须先于秒判断，否则 1773936000000 会被解析到公元 58000 年。
  2. 10 位纯数字 → Unix 秒。
  3. 带 Z 或 ±HH:MM 的 ISO8601 → 直接转 UTC，不再加任何偏移。
  4. 不带时区的 ISO8601（蓝桥杯 '2026-09-10T06:22:27'）→ 按 Asia/Shanghai 解释。
     这是最容易错的一类：当成 UTC 处理会整体偏 8 小时。
  5. 'YYYY/MM/DD'（AI Studio 列表页）→ 日期精度。
  6. 'YYYY-MM-DD HH:mm:ss'（AI Studio processList）→ 秒精度。
  7. 中文日期 '2026年9月10日'（讯飞路演日历）。
  8. 'YYYY-MM-DD' / 'YYYY.MM.DD' → 日期精度。
  9. 'M月D日' → 需要 default_year 补年。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .. import config
from ..models import ParsedTime

TZ = config.TZ

# 合理的 Unix 秒区间：2001-09 ~ 2033-05。超出即判为不是秒级时间戳。
_SEC_MIN = 1_000_000_000
_SEC_MAX = 2_000_000_000

_RE_DIGITS = re.compile(r"^\d+$")
_RE_CN_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_RE_CN_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_RE_YMD = re.compile(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")
_RE_YEAR = re.compile(r"(20\d{2})")


def to_utc_iso(dt: datetime) -> str:
    """统一入库格式：UTC ISO8601，秒级，'Z' 结尾。字典序即时间序。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_utc_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def to_local_date(dt: datetime) -> str:
    """给前端展示用：转回 Asia/Shanghai 的日期。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%Y-%m-%d")


def parse_any_datetime(
    value,
    *,
    assume_tz=TZ,
    default_year: int | None = None,
) -> ParsedTime | None:
    """把任意格式的时间文本/时间戳解析成 ParsedTime。解析不了返回 None，绝不瞎猜。"""
    if value is None:
        return None

    # 数值型时间戳（JSON 里可能是 int/float）
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch(float(value))

    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text or text.lower() in {"null", "none", "-", "nan"}:
        return None

    # 纯数字字符串也可能是时间戳
    if _RE_DIGITS.match(text):
        parsed = _from_epoch(float(text))
        if parsed is not None:
            return parsed
        # 不是合法时间戳则继续往下当日期文本试（如 "20260910"）

    # 中文日期：2026年9月10日
    m = _RE_CN_DATE.search(text)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0, text, "date")

    # ISO8601 / 常见分隔符格式。先尝试 withisoformat（能处理带时区的）
    iso = _try_isoformat(text, assume_tz)
    if iso is not None:
        return iso

    # YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
    m = _RE_YMD.search(text)
    if m:
        return _make(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            0, 0, 0, text, "date", assume_tz=assume_tz,
        )

    # M月D日（缺年份，靠 default_year 补）
    m = _RE_CN_MD.search(text)
    if m and default_year:
        return _make(
            default_year, int(m.group(1)), int(m.group(2)),
            0, 0, 0, text, "date", assume_tz=assume_tz,
        )

    return None


def _from_epoch(num: float) -> ParsedTime | None:
    # 13 位毫秒先判，再判 10 位秒。顺序反了会把毫秒值解释成公元 5 万年。
    if num > 1e11:
        seconds = num / 1000.0
        precision = "second"
    elif _SEC_MIN < num < _SEC_MAX:
        seconds = num
        precision = "second"
    else:
        return None
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return ParsedTime(utc=dt, raw=str(int(num)), precision=precision)


def _try_isoformat(text: str, assume_tz) -> ParsedTime | None:
    """处理 'YYYY-MM-DDTHH:MM:SS' 系列。带时区的直接用，不带的按 assume_tz 解释。"""
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    # 只处理看起来像 ISO 的（含 T 分隔或已带时区偏移）
    if "T" not in candidate and "+" not in candidate[10:]:
        return None
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    precision = "second" if (dt.hour or dt.minute or dt.second) else "date"
    if dt.tzinfo is None:
        # 关键：无时区的按本地时区解释，绝不当 UTC
        dt = dt.replace(tzinfo=assume_tz)
    return ParsedTime(utc=dt.astimezone(timezone.utc), raw=text, precision=precision)


def _make(
    year: int, month: int, day: int,
    hour: int, minute: int, second: int,
    raw: str, precision: str, *, assume_tz=TZ,
) -> ParsedTime | None:
    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=assume_tz)
    except ValueError:
        return None
    return ParsedTime(utc=dt.astimezone(timezone.utc), raw=raw, precision=precision)


def fix_cross_year(
    start: ParsedTime | None, end: ParsedTime | None
) -> tuple[ParsedTime | None, ParsedTime | None]:
    """跨年区间校正。

    蓝桥杯（头年 10 月报名、次年 4 月比赛）和 CCPC（头年 9 月网络赛）上，
    这是常态而非例外。判据：end < start 且月份差超过 6 个月，则 end 年份 +1。
    """
    if start is None or end is None:
        return start, end
    if end.utc >= start.utc:
        return start, end

    start_local = start.utc.astimezone(TZ)
    end_local = end.utc.astimezone(TZ)
    month_gap = (start_local.month - end_local.month) % 12
    if month_gap > 6:
        shifted = (end_local + timedelta(days=366)).replace(year=end_local.year + 1)
        # 用 replace(year+1) 更精确；+366 天只是为防止 2/29 越界的兜底
        try:
            shifted = end_local.replace(year=end_local.year + 1)
        except ValueError:
            pass
        return start, ParsedTime(
            utc=shifted.astimezone(timezone.utc), raw=end.raw, precision=end.precision
        )
    return start, end


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10}

_RE_EDITION_ARABIC = re.compile(r"第\s*(\d{1,2})\s*届")
_RE_EDITION_CHINESE = re.compile(r"第\s*([一二三四五六七八九十]{1,3})\s*届")


def _cn_to_int(text: str) -> int | None:
    """中文数字转整数，覆盖 1-99 的常见写法（十、十一、二十、二十三）。"""
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for ch in text:
        digit = _CN_DIGITS.get(ch)
        if digit is None:
            return None
        total = total * 10 + digit
    return total or None


def parse_edition_number(text: str | None) -> int | None:
    """从标题里提「第 N 届」的届次号（支持阿拉伯数字与中文数字）。

    注意：届次号本身不是年份。各赛事的届次起点不同（蓝桥杯第 12 届 = 2021，
    CCPC 第 11 届 = 2026），所以要由调用方给出偏移量换算。
    """
    if not text:
        return None
    match = _RE_EDITION_ARABIC.search(text)
    if match:
        return int(match.group(1))
    match = _RE_EDITION_CHINESE.search(text)
    if match:
        return _cn_to_int(match.group(1))
    return None


def year_from_edition(text: str | None, offset: int, *, min_year=2015, max_year=2035) -> int | None:
    """按「届次号 + 偏移 = 比赛年份」换算年份。

    蓝桥杯 offset=2009（第 17 届 = 2026），CCPC offset=2015（第 11 届 = 2026）。
    这个换算比"用发布时间年份"可靠得多 —— 头年 10 月发的报名通知，
    讲的是次年的比赛。
    """
    number = parse_edition_number(text)
    if number is None:
        return None
    year = number + offset
    return year if min_year <= year <= max_year else None


def infer_year(
    title: str | None = None,
    text: str | None = None,
    published_at: datetime | None = None,
) -> int | None:
    """推断届次年份。

    优先标题里显式的 20XX；其次正文首个 20XX；最后退回发布时间年份。
    注意：'第11届' 这类届次号不等于年份，不能拿来推算，只认显式出现的 20XX。
    """
    for source in (title, text):
        if source:
            m = _RE_YEAR.search(source)
            if m:
                return int(m.group(1))
    if published_at:
        return published_at.astimezone(TZ).year
    return None


def dates_with_positions(
    text: str, default_year: int | None = None
) -> list[tuple[int, int, ParsedTime]]:
    """抽出 (start, end, ParsedTime)，保留位置信息。

    位置是给抽取器判断「两个日期之间有没有区间连接词」用的 —— 没有连接词的
    两个日期往往属于两件不同的事，不该拼成一个区间。
    """
    normalized = unicodedata.normalize("NFKC", text)
    found: list[tuple[int, int, ParsedTime]] = []

    for regex in (_RE_CN_DATE, _RE_YMD):
        for m in regex.finditer(normalized):
            built = _make(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                0, 0, 0, m.group(0), "date",
            )
            if built:
                found.append((m.start(), m.end(), built))

    if default_year:
        for m in _RE_CN_MD.finditer(normalized):
            built = _make(
                default_year, int(m.group(1)), int(m.group(2)),
                0, 0, 0, m.group(0), "date",
            )
            if built:
                found.append((m.start(), m.end(), built))

    found.sort(key=lambda item: item[0])

    # 同一日期只保留首次出现（三种正则可能命中同一段文本）
    result: list[tuple[int, int, ParsedTime]] = []
    seen: set[str] = set()
    for start, end, parsed in found:
        key = to_local_date(parsed.utc)
        if key in seen:
            continue
        seen.add(key)
        result.append((start, end, parsed))
    return result


def all_dates_in(text: str, default_year: int | None = None) -> list[ParsedTime]:
    """按出现顺序抽出文本中所有日期。供 B 类关键词抽取使用。"""
    normalized = unicodedata.normalize("NFKC", text)
    found: list[tuple[int, ParsedTime]] = []

    for regex, builder in (
        (_RE_CN_DATE, lambda m: _make(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0, m.group(0), "date")),
        (_RE_YMD, lambda m: _make(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0, m.group(0), "date")),
    ):
        for m in regex.finditer(normalized):
            built = builder(m)
            if built:
                found.append((m.start(), built))

    if default_year:
        for m in _RE_CN_MD.finditer(normalized):
            built = _make(default_year, int(m.group(1)), int(m.group(2)), 0, 0, 0, m.group(0), "date")
            if built:
                found.append((m.start(), built))

    # 去重：同一位置只保留一个（_RE_CN_DATE 与 _RE_YMD 不会重叠，但保险起见）
    found.sort(key=lambda pair: pair[0])
    deduped: list[ParsedTime] = []
    seen_positions: set[int] = set()
    for pos, parsed in found:
        if pos in seen_positions:
            continue
        seen_positions.add(pos)
        deduped.append(parsed)

    # 同一年月日只保留首次出现
    seen_dates: set[str] = set()
    result: list[ParsedTime] = []
    for parsed in deduped:
        key = to_local_date(parsed.utc)
        if key in seen_dates:
            continue
        seen_dates.add(key)
        result.append(parsed)
    return result


def first_date(texts: Iterable[str], default_year: int | None = None) -> ParsedTime | None:
    for text in texts:
        if not text:
            continue
        dates = all_dates_in(text, default_year=default_year)
        if dates:
            return dates[0]
    return None
