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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.api.routes_events import upcoming  # noqa: E402
from app.api.serialize import edition_to_dict  # noqa: E402
from app.crawlers import ensure_registered  # noqa: E402
from app.crawlers.base import BaseCrawler  # noqa: E402
from app.crawlers.guards import SourceStructureError, expect_first_page  # noqa: E402
from app.models import EditionDraft  # noqa: E402
from app.pipeline import events_catalog, normalize, persist, runner  # noqa: E402
from app.pipeline.status import effective_status  # noqa: E402


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


class _FlakyCrawler(BaseCrawler):
    """带一条坏数据的假源，不联网。"""

    key = "flaky"
    name = "测试源"
    kind = "structured"
    base_url = "http://example.invalid/"

    payloads: list = []

    def fetch(self, mode):
        for payload in self.payloads:
            yield payload, payload["title"], None

    def external_id_of(self, payload):
        return str(payload["id"])

    def derive(self, payload, title, url):
        if payload.get("boom"):
            raise ValueError("字段残缺")
        return [EditionDraft(event_slug=None, year=2026, title=title)]


class PerItemIsolationTest(TempDbTest):
    """缺陷 3：单条解析失败不能丢掉该源剩下的条目。"""

    def setUp(self) -> None:
        super().setUp()
        self.crawler = _FlakyCrawler()
        self.crawler.payloads = [
            {"id": 1, "title": "第一条"},
            {"id": 2, "title": "第二条", "boom": True},
            {"id": 3, "title": "第三条"},
        ]
        runner.REGISTRY[self.crawler.key] = self.crawler
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO sources (key, name, kind) VALUES (?, ?, 'structured')",
                (self.crawler.key, self.crawler.name),
            )

    def tearDown(self) -> None:
        runner.REGISTRY.pop(self.crawler.key, None)
        super().tearDown()

    def _run(self) -> runner.CrawlStats:
        run_id = runner.create_run("manual", "incremental", [self.crawler.key])
        return runner.run_one_source(self.crawler.key, run_id, "incremental")

    def test_bad_item_is_skipped_not_fatal(self) -> None:
        stats = self._run()

        self.assertEqual(stats.status, "success")
        self.assertEqual(stats.items_found, 3)
        self.assertEqual(stats.items_failed, 1)
        self.assertEqual(stats.items_new, 2)

        ids = sorted(
            r["external_id"]
            for r in self.query(
                "SELECT external_id FROM source_items WHERE source_key = ?",
                (self.crawler.key,),
            )
        )
        # 坏条目之后的那条仍在库里。修复前整源一个 try，这里只剩 ["1"]
        self.assertEqual(ids, ["1", "3"])

    def test_failed_item_marks_source_degraded(self) -> None:
        self._run()

        row = self.query("SELECT * FROM sources WHERE key = ?", (self.crawler.key,))[0]
        self.assertEqual(row["health"], "degraded")
        self.assertIn("1 条解析失败", row["last_error"])
        # 单条失败不算源失败，不该累积连续失败次数
        self.assertEqual(row["consecutive_failures"], 0)

    def test_clean_run_restores_health(self) -> None:
        self._run()
        self.crawler.payloads = [p for p in self.crawler.payloads if not p.get("boom")]
        self._run()

        row = self.query("SELECT * FROM sources WHERE key = ?", (self.crawler.key,))[0]
        self.assertEqual(row["health"], "ok")
        self.assertIsNone(row["last_error"])


