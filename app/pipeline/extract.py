"""B 类源（新闻/通知）的关键词抽取：从标题与正文里找报名时间、比赛时间。

**设计原则是保守优先：宁可留空，也不猜。**

早期版本用固定的 80 字符窗口，结果把「备战蓝桥杯」文章里的日期、
「青少组模拟测试」的时间都当成了正式赛程，产出了一批看似权威实则错误的数据。
对备赛的人来说，一个错误的报名截止日期比没有日期危害大得多。

所以现在两道闸门：
  1. 窗口在**下一个标签或句子边界**处截断 —— 否则「比赛」的窗口会跨过
     「报名」标签，把报名区间当成比赛区间（CCPC 的实测案例）。
  2. 只有拿到**明确证据**才记录日期：区间必须是「日期 + 连接词 + 日期」，
     单日期必须紧邻「截止/开始」这类倾向词。其余一律留 None。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from ..models import ParsedTime
from .normalize import dates_with_positions, fix_cross_year

# 标签后最多看这么多字符
_WINDOW = 60
# 标签紧邻范围：倾向词必须出现在这么近的位置才算数
_HINT_NEAR = 12

_RE_REG = re.compile(
    r"(?:网上)?报名(?:时间|日期|期限|起止|截止日期|截止时间|起止时间)?"
)
_RE_CONTEST = re.compile(
    r"(?:比赛|竞赛|大赛|赛事|初赛|省赛|国赛|复赛|决赛|网络赛|选拔赛|正赛)"
    r"(?:时间|日期|安排|日程)?"
    r"|赛程|举办时间|竞赛日期|比赛日期"
)

# 单日期归属判断的倾向词
_RE_END_HINT = re.compile(r"截止|结束|止$|止[^a-zA-Z]|之前|以前|前$")
_RE_START_HINT = re.compile(r"开始|起$|即日|启动|开启")

# 明确表示"时间未定"，出现就别猜
_RE_OPEN_ENDED = re.compile(r"另行通知|待定|见后续|详见|留意|尚未|未定|以后续")

# 区间连接词：两个日期之间必须有它，否则不算一个区间
_RE_RANGE_SEP = re.compile(r"^\s*(?:至|到|—|–|-|~|～|─|起至|截至|until)\s*$|^\s*[至到—–~～]\s*")

# 句子边界：窗口遇到这里就停
_RE_SENTENCE_END = re.compile(r"[。！？；\n\r]|(?<=[a-zA-Z0-9])\.[\s]")

# 「即日起至X月X日」
_RE_FROM_NOW = re.compile(r"即日(?:起|期)?\s*(?:至|到|—|–|-|~|～)\s*")


def _normalize(text: str) -> str:
    """全角数字/标点统一成半角，否则正则匹配不到。"""
    return unicodedata.normalize("NFKC", text or "")


# 赛事相关性闸门。B 类源的新闻列表里混着大量非赛事内容
# （蓝桥杯的"招聘会""培训认证"、CCPC 的"组委会成员"），
# 不过滤的话时间线会被噪音塞满。
_RE_RELEVANT = re.compile(
    r"举办|报名|参赛|章程|赛程|比赛|大赛|竞赛|选拔赛|初赛|复赛|决赛|"
    r"省赛|国赛|网络赛|专项赛|赛道|分站赛"
)
_RE_IRRELEVANT = re.compile(
    r"招聘|就业|培训|认证|研讨|学术会议|论坛|获奖名单|成绩公示|证书|组委会成员|"
    r"备战|经验分享|心得体会|真题解析|模拟测试"
)


def is_competition_notice(title: str | None, text: str | None = None) -> bool:
    """判断一条通知是否值得进时间线。

    只扫标题不扫全文：全文里出现「大赛」二字太容易，会放过无关通知。
    """
    if not title:
        return False
    normalized = _normalize(title)
    if _RE_IRRELEVANT.search(normalized):
        return False
    return bool(_RE_RELEVANT.search(normalized))


def _window_after(text: str, match: re.Match) -> tuple[str, int]:
    """取标签之后的窗口，在下一个标签或句子边界处截断。

    截断是关键：不截断的话，「比赛」的窗口会一直向后延伸，
    跨过后面的「报名」标签，把报名区间误当成比赛区间。
    """
    start = match.end()
    window = text[start : start + _WINDOW]

    # 截到下一个标签
    for regex in (_RE_REG, _RE_CONTEST):
        other = regex.search(window)
        if other:
            window = window[: other.start()]

    # 截到句子边界
    sentence = _RE_SENTENCE_END.search(window)
    if sentence:
        window = window[: sentence.start()]

    return window, start


def _between(text: str, first_end: int, second_start: int) -> str:
    return text[first_end:second_start]


def _pick_range(
    window: str, default_year: int | None, published_at: datetime | None
) -> tuple[ParsedTime | None, ParsedTime | None]:
    """从窗口里提 (start, end)。拿不到明确证据就返回 (None, None)。"""
    if _RE_OPEN_ENDED.search(window):
        return None, None

    # 「即日起至X月X日」：起点是发布时间，证据充分
    if _RE_FROM_NOW.search(window) and published_at is not None:
        dated = dates_with_positions(window, default_year=default_year)
        if dated:
            start = ParsedTime(utc=published_at, raw="即日起", precision="date")
            return fix_cross_year(start, dated[0][2])

    dated = dates_with_positions(window, default_year=default_year)
    if not dated:
        return None, None

    if len(dated) >= 2:
        # 两个日期之间必须有连接词才算区间，否则它们很可能是两件不同的事
        first_start, first_end, first_time = dated[0]
        second_start, _, second_time = dated[1]
        gap = _between(window, first_end, second_start)
        if _RE_RANGE_SEP.match(gap) or _RE_RANGE_SEP.search(gap):
            return fix_cross_year(first_time, second_time)
        # 没有连接词：只认第一个日期，且必须是明显的起始描述
        return first_time, None

    # 单个日期：必须紧邻倾向词，否则不记录
    only = dated[0][2]
    head = window[: _HINT_NEAR + 8]
    if _RE_END_HINT.search(head):
        return None, only
    if _RE_START_HINT.search(head):
        return only, None
    # 窗口本身很短（说明标签后直接跟日期，上下文干净）时接受为起始
    if len(window.strip()) <= 20:
        return only, None
    return None, None


def extract_registration(
    text: str, *, default_year: int | None = None, published_at: datetime | None = None
) -> tuple[ParsedTime | None, ParsedTime | None, str | None]:
    """抽取报名起止。返回 (start, end, raw)。"""
    normalized = _normalize(text)
    match = _RE_REG.search(normalized)
    if not match:
        return None, None, None
    window, _ = _window_after(normalized, match)
    start, end = _pick_range(window, default_year, published_at)
    if start is None and end is None:
        return None, None, None
    return start, end, window.strip()[:120]


def extract_contest(
    text: str, *, default_year: int | None = None, published_at: datetime | None = None
) -> tuple[ParsedTime | None, ParsedTime | None, str | None]:
    """抽取比赛起止。返回 (start, end, raw)。"""
    normalized = _normalize(text)
    match = _RE_CONTEST.search(normalized)
    if not match:
        return None, None, None
    window, _ = _window_after(normalized, match)
    start, end = _pick_range(window, default_year, published_at)
    if start is None and end is None:
        return None, None, None
    return start, end, window.strip()[:120]


def has_explicit_registration(text: str) -> bool:
    """正文里是否明确提到过报名。用于判断这条通知值不值得建届次记录。"""
    return bool(_RE_REG.search(_normalize(text)))
