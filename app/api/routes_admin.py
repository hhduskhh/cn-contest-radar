"""管理接口：重载种子、管理讯飞 Cookie。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import credentials, seeds
from ..pipeline.events_catalog import reload_catalog

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/reload-seeds")
def reload_seeds():
    """重新导入 data/seeds/*.json。幂等，可反复调用。"""
    counts = seeds.load_all()
    reload_catalog()
    return {"ok": True, **counts}


class CredentialRequest(BaseModel):
    source_key: str
    cookie: str


@router.post("/credential")
def set_credential(req: CredentialRequest):
    """保存数据源 Cookie（目前用于讯飞）。

    接受两种粘贴形式：浏览器 Cookie 字符串，或整段 "Copy as cURL" 命令。
    **本接口永不回传 Cookie 明文**，只返回是否已配置。
    """
    if req.source_key != "xfyun":
        raise HTTPException(status_code=400, detail="目前仅支持 xfyun")

    value = credentials.parse_cookie_input(req.cookie)
    if not value:
        raise HTTPException(status_code=400, detail="Cookie 为空")

    credentials.set_cookie(req.source_key, value)
    return {
        "ok": True,
        "source_key": req.source_key,
        "status": credentials.get_cookie_status(req.source_key),
    }


@router.get("/credential/{source_key}")
def get_credential(source_key: str):
    """查询 Cookie 配置状态。只返回是否已配置与有效性，不含明文。"""
    return credentials.get_cookie_status(source_key)


@router.delete("/credential/{source_key}")
def delete_credential(source_key: str):
    credentials.clear_cookie(source_key)
    return {"ok": True, "status": credentials.get_cookie_status(source_key)}


@router.post("/credential/{source_key}/verify")
def verify_credential(source_key: str):
    """用一次真实请求验证 Cookie 是否还有效。"""
    if source_key != "xfyun":
        raise HTTPException(status_code=400, detail="目前仅支持 xfyun")

    from ..crawlers.xfyun import XfyunCrawler

    crawler = XfyunCrawler()
    if not credentials.get_cookie(source_key):
        raise HTTPException(status_code=400, detail="尚未配置 Cookie")

    try:
        items = crawler._fetch_authenticated(credentials.cookie_header(source_key))
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "message": str(exc)[:300]}
    return {
        "valid": bool(items),
        "items": len(items),
        "message": "Cookie 有效" if items else "Cookie 已配置，但接口未返回数据",
    }
