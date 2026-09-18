"""Nuxt 2 的 window.__NUXT__ payload 解析器。

页面里的 payload 长这样：

    window.__NUXT__=(function(a,b,c,...,dv){...; return {layout:"default",data:[...]}}(args...))

对象字面量里的值被替换成了几百个短变量名（bD、aF…），真实值按顺序放在尾部的实参列表里。
纯正则提 JSON 必然失败，必须做两件事，缺一不可：

  1. 按顶层逗号切分尾部实参（**要感知字符串状态**，否则字符串里的逗号会把参数切碎）
  2. 把函数体里的短变量名替换回实参值，同时给裸对象键补引号
     （Nuxt 的对象字面量是合法 JS 但不是合法 JSON —— 键没有引号）

只替换变量不补引号 → "Expecting property name enclosed in double quotes"
只补引号不替换变量 → {layout:"default",data:[{competitions:[{id:T,title:bD... }}
"""

from __future__ import annotations

import json
import re
from typing import Any

_RE_NUXT = re.compile(r"window\.__NUXT__\s*=\s*(.*?)</script>", re.S)
_RE_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def parse_nuxt_iife(html: str) -> dict[str, Any]:
    """从 HTML 中解析出 Nuxt payload。解析失败抛 ValueError。"""
    match = _RE_NUXT.search(html)
    if not match:
        raise ValueError("页面中没有找到 window.__NUXT__")

    raw = match.group(1).strip().rstrip(";")
    if raw.startswith("(function("):
        raw = raw[len("(function(") :]
    elif raw.startswith("function("):
        raw = raw[len("function(") :]
    else:
        # 有些站点的 __NUXT__ 是纯 JSON，直接返回
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法识别的 __NUXT__ 格式: {exc}") from exc

    if ")" not in raw:
        raise ValueError("__NUXT__ 函数签名不完整")
    signature, rest = raw.split(")", 1)
    params = [p.strip() for p in signature.split(",") if p.strip()]

    # 尾部实参列表从最后一个 "}(" 开始
    args_start = rest.rfind("}(")
    if args_start < 0:
        raise ValueError("__NUXT__ 没有找到尾部实参列表")
    args_blob = rest[args_start + 2 :].rstrip().rstrip(")")

    # 函数体是最后一个 return 到 "}(" 之间的部分
    return_at = rest.rfind("return ", 0, args_start)
    if return_at < 0:
        raise ValueError("__NUXT__ 没有找到 return 语句")
    body = rest[return_at + len("return ") : args_start]

    args = _split_args(args_blob)
    amap = {
        name: (None if value.strip() in ("void 0", "") else value.strip())
        for name, value in zip(params, args)
    }

    substituted = _substitute(body, amap)
    try:
        return json.loads(substituted)
    except json.JSONDecodeError as exc:
        raise ValueError(f"__NUXT__ 回填后仍不是合法 JSON: {exc}") from exc


def _split_args(blob: str) -> list[str]:
    """按顶层逗号切分实参，感知字符串与括号深度。

    朴素的 blob.split(',') 会被字符串内部的逗号切碎 —— 而这里的实参
    常常就是整段 JSON 字符串（如 "[{\\"id\\": 226, \\"name\\": ...}]"）。
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_string = False
    quote = ""
    escaped = False

    for ch in blob:
        if in_string:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue

        if ch in ('"', "'", "`"):
            in_string = True
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _substitute(body: str, amap: dict[str, Any]) -> str:
    """把函数体里的短变量名替换成实参值，并给裸对象键补上引号。"""
    out: list[str] = []
    i = 0
    n = len(body)
    in_string = False
    quote = ""
    escaped = False

    while i < n:
        ch = body[i]

        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            i += 1
            continue

        if ch in ('"', "'"):
            in_string = True
            quote = ch
            out.append(ch)
            i += 1
            continue

        match = _RE_IDENT.match(body, i)
        if match:
            name = match.group(0)
            after = body[match.end() :].lstrip()
            if after.startswith(":"):
                # 对象键：补引号。注意不要消费后面的冒号。
                out.append(f'"{name}"')
            elif name in amap:
                value = amap[name]
                out.append("null" if value is None else str(value))
            elif name == "void":
                out.append("null")
                # 跳过后面的 " 0"
                rest = body[match.end() :]
                skip = re.match(r"\s+0", rest)
                if skip:
                    i = match.end() + skip.end()
                    continue
            elif name in ("true", "false", "null"):
                out.append(name)
            elif name == "undefined":
                out.append("null")
            else:
                out.append(name)
            i = match.end()
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def find_payload(html: str, path: list[str | int]) -> Any:
    """按路径取 payload 里的值，任一层缺失返回 None。"""
    try:
        node: Any = parse_nuxt_iife(html)
    except ValueError:
        return None
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        else:
            return None
    return node
