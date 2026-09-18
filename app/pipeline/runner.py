"""爬取编排：并发跑各源、单源失败隔离、批次与进度落库。

失败隔离是硬要求：8 个源里任何一个挂掉或改版，都不能影响其他源。
因此每个源的执行被完整包在 try/except 里，异常转成该源的 error 字段，
绝不向外抛。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .. import config
from ..crawlers import REGISTRY, ensure_registered
from ..db import connect, get_conn
from ..models import CrawlStats, RawItem
from .events_catalog import get_catalog
from .normalize import utcnow_iso
from .persist import upsert_edition, upsert_source_item

log = logging.getLogger(__name__)

# 进程内并发闸门。多 worker 部署会让每个进程各有一个锁，
# 所以 run.py 固定单 worker —— 这两件事必须配套。
_run_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_run(trigger: str, mode: str, source_keys: list[str]) -> int:
    """建批次并预置各源的 pending 行。返回 run_id。"""
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO crawl_runs (trigger, mode, status, started_at) VALUES (?, ?, 'running', ?)",
            (trigger, mode, _now_iso()),
        )
        run_id = int(cursor.lastrowid)
        for key in source_keys:
            conn.execute(
                "INSERT INTO crawl_run_items (run_id, source_key, status) VALUES (?, ?, 'pending')",
                (run_id, key),
            )
    return run_id


def active_run_id() -> int | None:
    """是否有正在跑的批次。用于 API 的 409 判断与页面刷新后接续轮询。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM crawl_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return int(row["id"]) if row else None


def reap_stale_runs() -> int:
    """启动时把上次进程异常退出留下的 running 行标记为 failed。

    不做这一步的话，残留的 running 会让之后所有的手动触发都被 409 挡掉。
    """
    with get_conn() as conn:
        stale = conn.execute("SELECT id FROM crawl_runs WHERE status = 'running'").fetchall()
        if not stale:
            return 0
        conn.execute(
            "UPDATE crawl_runs SET status = 'failed', finished_at = ? WHERE status = 'running'",
            (_now_iso(),),
        )
        conn.execute(
            """UPDATE crawl_run_items SET status = 'failed', error = '进程中断'
               WHERE status IN ('pending', 'running')"""
        )
    log.warning("清理了 %d 个中断的批次", len(stale))
    return len(stale)


def _update_run_item(run_id: int, source_key: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE crawl_run_items SET {assignments} WHERE run_id = ? AND source_key = ?",
            [*fields.values(), run_id, source_key],
        )


