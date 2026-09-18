"""后端缺陷修复的回归测试。

只用标准库 unittest（项目不依赖 pytest）。在仓库根目录运行：

    python -m unittest discover -s tests -v

每个用例跑在自己的临时 SQLite 库上：config.DB_PATH 是每次 connect() 现读的，
所以在 setUp 里改掉它就够了，不会碰到 data/app.db。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.crawlers import ensure_registered  # noqa: E402
from app.crawlers.guards import SourceStructureError, expect_first_page  # noqa: E402
from app.pipeline import runner  # noqa: E402


class TempDbTest(unittest.TestCase):
    """临时库基类。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        db.init_db()

    def tearDown(self) -> None:
        config.DB_PATH = self._orig_db_path
        self._tmp.cleanup()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with db.get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]


class RunLockTest(TempDbTest):
    """缺陷 1：抢不到执行锁时，批次不能留在 running。"""

    def test_lock_contention_marks_run_failed(self) -> None:
        run_id = runner.create_run("manual", "incremental", ["fake"])

        # 占住执行锁，模拟"已有批次在跑"
        self.assertTrue(runner._run_lock.acquire(blocking=False))
        try:
            runner.execute_run(run_id, ["fake"], "incremental")

            run = self.query("SELECT * FROM crawl_runs WHERE id = ?", (run_id,))[0]
            self.assertEqual(run["status"], "failed")
            self.assertIsNotNone(run["finished_at"])

            items = self.query("SELECT * FROM crawl_run_items WHERE run_id = ?", (run_id,))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["status"], "failed")
            self.assertIn("已有批次", items[0]["error"])

            # 关键：没有在别人持锁时把锁释放掉（否则并发批次会同时跑）
            self.assertTrue(runner._run_lock.locked())
        finally:
            runner._run_lock.release()

    def test_failed_run_no_longer_blocks_new_runs(self) -> None:
        """被判失败的批次不能继续挡着后续触发。"""
        first = runner.create_run("manual", "incremental", ["fake"])
        self.assertTrue(runner._run_lock.acquire(blocking=False))
        try:
            runner.execute_run(first, ["fake"], "incremental")
        finally:
            runner._run_lock.release()

        self.assertIsNone(runner.active_run_id())


class _StubHttp:
    """假 HTTP 层：不联网，按给定内容作答。"""

    def __init__(self, *, json_payload=None, text: str = "") -> None:
        self._json = json_payload if json_payload is not None else {}
        self._text = text

    def get_json(self, url, **kwargs):
        return self._json

    def get_text(self, url, **kwargs):
        return self._text


# datafountain 的列表在 Nuxt payload 里，得给它一段能解析出 competitions:[] 的 HTML
_NUXT_EMPTY = (
    "window.__NUXT__=(function(a){return {data:[{competitions:[]}]}}(1))</script>"
)
_EMPTY_HTML_BY_SOURCE = {"datafountain": _NUXT_EMPTY}


class FirstPageGuardTest(TempDbTest):
    """缺陷 2：首页取空必须抛错，不能静默当成"成功，0 条"。"""

    def test_empty_first_page_raises_for_every_source(self) -> None:
        registry = ensure_registered()
        self.assertEqual(len(registry), 9)
        for key, crawler in sorted(registry.items()):
            with self.subTest(source=key):
                crawler.http = _StubHttp(text=_EMPTY_HTML_BY_SOURCE.get(key, ""))
                with self.assertRaises(SourceStructureError) as ctx:
                    list(crawler.fetch("incremental"))
                self.assertIn(crawler.name, str(ctx.exception))

    def test_later_pages_may_still_be_empty(self) -> None:
        """次页起取空是正常翻到底，必须照常放行。"""
        for page in (2, 7):
            self.assertEqual(list(expect_first_page([], source="x", page=page)), [])

    def test_non_empty_first_page_passes_through(self) -> None:
        """防线不能误伤正常数据。"""
        crawler = ensure_registered()["raicom"]
        crawler.http = _StubHttp(
            json_payload={"data": [{"id": 1, "matchname": "睿抗机器人开发者大赛"}]}
        )
        self.assertEqual(len(list(crawler.fetch("incremental"))), 1)


if __name__ == "__main__":
    unittest.main()
