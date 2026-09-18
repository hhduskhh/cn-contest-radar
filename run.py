"""启动入口。

固定单 worker、不加 reload —— 这两点必须配套：
多进程会让每个进程各起一个 APScheduler，同一时刻重复爬取；
--reload 会在文件变动时重启进程，同样造成调度器重复。
"""

from __future__ import annotations

import os
import socket
import sys

# Windows 控制台默认 GBK，中文日志会乱码或抛 UnicodeEncodeError。
# 必须在任何输出发生之前设置。
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn  # noqa: E402

from app import config  # noqa: E402


def local_ips() -> list[str]:
    ips: set[str] = set()
    try:
        # 不会真的发包，只是让内核选出出口网卡，从而拿到局域网 IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main() -> None:
    port = config.PORT
    print("=" * 62)
    print("  国内计算机 / AI 赛事信息聚合")
    print("=" * 62)
    print(f"  本机访问   http://127.0.0.1:{port}/")
    for ip in local_ips():
        print(f"  手机访问   http://{ip}:{port}/   （需同一 WiFi）")
    print(f"  存活探测   http://127.0.0.1:{port}/healthz   （免登录）")
    print()
    print(f"  登录账号   {config.APP_USER}")
    print(f"  登录密码   见项目根目录 .env 中的 APP_PASSWORD")
    print("=" * 62)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        reload=False,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
