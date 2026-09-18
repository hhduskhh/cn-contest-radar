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
    # synchronous 是**连接级**设置。写在 init_db() 里只对那一条连接生效，
    # 爬虫、调度器、网页各自新开的连接仍是默认的 FULL —— 每次 commit 都 fsync，
    # 而爬虫是每条记录一提交。WAL 下 NORMAL 足够安全（只有断电才可能丢最后
    # 几个事务，缓存型数据库重爬即可），所以每条连接都设一遍。
    conn.execute("PRAGMA synchronous=NORMAL")
    # 注意：**不要**在这里执行 PRAGMA journal_mode=WAL。
    # 它是数据库级持久设置，但每次执行都需要短暂独占锁 —— 多个爬虫线程
    # 并发建连接时会互相抢锁，直接报 "database is locked"。
    # 好在它是持久的：WAL 只在 init_db() 里设一次，之后所有连接都享受。
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
        # （synchronous 相反：连接级、非持久，已挪到 connect() 里逐条设置。）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema)


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
