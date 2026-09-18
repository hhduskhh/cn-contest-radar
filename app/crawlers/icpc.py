"""ICPC 国际大学生程序设计竞赛（亚洲区域赛，北大总部公告）。

站点：https://icpc.pku.edu.cn/tzgg/

实测要点：
  - **服务端渲染的静态 HTML**，北大网站群 CMS，requests + 正则即可，不需要浏览器
  - 分页是 index.htm / index1.htm / index2.htm …（index6 起为空）
  - 列表项结构：<li><a href="HASH.htm">标题<span class="s-dt">[2026-09-10]</span></a></li>
    标题与日期都在 HTML 里，日期用方括号包着
  - 标题含赛站（西安/武汉/南京/沈阳/南昌/成都/上海/港澳/深圳），是判地域的依据
  - detail 页正文里有赛程细节，按预算取一部分补时间

ICPC 的赛季跨年（第 51 届 = 2026-2027 赛季），届次换算用 offset=1975。
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Any, Iterator

from ..models import EditionDraft
from ..pipeline import extract, region
from ..pipeline.normalize import parse_any_datetime, year_from_edition
from ..pipeline.textutil import strip_html
from .base import BaseCrawler

log = logging.getLogger(__name__)

_BASE = "https://icpc.pku.edu.cn/tzgg/"
_MAX_PAGES = 8
# 详情页正文实测为空 —— 通知的实际内容都放在 PDF 附件里（如「邀请函.pdf」），
# HTML 里只有标题。所以只留很小的预算偶尔碰一下，不做无谓的请求。
_DETAIL_BUDGET = 8
_EDITION_OFFSET = 1975  # 第 51 届 = 2026

_RE_ITEM = re.compile(
    r'<li>\s*<a[^>]*href="([^"]+\.htm)"[^>]*>(.*?)</a>\s*</li>', re.S
)
_RE_LIST_BLOCK = re.compile(r'<ul class="item-list">(.*?)</ul>', re.S)
_RE_DATE_SPAN = re.compile(r'<span class="s-dt">\s*\[?([\d-]+)\]?\s*</span>', re.S)
_RE_CONTENT = re.compile(r'<div class="article">(.*?)</div>\s*</div>', re.S)


class ICPCCrawler(BaseCrawler):
    key = "icpc"
    name = "ICPC 亚洲区域赛"
    kind = "news"
    base_url = _BASE
    rate_limit_seconds = 1.5
    max_pages = _MAX_PAGES
    has_location = True

    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        known = self.known_external_ids() if mode == "incremental" else set()
        detail_budget = _DETAIL_BUDGET
        index = 0

        while index < self.max_pages:
            url = _BASE if index == 0 else f"{_BASE}index{index}.htm"
            try:
                page = self.http.get_text(url)
            except Exception as exc:  # noqa: BLE001 —— 翻到底了就别报错
                log.debug("ICPC 第 %d 页取不到，视为结束：%s", index, exc)
                break

            items = self._parse_list(page)
            if not items:
                break

            for item in items:
                if item["is_new"] or mode == "backfill":
                    if detail_budget > 0:
                        detail_budget -= 1
                        body = self._fetch_detail(item["url"])
                        if body:
                            item["content"] = body
                yield item, item["title"], item["url"]

            index += 1

    def _parse_list(self, page: str) -> list[dict]:
        known = self.known_external_ids()
        out: list[dict] = []
        # 只在文章列表区块里找：整页还有导航菜单，那些 <li><a> 会被同一个正则误捞进来
        block = _RE_LIST_BLOCK.search(page)
        scope = block.group(1) if block else page
        for href, inner in _RE_ITEM.findall(scope):
            # 日期在 <span class="s-dt"> 里，先摘出去再取标题
            date_match = _RE_DATE_SPAN.search(inner)
            published = date_match.group(1) if date_match else None
            title = html.unescape(_RE_DATE_SPAN.sub("", inner))
            title = re.sub(r"<[^>]+>", "", title).strip()
            if not title or len(title) < 6:
                continue
            full_url = href if href.startswith("http") else _BASE + href.lstrip("./")
            out.append(
                {
                    "id": href.rsplit("/", 1)[-1],
                    "title": title,
                    "url": full_url,
                    "published": published,
                    "is_new": href.rsplit("/", 1)[-1] not in known,
                    "content": None,
                }
            )
        return out

    def _fetch_detail(self, url: str) -> str | None:
        try:
            page = self.http.get_text(url)
        except Exception as exc:  # noqa: BLE001
            log.debug("ICPC 详情取不到 %s：%s", url, exc)
            return None
        match = _RE_CONTENT.search(page)
        segment = match.group(1) if match else page
        return strip_html(segment)[:8000]

    def external_id_of(self, payload: Any) -> str:
        return payload["id"]

    def title_of(self, payload: Any) -> str:
        return payload["title"]

    def url_of(self, payload: Any) -> str | None:
        return payload["url"]

    def published_at_of(self, payload: Any) -> datetime | None:
        parsed = parse_any_datetime(payload.get("published"))
        return parsed.utc if parsed else None

    def text_blob_of(self, payload: Any) -> str | None:
        return payload.get("content")

    def derive(self, payload: Any, title: str, url: str | None) -> list[EditionDraft]:
        if not extract.is_competition_notice(title):
            return []

        body = payload.get("content") or ""
        published = self.published_at_of(payload)
        year = year_from_edition(title, _EDITION_OFFSET) or (
            published.astimezone(_tz()).year if published else None
        )
        if year is None:
            return []

        station = self._station(title)
        blob = f"{title}\n{body}"
        reg_start, reg_end, reg_raw = extract.extract_registration(
            blob, default_year=year, published_at=published
        )
        con_start, con_end, con_raw = extract.extract_contest(
            blob, default_year=year, published_at=published
        )

        return [
            EditionDraft(
                event_slug="icpc",
                year=year,
                sub_event=station,
                registration_start=reg_start.utc if reg_start else None,
                registration_end=reg_end.utc if reg_end else None,
                contest_start=con_start.utc if con_start else None,
                contest_end=con_end.utc if con_end else None,
                registration_raw=reg_raw,
                contest_raw=con_raw,
                time_confidence="low",
                time_precision="date",
                region=region.classify(True, station, title),
                extra={"notice": title},
            )
        ]

    @staticmethod
    def _station(title: str) -> str:
        """从标题括号里提赛站，如「亚洲区域赛（西安）」「全国邀请赛（深圳）」。"""
        if "网络预选赛" in title or "网络赛" in title:
            return "网络预选赛"
        if "EC-Final" in title or "EC 区域赛" in title or "东亚" in title:
            return "EC-Final"
        match = re.search(r"[（(]([^）)]{2,10})[）)]", title)
        if match:
            candidate = match.group(1)
            # 括号里可能不是赛站：「第51届」这类届次号、或「ICPC」这类纯英文缩写，
            # 都不是举办地。赛站名一律是中文（深圳/西安/港澳…）。
            if re.search(r"^\d|届$", candidate):
                return ""
            if not re.search(r"[一-龥]", candidate):
                return ""
            return candidate
        return ""


def _tz():
    from .. import config

    return config.TZ
