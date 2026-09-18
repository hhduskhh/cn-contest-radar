"""每周定时爬取。

Windows 注意事项（都踩过）：
  - 必须用 AsyncIOScheduler，不要用 BackgroundScheduler + 多进程：
    Windows 没有 fork，spawn 出来的子进程里任务会静默不执行。
  - 启动必须固定单 worker 且不加 --reload：多 worker 会让每个进程各起一个
    调度器，同一时刻重复爬取。max_instances=1 只在本进程内生效，挡不住多进程。
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config
from .pipeline import runner

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _scheduled_crawl() -> None:
    log.info("定时爬取开始")
    try:
        run_id = await asyncio.to_thread(runner.run_all_sources, "incremental", "schedule")
        log.info("定时爬取完成，批次 %s", run_id)
    except Exception:  # noqa: BLE001 —— 定时任务绝不能因异常而停止后续调度
        log.exception("定时爬取失败")


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone=config.TZ)
    scheduler.add_job(
        _scheduled_crawl,
        CronTrigger(
            day_of_week=config.CRAWL_CRON_DAY_OF_WEEK,
            hour=config.CRAWL_CRON_HOUR,
            minute=config.CRAWL_CRON_MINUTE,
            timezone=config.TZ,
        ),
        id="weekly_crawl",
        replace_existing=True,
        max_instances=1,        # 上一轮没跑完，本轮直接跳过
        coalesce=True,          # 宕机期间堆积的多次触发合并成一次
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "定时爬取已启动：每 %s %02d:%02d（%s）",
        config.CRAWL_CRON_DAY_OF_WEEK,
        config.CRAWL_CRON_HOUR,
        config.CRAWL_CRON_MINUTE,
        config.TZ,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("weekly_crawl")
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()
