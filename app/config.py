"""配置读取。所有路径统一用 pathlib.Path（项目路径含中文，Windows 下尤其要注意）。"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SEED_DIR = DATA_DIR / "seeds"
WEB_DIR = BASE_DIR / "web"
LOG_DIR = BASE_DIR / "logs"
FIXTURE_DIR = BASE_DIR / "tests" / "fixtures"

load_dotenv(BASE_DIR / ".env")

TZ = ZoneInfo("Asia/Shanghai")

DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "app.db")))
RUNTIME_PATH = DATA_DIR / "runtime.json"

APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "admin")

PORT = int(os.getenv("PORT", "8000"))

CRAWL_CRON_DAY_OF_WEEK = os.getenv("CRAWL_CRON_DAY_OF_WEEK", "mon")
CRAWL_CRON_HOUR = int(os.getenv("CRAWL_CRON_HOUR", "3"))
CRAWL_CRON_MINUTE = int(os.getenv("CRAWL_CRON_MINUTE", "17"))
CRAWL_ON_STARTUP = os.getenv("CRAWL_ON_STARTUP", "0") == "1"

# 爬取礼貌性：同域请求最小间隔（秒）、全局并发上限、超时（连接, 读取）
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "1.5"))
MAX_CONCURRENT_SOURCES = int(os.getenv("MAX_CONCURRENT_SOURCES", "3"))
HTTP_TIMEOUT = (10, 30)
HTTP_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, SEED_DIR, LOG_DIR, FIXTURE_DIR):
        d.mkdir(parents=True, exist_ok=True)
