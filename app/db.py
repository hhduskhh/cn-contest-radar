"""SQLite 连接工厂与初始化。

用标准库 sqlite3 而非 ORM：表结构固定、无迁移需求，裸 SQL 更透明。
WAL 模式让调度器写入与网页读取不互斥；busy_timeout 避免 Windows 上瞬时 database is locked。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    # 注意：**不要**在这里执行 PRAGMA journal_mode=WAL。
    # 它是数据库级持久设置，但每次执行都需要短暂独占锁 —— 多个爬虫线程
    # 并发建连接时会互相抢锁，直接报 "database is locked"。
    # WAL 只在 init_db() 里设一次。
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """短连接上下文：自动提交/回滚并关闭。爬虫是长任务，绝不长期持有连接。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表建视图。幂等，可反复调用。"""
    config.ensure_dirs()
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        # WAL 让调度器写入与网页读取不互斥。数据库级持久设置，设一次即可 ——
        # 放在这里而不是 connect()，是因为它需要独占锁。
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL 下 NORMAL 是安全的：只有断电才可能丢最后几个事务，
        # 而这是缓存型数据库，重爬即可。换来的是提交时不做 fsync ——
        # 爬虫每个条目都提交，这个差别很大。
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(schema)


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
