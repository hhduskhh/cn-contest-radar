"""BaseCrawler 抽象与统一 HTTP 层。

设计要点：A 类（结构化接口）与 B 类（新闻列表）在 fetch 层完全一致，
只在 derive 层分叉，统一产出 EditionDraft。因此 persist 层不需要知道
数据来自结构化字段还是正则抽取。
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

import requests

from .. import config

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HttpClient:
    """带限速与重试的同步 HTTP 客户端。每个 crawler 持有一个实例。

    限速按实例而非按域名统计：各 crawler 对应不同站点，一个实例即一个域名。
    """

    def __init__(
        self,
        *,
        rate_limit_seconds: float = config.RATE_LIMIT_SECONDS,
        referer: str | None = None,
    ) -> None:
        self.rate_limit_seconds = rate_limit_seconds
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "application/json, text/html, */*;q=0.8",
            }
        )
        if referer:
            self.session.headers["Referer"] = referer

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.rate_limit_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        cookies: dict | None = None,
    ) -> requests.Response:
        """发起请求，自带限速、超时、指数退避重试、429 Retry-After 处理。"""
        last_exc: Exception | None = None
        for attempt in range(config.HTTP_RETRIES):
            self._throttle()
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=config.HTTP_TIMEOUT,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    log.warning("被限流 %s，等待 %ss", url, retry_after)
                    time.sleep(min(retry_after, 60))
                    continue
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001 —— 重试所有异常，最后一轮抛给调用方
                last_exc = exc
                if attempt < config.HTTP_RETRIES - 1:
                    backoff = 2**attempt
                    log.debug("请求失败（第 %d 次）%s: %s，%ds 后重试", attempt + 1, url, exc, backoff)
                    time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def get_json(self, url: str, **kwargs) -> Any:
        resp = self.request("GET", url, **kwargs)
        # 显式用 content 解码再 loads：部分站点 Content-Type 声明与实际编码不符，
        # 中文长文本依赖 requests 自动推断会出问题。
        return json.loads(resp.content.decode("utf-8", errors="replace"))

    def get_text(self, url: str, *, encoding: str | None = None, **kwargs) -> str:
        resp = self.request("GET", url, **kwargs)
        return decode_body(resp.content, resp.headers.get("Content-Type"), encoding)


def decode_body(content: bytes, content_type: str | None, forced: str | None = None) -> str:
    """稳健的正文解码。

    不能直接信 requests 的 resp.text：当 Content-Type 是 text/html 但没带 charset 时，
    requests 会按 RFC 2616 默认用 ISO-8859-1，中文页面就会整篇乱码
    （表现为「第51届」变成「ç¬¬51å±」），后续的标题匹配和年份推断会静默全部失败。

    策略：先看响应头声明 → 再试 UTF-8 → 再试 GB18030（覆盖 GBK/GB2312）→ 最后兜底。
    """
    if forced:
        return content.decode(forced, errors="replace")

    declared = None
    if content_type and "charset=" in content_type.lower():
        declared = content_type.lower().split("charset=", 1)[1].split(";")[0].strip().strip('"\'')
        # requests 对无 charset 的 text/* 会填 iso-8859-1，那不是站点真的声明，忽略它
        if declared in ("iso-8859-1", "latin-1", "latin1"):
            declared = None

    if declared:
        try:
            return content.decode(declared)
        except (LookupError, UnicodeDecodeError):
            pass

    for candidate in ("utf-8", "gb18030"):
        try:
            return content.decode(candidate)
        except UnicodeDecodeError:
            continue

    return content.decode("utf-8", errors="replace")


class BaseCrawler(ABC):
    key: str
    name: str
    kind: Literal["structured", "news"]
    base_url: str
    referer: str | None = None
    rate_limit_seconds: float = config.RATE_LIMIT_SECONDS
    max_pages: int = 60  # 防失控上限，任何翻页循环都不得突破

    # 该源的数据口径说明，会展示给用户（例如讯飞的"数据不完整"）
    note: str | None = None

    # 该源的赛事是否有线下举办地。False（各竞赛平台）的届次一律标 online，
    # 不会被"只看广东"的筛选误伤。
    has_location: bool = False

    def __init__(self) -> None:
        self.http = HttpClient(
            rate_limit_seconds=self.rate_limit_seconds,
            referer=self.referer or self.base_url,
        )

    # ---- 子类实现这两件事 ----

    @abstractmethod
    def fetch(self, mode: str) -> Iterator[tuple[Any, str, str | None]]:
        """产出 (payload, title, url) 三元组。

        payload 是该条记录的原始数据（dict 或 list），persist 层会原样存进 raw_json。
        external_id 由 external_id_of() 从 payload 推导，保证同一源的幂等性。
        mode 为 'incremental' 或 'backfill'。"""

    @abstractmethod
    def external_id_of(self, payload: Any) -> str:
        """从 payload 推导该源内的唯一标识。"""

    @abstractmethod
    def derive(self, payload: Any, title: str, url: str | None) -> list:
        """把一条原始记录转成 EditionDraft 列表。

        A 类：字段直接映射，time_confidence='high'。
        B 类：调 extract.py 正则抽取，time_confidence='low'；
              抽不到时间也要返回 draft（四个时间字段为 None），不要丢弃记录。"""

    # ---- 供子类覆盖的钩子 ----

    def title_of(self, payload: Any) -> str:
        return ""

    def url_of(self, payload: Any) -> str | None:
        return None

    def published_at_of(self, payload: Any) -> datetime | None:
        return None

    def text_blob_of(self, payload: Any) -> str | None:
        """B 类源返回正文/摘要纯文本，供关键词抽取。A 类通常不需要。"""
        return None

    # ---- 基类统一实现 ----

    def known_external_ids(self) -> set[str]:
        """本源已入库的 external_id 集合。翻页时用它做增量短路。"""
        from ..db import get_conn

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT external_id FROM source_items WHERE source_key = ?", (self.key,)
            ).fetchall()
        return {str(r["external_id"]) for r in rows}

    def health_probe(self) -> tuple[bool, str | None]:
        """轻量探测：只打一个最小请求判断源是否还活着。"""
        try:
            next(iter(self.fetch("incremental")), None)
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:500]
