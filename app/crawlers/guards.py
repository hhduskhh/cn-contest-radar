"""源的首页防线。

各源的翻页循环都写成了「取到空页就 break」。对**第二页起**这是对的 ——
翻到底了就该停。但**首页**取空完全是另一回事：接口改版、被拦、返回空壳
JSON 都会让首页为空，而调用方看到的是「成功，0 条」——批次照记 success，
源健康度照记 ok。等人工发现时，数据可能已经空了几周，且每次爬取都在
把「上次的好数据」当历史留着，看不出异常。

所以首页空一律抛错，让该源当次标红；次页起为空仍按翻到底处理。
"""

from __future__ import annotations

from typing import Sequence, TypeVar

T = TypeVar("T")


class SourceStructureError(RuntimeError):
    """源返回的数据结构不符合预期（改版、空壳、被拦截）。"""


def expect_first_page(
    items: Sequence[T], *, source: str, page: int = 1
) -> Sequence[T]:
    """首页为空即抛 SourceStructureError；次页起为空原样返回。

    返回值透传，便于直接 `items = expect_first_page(items, ...)` 接着用。
    """
    if page <= 1 and not items:
        raise SourceStructureError(f"{source} 首页返回 0 条，疑似改版或接口异常")
    return items
