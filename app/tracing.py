"""请求追踪中间件：沿用 / 生成 X-Request-ID、回传响应头并输出 JSON 访问日志。

- 入站请求携带合法 ``X-Request-ID``（长度 1～64，仅含 ASCII 字母、数字、
  连字符与下划线）时沿用，否则生成 UUID4；
- 每个响应（含业务错误、422 校验错误与未处理异常的 500）都回传
  ``X-Request-ID`` 响应头；
- 每个请求向标准输出写一条 JSON 日志，记录请求 ID、方法、路径、状态码与
  耗时毫秒，不记录请求体；
- ID 存放在每个请求独立的 ``request.state`` 与协程局部变量中，
  并发请求互不串用。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 访问日志固定写到标准输出（容器内与 uvicorn 常规 stderr 日志分流）。
access_logger = logging.getLogger("kiln.access")
if not access_logger.handlers:
    _handler = logging.StreamHandler()  # 默认 stream=sys.stderr
    access_logger.addHandler(_handler)
    access_logger.propagate = False


def _resolve_request_id(request: Request) -> str:
    """合法的入站 X-Request-ID 原样沿用，否则生成 UUID4。"""
    incoming = request.headers.get(REQUEST_ID_HEADER)
    if incoming is not None and _REQUEST_ID_RE.fullmatch(incoming):
        return incoming
    return str(uuid.uuid4())


def _write_access_log(
    request_id: str, method: str, path: str, status_code: int, started: float
) -> None:
    """向标准输出写单行 JSON；仅记录元数据，不触碰请求体。"""
    record = {
        "request_id": request_id,
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)


class RequestTrackingMiddleware(BaseHTTPMiddleware):
    """为每个请求分配请求 ID、回传响应头并记录 JSON 访问日志。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _resolve_request_id(request)
        # 存入 request.state 供路由 / 其他组件按请求读取（每个请求独立对象，
        # 不使用任何进程级可变状态，天然支持并发）。
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # 未处理异常：返回不含内部细节的通用 500，堆栈交 uvicorn 记录。
            access_logger.exception(
                "未处理异常 request_id=%s %s %s",
                request_id,
                request.method,
                request.url.path,
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "INTERNAL_SERVER_ERROR",
                    "message": "服务器内部错误",
                },
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        _write_access_log(
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            started,
        )
        return response
