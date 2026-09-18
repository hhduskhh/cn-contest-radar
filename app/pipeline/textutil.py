"""文本清洗小工具。"""

from __future__ import annotations

import html
import re

_RE_TAG = re.compile(r"<[^>]+>")
_RE_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_RE_WS = re.compile(r"[ \t 　]+")
_RE_NEWLINES = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    """HTML 转纯文本。用于 CCPC 的 content、睿抗的 detail 等富文本字段。"""
    if not raw:
        return ""
    text = _RE_SCRIPT.sub(" ", raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.I)
    text = _RE_TAG.sub("", text)
    text = html.unescape(text)
    text = _RE_WS.sub(" ", text)
    text = _RE_NEWLINES.sub("\n\n", text)
    return text.strip()


def clean_text(raw: str | None) -> str:
    """纯文本字段的清洗：解实体、压空白。蓝桥杯的 synopsis 含 &ldquo; 这类实体。"""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _RE_WS.sub(" ", text)
    text = _RE_NEWLINES.sub("\n\n", text)
    return text.strip()
