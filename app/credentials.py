"""运行时凭据存取（目前只有讯飞的 Cookie）。

设计原则：
  - **只存 Cookie，永不存密码**。代码全程不接触用户密码，也不模拟登录流程。
  - Cookie 由用户自己在浏览器登录后复制粘贴，用户随时可在平台退出登录使其失效。
  - 存到 data/runtime.json（已 gitignore），API 端点**永不回传 Cookie 明文**，
    只返回是否已配置与是否有效，供前端显示状态。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import config

log = logging.getLogger(__name__)


def _read() -> dict[str, Any]:
    if not config.RUNTIME_PATH.exists():
        return {}
    try:
        return json.loads(config.RUNTIME_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("runtime.json 读取失败，按空处理：%s", exc)
        return {}


def _write(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    # 先写临时文件再替换，避免写一半断电留下坏文件
    tmp = config.RUNTIME_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.RUNTIME_PATH)


def get_cookie(source_key: str) -> str | None:
    entry = _read().get("cookies", {}).get(source_key)
    return entry.get("value") if entry else None


def get_cookie_status(source_key: str) -> dict[str, Any]:
    """给前端看的状态，**不含 Cookie 明文**。"""
    entry = _read().get("cookies", {}).get(source_key)
    if not entry:
        return {"configured": False, "updated_at": None, "valid": None}
    return {
        "configured": True,
        "updated_at": entry.get("updated_at"),
        "valid": entry.get("valid"),
        "last_checked_at": entry.get("last_checked_at"),
    }


def set_cookie(source_key: str, value: str, *, valid: bool | None = None) -> None:
    data = _read()
    cookies = data.setdefault("cookies", {})
    cookies[source_key] = {
        "value": value.strip(),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid": valid,
    }
    _write(data)
    log.info("已更新 %s 的 Cookie", source_key)


def mark_cookie_validity(source_key: str, valid: bool) -> None:
    data = _read()
    entry = data.get("cookies", {}).get(source_key)
    if not entry:
        return
    entry["valid"] = valid
    entry["last_checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(data)


def clear_cookie(source_key: str) -> None:
    data = _read()
    data.get("cookies", {}).pop(source_key, None)
    _write(data)
    log.info("已清除 %s 的 Cookie", source_key)


def cookie_header(source_key: str) -> dict[str, str]:
    """把 Cookie 组装成请求头。没有配置则返回空 dict。"""
    value = get_cookie(source_key)
    return {"Cookie": value} if value else {}


def parse_cookie_input(raw: str) -> str:
    """兼容用户直接粘贴整段 'Copy as cURL' 或 'name=value; name2=value2' 两种形式。"""
    text = (raw or "").strip()
    if not text:
        return ""

    # 粘贴的是 curl 命令：从中抽出 -H 'cookie: ...' 或 -b '...'
    if text.lower().startswith("curl") or " -h " in text.lower():
        import re
        import shlex

        try:
            tokens = shlex.split(text.replace("\\\n", " "))
        except ValueError:
            tokens = text.split()

        for i, token in enumerate(tokens):
            lowered = token.lower()
            if lowered in ("-b", "--cookie") and i + 1 < len(tokens):
                return tokens[i + 1].strip().strip("'\"")
            if lowered in ("-h", "--header") and i + 1 < len(tokens):
                header = tokens[i + 1]
                name, _, value = header.partition(":")
                if name.strip().lower() == "cookie":
                    return value.strip()
        # 兜底：直接在原文里找 cookie: 后面到行尾的内容
        match = re.search(r"[Cc]ookie:\s*([^\n\r'\"]+)", text)
        if match:
            return match.group(1).strip()

    return text
