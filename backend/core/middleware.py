"""HTTP 中间件。

Request ID：让一次请求的日志、错误响应与（将来的）trace 可以互相关联。

```text
请求头 X-Request-ID 存在 → 复用（便于跨服务串联）
不存在                  → 生成
```

生成的 ID 同时写入 ``request.state``（供错误响应读取）与响应头
（供前端与排障使用）。
"""

from __future__ import annotations

import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

REQUEST_ID_HEADER = "X-Request-ID"


async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
    request.state.request_id = request_id

    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def rate_limit_middleware(request: Request, call_next, *, store: dict, window: int, limit: int):
    """滑动窗口限流。

    ``store`` 由调用方持有（模块级 dict），便于测试重置——
    它曾经是模块级状态却不在测试重置清单里，导致 API 测试跑到第 31 个
    请求起全红 429，表现为「单独跑通过、连跑必失败」。
    """
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    window_start = now - window
    store[client_ip] = [t for t in store[client_ip] if t > window_start]

    if len(store[client_ip]) >= limit:
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "请求过于频繁，请稍后再试",
                    "request_id": getattr(request.state, "request_id", None),
                    "details": {"retry_after": window},
                }
            },
            headers={"Retry-After": str(window)},
        )

    store[client_ip].append(now)
    return await call_next(request)


__all__ = ["REQUEST_ID_HEADER", "rate_limit_middleware", "request_id_middleware"]
