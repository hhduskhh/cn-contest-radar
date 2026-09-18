"""蓝桥杯全国软件和信息技术专业人才大赛（B 类：通知列表 + 关键词抽取）。

接口：https://www.guoxinlanqiao.com/api/news/find?status=1&project=dasai&progid=20&pageno=N&pagesize=50
实测要点：
  - **API 域名是 guoxinlanqiao.com（国信蓝桥，官方后端），不是 lanqiao.cn**
  - 列表键是 datalist，total 在顶层（738 条）
  - pagesize 上限 50；传 100 会返回被截断的坏 JSON
  - creatTime 形如 '2026-09-10T06:22:27'，**不带时区**，按 Asia/Shanghai 解释
  - 无详情接口（/api/news/detail 等均 404），正文靠 synopsis 摘要

正文只有摘要，所以时间抽取的召回率有限 —— 这是 B 类源的固有代价。
抽不到时间不影响落库：那条记录会显示为「时间待公布」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline import extract
from ..pipeline.normalize import infer_year, parse_any_datetime, year_from_edition
from ..pipeline.textutil import clean_text
from .base import BaseCrawler

_PAGE_SIZE = 50
_PROGID = 20  # 大赛通知栏目

# 蓝桥杯「第 N 届」= N + 2009 年（第 12 届 = 2021，第 17 届 = 2026，第 18 届 = 2027）。
# 这个换算比发布时间可靠：头年 10 月发的报名通知讲的是次年的比赛，
# 用发布年份会把整届的时间线错位一年。
_EDITION_OFFSET = 2009


class LanqiaoCrawler(BaseCrawler):
    key = "lanqiao"
    name = "蓝桥杯"
    kind = "news"
    base_url = "https://dasai.lanqiao.cn/"
    rate_limit_seconds = 1.5
    max_pages = 20

    _API = "https://www.guoxinlanqiao.com/api/news/find"

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        for page in range(1, self.max_pages + 1):
            data = self.http.get_json(
                self._API,
                params={
                    "status": 1,
                    "project": "dasai",
                    "progid": _PROGID,
                    "pageno": page,
                    "pagesize": _PAGE_SIZE,
                },
            )
            items = data.get("datalist") or []
            if not items:
                break
            for item in items:
                yield item, item.get("title") or "", self.url_of(item)

    def external_id_of(self, payload: Any) -> str:
        return str(payload.get("nnid"))

    def title_of(self, payload: Any) -> str:
        return payload.get("title") or ""

    def url_of(self, payload: Any) -> str | None:
        # 官方通知详情页。格式与高校转引的官方链接一致（如 dasai.lanqiao.cn/notices/1568/）。
        # 注意：官网是 SPA，对任何 ID 都返回 200，所以无法用 HTTP 状态码验证某条 ID 是否存在。
        # nnid 是通知的自然主键，映射关系合理但未逐条核实——若点开是空页面，
        # 用 extra 里的文号（如「蓝桥杯组委会字〔2026〕47号」）在官网搜索原文。
        nnid = payload.get("nnid")
        if not nnid:
            return self.base_url
        return f"https://dasai.lanqiao.cn/notices/{nnid}"

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("publishTime") or payload.get("creatTime"))
        return parsed.utc if parsed else None

    def text_blob_of(self, payload: Any) -> str | None:
        return clean_text(payload.get("synopsis"))

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        # 通知列表里混着招聘会、培训认证、文创设计等非赛事内容
        if not extract.is_competition_notice(title):
            return []

        synopsis = clean_text(payload.get("synopsis"))
        published = self.published_at_of(payload)
        # 标题常用「第十八届」而非年份。优先按届次号换算，其次才退回发布时间。
        year = year_from_edition(title, _EDITION_OFFSET) or infer_year(
            title, synopsis, published
        )
        blob = f"{title}\n{synopsis}"

        reg_start, reg_end, reg_raw = extract.extract_registration(
            blob, default_year=year, published_at=published
        )
        con_start, con_end, con_raw = extract.extract_contest(
            blob, default_year=year, published_at=published
        )

        return [
            EditionDraft(
                event_slug="lanqiao",
                year=year,
                sub_event=self._sub_event(title),
                registration_start=reg_start.utc if reg_start else None,
                registration_end=reg_end.utc if reg_end else None,
                contest_start=con_start.utc if con_start else None,
                contest_end=con_end.utc if con_end else None,
                registration_raw=reg_raw,
                contest_raw=con_raw,
                time_confidence="low",
                time_precision="date",
                extra={
                    "notice_no": self._notice_no(synopsis),
                    "author": payload.get("author"),
                    "programa": payload.get("programaName"),
                },
            )
        ]

    @staticmethod
    def _notice_no(synopsis: str) -> str | None:
        """提取文号（如「蓝桥杯组委会字〔2026〕47号」），便于用户去官网核对原文。"""
        import re

        match = re.search(r"〔\d{4}〕\s*\d+\s*号", synopsis)
        return match.group(0) if match else None

    @staticmethod
    def _sub_event(title: str) -> str:
        """蓝桥杯有多个专项赛/赛道，分开展示避免混成一个。"""
        for keyword, label in (
            ("视觉艺术", "视觉艺术设计赛"),
            ("文创", "文创设计专项赛"),
            ("人工智能", "人工智能专项赛"),
            ("HarmonyOS", "HarmonyOS 专项赛"),
            ("嵌入式", "嵌入式专项赛"),
            ("网络安全", "网络安全专项赛"),
            ("研究生", "研究生组"),
            ("省赛", "省赛"),
            ("国赛", "国赛"),
        ):
            if keyword in title:
                return label
        return ""
