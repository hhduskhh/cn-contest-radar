"""HTTP Basic 鉴权。

用标准库 secrets.compare_digest 做常数时间比较，不引 passlib/bcrypt ——
单用户局域网场景下，少一个 native 依赖就少一份 Windows 安装风险。

注意：Basic 凭据随每个请求明文传输，仅适用于局域网，绝不可暴露到公网。
"""

from __future__ import annotations

import base64
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import config

# 免鉴权路径：存活探测 + 静态资源（静态资源本身不含敏感数据）
_PUBLIC_PATHS = {"/healthz", "/favicon.ico"}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = decoded.partition(":")
            except Exception:
                user, password = "", ""
            # 两个字段都要比，且都用常数时间比较，避免通过响应耗时区分用户名是否存在
            user_ok = secrets.compare_digest(user, config.APP_USER)
            pass_ok = secrets.compare_digest(password, config.APP_PASSWORD)
            if user_ok and pass_ok:
                return await call_next(request)

        # 注意：HTTP 响应头必须是 latin-1 可编码的，这里绝不能出现中文 ——
        # 写成 realm="赛事信息" 会在构造响应时抛 UnicodeEncodeError（表现为 500 而非 401）。
        # 响应体不受此限制，中文放在 content 里。
        return Response(
            content="需要登录 / Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Competition Hub", charset="UTF-8"'},
        )
