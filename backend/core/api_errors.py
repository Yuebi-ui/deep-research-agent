"""统一 API 错误契约（执行计划 §7.4 / §7.5）。

设计：

```text
业务代码抛 ApiError(code, message, status) 或 domain 异常
        ↓
集中注册的 exception handler
        ↓
{"error": {"code", "message", "request_id", "details"}}
```

**绝不把 traceback 返回给前端。**
未预期的异常统一映射为 500 / INTERNAL_ERROR，细节只进日志。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.domain.task import (
    InvalidReviewState,
    InvalidTaskTransition,
    UnknownTaskStatus,
)

# ===== 稳定错误码（§7.4）=====

CODE_TASK_NOT_FOUND = "TASK_NOT_FOUND"
CODE_INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
CODE_REVIEW_NOT_ALLOWED = "REVIEW_NOT_ALLOWED"
CODE_TASK_ALREADY_TERMINAL = "TASK_ALREADY_TERMINAL"
CODE_CANCEL_NOT_ALLOWED = "CANCEL_NOT_ALLOWED"
CODE_QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
CODE_CHECKPOINT_UNAVAILABLE = "CHECKPOINT_UNAVAILABLE"
CODE_EVENT_STREAM_UNAVAILABLE = "EVENT_STREAM_UNAVAILABLE"
CODE_VALIDATION_ERROR = "VALIDATION_ERROR"
CODE_INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    """带稳定错误码的业务异常。

    路由层抛它，handler 负责转成统一结构。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def error_body(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details,
        }
    }


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器。"""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                exc.code, exc.message,
                request_id=_request_id(request), details=exc.details,
            ),
        )

    @app.exception_handler(InvalidTaskTransition)
    async def _invalid_transition(request: Request, exc: InvalidTaskTransition):
        return JSONResponse(
            status_code=409,
            content=error_body(
                CODE_INVALID_STATE_TRANSITION,
                str(exc),
                request_id=_request_id(request),
                details={"current": exc.current.value, "target": exc.target.value},
            ),
        )

    @app.exception_handler(InvalidReviewState)
    async def _invalid_review(request: Request, exc: InvalidReviewState):
        return JSONResponse(
            status_code=409,
            content=error_body(
                CODE_REVIEW_NOT_ALLOWED,
                str(exc),
                request_id=_request_id(request),
                details={"current": exc.current.value},
            ),
        )

    @app.exception_handler(UnknownTaskStatus)
    async def _unknown_status(request: Request, exc: UnknownTaskStatus):
        # 数据异常 → 500，但不泄漏细节
        return JSONResponse(
            status_code=500,
            content=error_body(
                CODE_INTERNAL_ERROR, "任务状态不可识别",
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=error_body(
                CODE_VALIDATION_ERROR,
                "请求参数校验失败",
                request_id=_request_id(request),
                details={"errors": exc.errors()[:10]},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        """把 FastAPI 的 HTTPException 也统一成同一结构。

        路由里仍在用 HTTPException 的地方（404 等）会走到这里，
        因此前端只需要认一种错误结构。
        """
        code = _code_for_status(exc.status_code)
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message, request_id=_request_id(request)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # 未预期异常：细节只进日志，响应里不给 traceback
        import logging

        logging.getLogger(__name__).error(
            "未处理异常: %s", exc, exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content=error_body(
                CODE_INTERNAL_ERROR, "内部错误",
                request_id=_request_id(request),
            ),
        )


def _code_for_status(status_code: int) -> str:
    return {
        400: "BAD_REQUEST",
        404: CODE_TASK_NOT_FOUND,
        409: CODE_INVALID_STATE_TRANSITION,
        422: CODE_VALIDATION_ERROR,
        429: "RATE_LIMITED",
        503: CODE_QUEUE_UNAVAILABLE,
    }.get(status_code, CODE_INTERNAL_ERROR)


__all__ = [
    "ApiError",
    "CODE_CANCEL_NOT_ALLOWED",
    "CODE_CHECKPOINT_UNAVAILABLE",
    "CODE_EVENT_STREAM_UNAVAILABLE",
    "CODE_INTERNAL_ERROR",
    "CODE_INVALID_STATE_TRANSITION",
    "CODE_QUEUE_UNAVAILABLE",
    "CODE_REVIEW_NOT_ALLOWED",
    "CODE_TASK_ALREADY_TERMINAL",
    "CODE_TASK_NOT_FOUND",
    "CODE_VALIDATION_ERROR",
    "error_body",
    "register_exception_handlers",
]
