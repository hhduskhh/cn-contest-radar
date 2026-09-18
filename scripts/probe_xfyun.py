"""讯飞接口探测工具（一次性使用）。

用途：讯飞站点重构后，原来那个「一次返回 667 条全量赛题、含完整分阶段时间」
的接口已经失效。确切的新路径需要用你**自己登录后的真实请求**来确定。

用法（三步）：

  1. 浏览器登录 https://challenge.xfyun.cn/ ，打开赛题列表页
  2. F12 → Network 面板 → 筛选框输入 `ai-contest` 或 `contest`
     → 找到返回赛题列表的那个请求 → 右键 → Copy → **Copy as cURL (bash)**
  3. 运行本脚本，把复制的内容整段粘进去：

         python scripts/probe_xfyun.py

  脚本会自动提取端点、参数、请求头和 Cookie，试跑一次，并打印返回的数据结构。

拿到结果后把输出发给我，我据此补全 app/crawlers/xfyun.py 的登录分支。

注意：本脚本只做只读 GET 请求，不会修改你账号上的任何东西。
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def parse_curl(text: str) -> dict | None:
    """从 'Copy as cURL' 的文本里提取 url / headers / cookie。"""
    cleaned = text.replace("\\\n", " ").replace("^\n", " ")
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        tokens = cleaned.split()

    url = None
    headers: dict[str, str] = {}
    cookie = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("http") and url is None:
            url = token
        elif token in ("-H", "--header") and i + 1 < len(tokens):
            name, _, value = tokens[i + 1].partition(":")
            headers[name.strip()] = value.strip()
            i += 1
        elif token in ("-b", "--cookie") and i + 1 < len(tokens):
            cookie = tokens[i + 1]
            i += 1
        i += 1

    if url is None:
        return None
    if cookie and "cookie" not in {k.lower() for k in headers}:
        headers["Cookie"] = cookie
    return {"url": url, "headers": headers}


def parse_cookie_only(text: str) -> dict | None:
    """用户只粘了 Cookie 字符串的情况。"""
    text = text.strip()
    if not text or "=" not in text:
        return None
    return {
        "url": "https://challenge.xfyun.cn/2020/ai-contest/api/algorithm/contest-list",
        "headers": {"Cookie": text},
    }


def describe(payload, depth: int = 0, max_depth: int = 3) -> None:
    """递归打印 JSON 结构（只打形状，不打全部数据，避免刷屏）。"""
    pad = "  " * depth
    if depth > max_depth:
        return
    if isinstance(payload, dict):
        for key, value in list(payload.items())[:25]:
            kind = type(value).__name__
            if isinstance(value, (dict, list)):
                size = len(value)
                print(f"{pad}{key}: {kind}({size})")
                describe(value, depth + 1, max_depth)
            else:
                print(f"{pad}{key}: {kind} = {repr(value)[:70]}")
    elif isinstance(payload, list):
        if payload:
            print(f"{pad}[0] 样例：")
            describe(payload[0], depth + 1, max_depth)


def main() -> None:
    print(__doc__)
    print("=" * 66)
    print("请粘贴 cURL 命令或 Cookie 字符串，然后按回车，再按 Ctrl+Z 回车（Windows 结束输入）：")
    print("=" * 66)

    raw = sys.stdin.read()
    if not raw.strip():
        print("没有输入内容，退出。")
        return

    spec = parse_curl(raw) or parse_cookie_only(raw)
    if spec is None:
        print("无法从输入中识别出 URL 或 Cookie。请确认复制的是完整的 Copy as cURL 内容。")
        return

    url = spec["url"]
    headers = spec["headers"]
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Referer", "https://challenge.xfyun.cn/")

    print(f"\n请求 URL: {url}")
    print(f"请求头: {sorted(headers.keys())}")
    cookie_len = len(headers.get("Cookie", ""))
    print(f"Cookie 长度: {cookie_len} 字符")

    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"\n请求失败: {exc}")
        return

    print(f"\nHTTP {resp.status_code}  Content-Type: {resp.headers.get('Content-Type')}")
    body = resp.content.decode("utf-8", errors="replace")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print("返回的不是 JSON。前 400 字符：")
        print(body[:400])
        return

    # 常见失败信号
    if isinstance(payload, dict):
        if payload.get("flag") is False or payload.get("code") in (80000, 10008):
            print(f"\n⚠ 接口返回失败：code={payload.get('code')} desc={payload.get('desc')}")
            print("  可能原因：Cookie 已过期、需要额外请求头、或该端点已变更。")
            print("  建议在 DevTools 里确认你复制的是**返回赛题列表**的那个请求。")

    print("\n返回结构：")
    describe(payload)

    # 尝试找赛题数组并给出条数
    def find_list(node, depth=0):
        if depth > 4:
            return None
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return node
        if isinstance(node, dict):
            for value in node.values():
                found = find_list(value, depth + 1)
                if found:
                    return found
        return None

    items = find_list(payload)
    if items:
        print(f"\n✓ 找到了疑似赛题数组，共 {len(items)} 条。首个元素的字段：")
        for key in list(items[0].keys())[:40]:
            print(f"    {key}")
        print("\n把以上输出发给我，我据此补全 xfyun 爬虫的登录分支。")


if __name__ == "__main__":
    main()
