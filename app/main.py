"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config, seeds
from .api import routes_admin, routes_crawl, routes_events
from .db import init_db
from .pipeline import runner
from .pipeline.events_catalog import reload_catalog
from .scheduler import next_run_time, shutdown_scheduler, start_scheduler
from .security import BasicAuthMiddleware


def setup_logging() -> None:
    config.ensure_dirs()
    # Windows 上 stdout 默认是 GBK，中文日志会乱码甚至抛 UnicodeEncodeError，
    # 所以文件处理器必须显式指定 utf-8，控制台输出则依赖 PYTHONUTF8=1。
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler, console]
    # 这些库的 INFO 日志太吵
    for noisy in ("apscheduler", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log = logging.getLogger("app.startup")

    init_db()
    counts = seeds.load_all()
    reload_catalog()
    log.info(
        "种子导入完成：赛事 %s，历年数据 %s，数据源 %s",
        counts["events"], counts["editions"], counts["sources"],
    )

    # 上次进程异常退出可能留下 running 状态的批次，不清理会把之后所有手动触发都挡掉
    reaped = runner.reap_stale_runs()
    if reaped:
        log.warning("清理了 %d 个中断的批次", reaped)

    start_scheduler()

    if config.CRAWL_ON_STARTUP:
        import asyncio

        asyncio.create_task(asyncio.to_thread(runner.run_all_sources, "incremental", "schedule"))
        log.info("已在启动时触发一次增量爬取")

    yield

    shutdown_scheduler()


app = FastAPI(
    title="赛事信息聚合",
    description="国内计算机 / AI 赛事信息聚合与历年时间线",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(BasicAuthMiddleware)


@app.get("/healthz")
def healthz():
    """免鉴权的存活探测。手机可以先访问这个确认服务可达。"""
    return {"ok": True, "next_scheduled_crawl": next_run_time()}


app.include_router(routes_events.router)
app.include_router(routes_crawl.router)
app.include_router(routes_admin.router)

# 静态站点挂在最后：Starlette 按注册顺序匹配，API 路由先注册才能优先命中
config.ensure_dirs()
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