def _mark_source_health(source_key: str, ok: bool, error: str | None, *, note: str | None = None) -> None:
    """更新源健康状态。连续失败 3 次以上标 failing。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM sources WHERE key = ?", (source_key,)
        ).fetchone()
        failures = (row["consecutive_failures"] if row else 0) or 0

        if ok:
            conn.execute(
                """UPDATE sources
                      SET last_run_at = ?, last_success_at = ?, last_error = NULL,
                          consecutive_failures = 0,
                          health = CASE WHEN note IS NULL OR note = '' THEN 'ok' ELSE 'degraded' END
                    WHERE key = ?""",
                (_now_iso(), _now_iso(), source_key),
            )
        else:
            failures += 1
            health = "failing" if failures >= 3 else "degraded"
            if error and "登录" in error:
                health = "needs_reauth"
            conn.execute(
                """UPDATE sources
                      SET last_run_at = ?, last_error = ?, consecutive_failures = ?, health = ?
                    WHERE key = ?""",
                (_now_iso(), (error or "")[:1000], failures, health, source_key),
            )


def run_one_source(source_key: str, run_id: int, mode: str) -> CrawlStats:
    """跑单个源。任何异常都在这里被捕获并转成 stats.error。"""
    started = time.monotonic()
    stats = CrawlStats(source_key=source_key)
    _update_run_item(run_id, source_key, status="running", started_at=_now_iso())

    crawler = REGISTRY.get(source_key)
    if crawler is None:
        stats.status = "failed"
        stats.error = f"未注册的数据源: {source_key}"
        _finish(stats, run_id, started)
        return stats

    catalog = get_catalog()
    conn = connect()
    edited = 0
    try:
        for payload, title, url in crawler.fetch(mode):
            stats.items_found += 1

            item = RawItem(
                source_key=source_key,
                external_id=crawler.external_id_of(payload),
                title=title,
                url=url,
                published_at=crawler.published_at_of(payload),
                payload=payload,
                text_blob=crawler.text_blob_of(payload),
            )
            row_id, is_new, changed = upsert_source_item(conn, item)
            if is_new:
                stats.items_new += 1
            elif changed:
                stats.items_updated += 1

            # 内容没变就跳过解析与写库 —— 增量模式省下的开销主要在这里
            if changed:
                for draft in crawler.derive(payload, title, url):
                    # derive 里可能引用了标题匹配，这里兜一道，保证 slug 有效
                    if draft.event_slug and catalog.id_of(draft.event_slug) is None:
                        draft.event_slug = catalog.match(title)
                    if upsert_edition(
                        conn,
                        draft,
                        source_key=source_key,
                        source_item_id=row_id,
                        source_url=url,
                    ):
                        edited += 1

            # **每条都提交**。不批量攒着提交，是因为 fetch() 会在 yield 之间发网络请求
            # （CCPC 每条要取一次详情，约 1 秒），攒 50 条就等于把写事务开着 50 秒，
            # 其它源并发写入必然 "database is locked"。WAL + synchronous=NORMAL 下
            # 单条提交的开销可以忽略。
            conn.commit()

        conn.commit()
        stats.status = "success"
    except Exception as exc:  # noqa: BLE001 —— 单源失败绝不能影响其他源
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        stats.status = "failed"
        stats.error = f"{type(exc).__name__}: {exc}"[:1000]
        log.exception("数据源 %s 执行失败", source_key)
    finally:
        conn.close()

    stats.editions_written = edited
    _finish(stats, run_id, started)
    _mark_source_health(source_key, stats.status == "success", stats.error)
    return stats


def _finish(stats: CrawlStats, run_id: int, started: float) -> None:
    stats.duration_ms = int((time.monotonic() - started) * 1000)
    _update_run_item(
        run_id,
        stats.source_key,
        status=stats.status,
        finished_at=_now_iso(),
        duration_ms=stats.duration_ms,
        items_found=stats.items_found,
        items_new=stats.items_new,
        items_updated=stats.items_updated,
        error=stats.error,
    )


def execute_run(run_id: int, source_keys: list[str], mode: str) -> None:
    """执行批次。同步阻塞，调用方负责放到线程里跑。"""
    if not _run_lock.acquire(blocking=False):
        log.warning("已有批次在执行，跳过 run_id=%s", run_id)
        return
    try:
        results: list[CrawlStats] = []
        with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_SOURCES) as pool:
            futures = {
                pool.submit(run_one_source, key, run_id, mode): key for key in source_keys
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 —— 兜底，run_one_source 理论上不会抛
                    log.exception("源 %s 的 future 抛出异常", key)
                    results.append(CrawlStats(source_key=key, status="failed", error=str(exc)[:500]))

        total_new = sum(r.items_new for r in results)
        total_updated = sum(r.items_updated for r in results)
        failed = [r for r in results if r.status == "failed"]
        succeeded = [r for r in results if r.status == "success"]

        if not failed:
            status = "success"
        elif succeeded:
            status = "partial"
        else:
            status = "failed"

        with get_conn() as conn:
            conn.execute(
                """UPDATE crawl_runs
                      SET status = ?, finished_at = ?, total_new = ?, total_updated = ?
                    WHERE id = ?""",
                (status, _now_iso(), total_new, total_updated, run_id),
            )
        log.info(
            "批次 %s 完成：%s，新增 %d，更新 %d，失败 %d 个源",
            run_id, status, total_new, total_updated, len(failed),
        )
    finally:
        _run_lock.release()


def run_all_sources(mode: str = "incremental", trigger: str = "manual",
                    source_keys: list[str] | None = None) -> int:
    """建批次并同步执行到底。供调度器与后台线程调用。"""
    ensure_registered()
    keys = source_keys or list(REGISTRY.keys())
    run_id = create_run(trigger, mode, keys)
    execute_run(run_id, keys, mode)
    return run_id