class UpcomingWindowTest(TempDbTest):
    """缺陷 4：upcoming 的时间边界必须带时分秒，不能只比到天。"""

    def setUp(self) -> None:
        super().setUp()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO events (id, slug, name, category)"
                " VALUES (1, 'demo', '演示赛', 'algorithm')"
            )
        self.now = datetime.now(timezone.utc)

    def _add_edition(self, edition_id: int, **times) -> None:
        stamp = normalize.utcnow_iso()
        with db.get_conn() as conn:
            conn.execute(
                """INSERT INTO event_editions
                     (id, event_id, year, sub_event, origin, source_key,
                      registration_end, contest_start, first_seen_at, updated_at)
                   VALUES (?, 1, 2026, ?, 'crawl', 'demo', ?, ?, ?, ?)""",
                (
                    edition_id,
                    f"s{edition_id}",
                    normalize.to_utc_iso(times["registration_end"])
                    if times.get("registration_end")
                    else None,
                    normalize.to_utc_iso(times["contest_start"])
                    if times.get("contest_start")
                    else None,
                    stamp,
                    stamp,
                ),
            )

    def _upcoming_ids(self, days: int = 90) -> set[int]:
        result = upcoming(days=days, limit=30, region_filter=None)
        return {row["id"] for row in result["editions"]}

    def test_deadline_earlier_today_is_excluded(self) -> None:
        """今天已经过去的截止时刻不能被算成"即将截止"。

        修复前它和 datetime('now') 比到第 10 个字符就分出大小（'T' > ' '），
        被当成还没到，于是过期的报名截止照样挂在红榜上。
        """
        today_start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        if today_start >= self.now:
            self.skipTest("跑在 UTC 当天零点，构造不出当天更早的时刻")

        self._add_edition(1, registration_end=today_start)
        self.assertNotIn(1, self._upcoming_ids())

    def test_deadline_within_window_is_included(self) -> None:
        self._add_edition(2, registration_end=self.now + timedelta(days=3))
        self.assertIn(2, self._upcoming_ids())

    def test_last_day_of_window_is_not_dropped(self) -> None:
        """窗口最后一天不能因空格 vs 'T' 的差异被整日排除。"""
        self._add_edition(3, registration_end=self.now + timedelta(days=90))
        self.assertIn(3, self._upcoming_ids())

    def test_beyond_window_is_excluded(self) -> None:
        self._add_edition(4, contest_start=self.now + timedelta(days=200))
        self.assertNotIn(4, self._upcoming_ids())


class EffectiveStatusTest(unittest.TestCase):
    """缺陷 5：状态不能爬取时算一次就冻住，读取时要按当前时间纠正。"""

    NOW = "2026-09-18T10:00:00Z"

    def test_stored_running_but_contest_ended(self) -> None:
        """实测那 68 条的形态：比赛早打完了，状态还挂着进行中。"""
        record = {"status": "running", "contest_end": "2026-06-01T00:00:00Z"}
        self.assertEqual(effective_status(record, now=self.NOW), "finished")

    def test_registration_closed_without_contest_time(self) -> None:
        record = {"status": "registering", "registration_end": "2026-08-01T00:00:00Z"}
        self.assertEqual(effective_status(record, now=self.NOW), "finished")

    def test_ongoing_keeps_stored_status(self) -> None:
        """还在报名/还在比赛的，不能被改掉。"""
        running = {
            "status": "running",
            "contest_start": "2026-09-01T00:00:00Z",
            "contest_end": "2026-10-01T00:00:00Z",
        }
        self.assertEqual(effective_status(running, now=self.NOW), "running")

        registering = {"status": "registering", "registration_end": "2026-10-01T00:00:00Z"}
        self.assertEqual(effective_status(registering, now=self.NOW), "registering")

    def test_unknown_status_is_not_invented(self) -> None:
        """时间待公布的行不该被硬塞一个状态。"""
        self.assertIsNone(effective_status({"status": None}, now=self.NOW))
        self.assertEqual(effective_status({"status": "upcoming"}, now=self.NOW), "upcoming")

    def test_read_path_applies_it(self) -> None:
        """收口点在 edition_to_dict —— 那条 68 条的路径。"""
        row = {"id": 1, "status": "running", "contest_end": "2020-01-01T00:00:00Z"}
        self.assertEqual(edition_to_dict(row)["status"], "finished")


