"""爬取触发、进度轮询、源健康。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import get_conn
from ..pipeline import runner
from ..pipeline.normalize import utcnow_iso

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crawl", tags=["crawl"])

# 手动触发的批次跑在这个线程池里，避免阻塞事件循环
_background_tasks: set[asyncio.Task] = set()


class RunRequest(BaseModel):
    mode: str = "incremental"
    sources: list[str] | None = None


@router.post("/run")
async def trigger_run(req: RunRequest):
    """手动触发爬取。已有批次在跑时返回 409 并带上那个 run_id。"""
    if req.mode not in ("incremental", "backfill"):
        raise HTTPException(status_code=400, detail="mode 只能是 incremental 或 backfill")

    from ..crawlers import REGISTRY, ensure_registered

    ensure_registered()

    keys = req.sources or list(REGISTRY.keys())
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知数据源: {', '.join(unknown)}")

    existing = runner.active_run_id()
    if existing is not None:
        # 不报错，把已有的 run 交回去 —— 双开手机时点第二次是很常见的操作
        return {
            "run_id": existing,
            "status": "already_running",
            "message": "已有爬取任务在执行，已为你接续该任务的进度",
        }

    run_id = runner.create_run("manual", req.mode, keys)

    async def _run() -> None:
        try:
            # 爬虫是同步阻塞的，丢到线程里跑，别堵住事件循环
            await asyncio.to_thread(runner.execute_run, run_id, keys, req.mode)
        except Exception:  # noqa: BLE001
            log.exception("批次 %s 执行异常", run_id)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"run_id": run_id, "status": "started", "sources": keys, "mode": req.mode}


@router.get("/status")
def crawl_status():
    """当前是否有批次在跑。页面加载时用它决定要不要接续轮询。"""
    run_id = runner.active_run_id()
    return {"running": run_id is not None, "run_id": run_id}


@router.get("/runs")
def list_runs(limit: int = 20):
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
    return {"runs": rows}


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    """批次进度。前端按 1s 轮询这个接口。"""
    with get_conn() as conn:
        run = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="未找到该批次")

        items = [
            dict(r)
            for r in conn.execute(
                """SELECT i.*, s.name AS source_name, s.note AS source_note
                     FROM crawl_run_items i
                     LEFT JOIN sources s ON s.key = i.source_key
                    WHERE i.run_id = ?
                    ORDER BY i.id""",
                (run_id,),
            ).fetchall()
        ]

    run_dict = dict(run)
    finished = sum(1 for i in items if i["status"] in ("success", "failed", "skipped"))
    run_dict["total_sources"] = len(items)
    run_dict["finished_sources"] = finished
    run_dict["done"] = run_dict["status"] != "running"

    return {"run": run_dict, "items": items}


@router.get("/sources")
def list_sources():
    """源健康状态。含上次成功时间、连续失败次数、最近错误。"""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY key").fetchall()]

    from ..credentials import get_cookie_status

    for row in rows:
        row["cookie"] = get_cookie_status(row["key"])
    return {"sources": rows}


@router.post("/reap")
def reap():
    """把中断的批次标记为失败。启动时自动执行一次，也可手动调用。"""
    count = runner.reap_stale_runs()
    return {"reaped": count, "at": utcnow_iso()}