class DeriveStatusTest(unittest.TestCase):
    """入库时的推导逻辑与改前一致（只是换成了 ISO 串实现）。"""

    NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)

    def _derive(self, **kwargs) -> str | None:
        draft = EditionDraft(event_slug="demo", year=2026, **kwargs)
        return persist.derive_status(draft, now=self.NOW)

    def test_same_rules_as_before(self) -> None:
        cases = [
            (dict(registration_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
                  contest_end=datetime(2026, 8, 10, tzinfo=timezone.utc)), "finished"),
            (dict(registration_end=datetime(2026, 8, 1, tzinfo=timezone.utc)), "finished"),
            (dict(contest_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                  contest_end=datetime(2026, 10, 1, tzinfo=timezone.utc)), "running"),
            (dict(registration_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                  registration_end=datetime(2026, 10, 1, tzinfo=timezone.utc)), "registering"),
            (dict(contest_start=datetime(2026, 10, 1, tzinfo=timezone.utc)), "upcoming"),
            (dict(registration_end=datetime(2026, 10, 1, tzinfo=timezone.utc)), "upcoming"),
            (dict(), None),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self._derive(**kwargs), expected)

    def test_draft_status_wins(self) -> None:
        """源自带的 status 优先于按时间推的。"""
        draft = EditionDraft(
            event_slug="demo",
            year=2026,
            status="running",
            contest_end=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(persist.derive_status(draft, now=self.NOW), "running")


class EditionMergeTest(TempDbTest):
    """缺陷 6：同一届次被同源的多次解析先后写入时，低置信度不能覆盖高置信度。"""

    def setUp(self) -> None:
        super().setUp()
        self._saved_catalog = events_catalog._catalog
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO events (id, slug, name, category)"
                " VALUES (1, 'demo', '演示赛', 'algorithm')"
            )
        events_catalog.reload_catalog()

    def tearDown(self) -> None:
        events_catalog._catalog = self._saved_catalog
        super().tearDown()

    def _write(self, *, origin: str = "crawl", **draft_fields) -> None:
        """按同一唯一键写一条届次（source_key/sub_event/year 固定）。"""
        draft = EditionDraft(event_slug="demo", year=2026, **draft_fields)
        with db.get_conn() as conn:
            persist.upsert_edition(
                conn,
                draft,
                source_key="demo",
                source_item_id=None,
                source_url=None,
                origin=origin,
            )

    def _row(self) -> dict:
        rows = self.query("SELECT * FROM event_editions")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_low_confidence_does_not_downgrade_high(self) -> None:
        self._write(
            time_confidence="high",
            time_precision="second",
            contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            contest_end=datetime(2026, 5, 3, tzinfo=timezone.utc),
        )
        # 同一届次的另一条通知：正则一条时间都没抽到，置信度 low
        self._write(time_confidence="low")

        row = self._row()
        self.assertEqual(row["time_confidence"], "high")
        self.assertEqual(row["time_precision"], "second")
        self.assertEqual(row["contest_start"], "2026-05-01T00:00:00Z")

    def test_higher_confidence_still_updates(self) -> None:
        """只升不降不等于不更新：更高置信度的解析照常覆盖。"""
        self._write(
            time_confidence="low",
            contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        self._write(
            time_confidence="high",
            time_precision="second",
            contest_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

        row = self._row()
        self.assertEqual(row["time_confidence"], "high")
        self.assertEqual(row["time_precision"], "second")
        self.assertEqual(row["contest_start"], "2026-06-01T00:00:00Z")

    def test_low_confidence_can_still_fill_missing_time(self) -> None:
        """合并仍按字段走：高置信度没给出的时间，低置信度的可以补上。"""
        self._write(
            time_confidence="high",
            contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        self._write(
            time_confidence="low",
            registration_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )

        row = self._row()
        self.assertEqual(row["registration_end"], "2026-04-01T00:00:00Z")
        self.assertEqual(row["time_confidence"], "high")

    def test_region_not_downgraded_to_online(self) -> None:
        self._write(
            region="guangdong",
            contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        # 后一条通知的赛站没认出来，落到兜底值 online
        self._write(
            region="online",
            contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(self._row()["region"], "guangdong")

    def test_region_updates_to_concrete_value(self) -> None:
        """真判出了别的赛站还是要更新，不能一律守着旧值。"""
        self._write(region="online", contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self._write(region="other", contest_start=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.assertEqual(self._row()["region"], "other")


if __name__ == "__main__":
    unittest.main()
