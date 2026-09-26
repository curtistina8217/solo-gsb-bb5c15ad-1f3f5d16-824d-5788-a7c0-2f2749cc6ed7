"""请求追踪测试：X-Request-ID 沿用 / 生成、响应头、JSON 访问日志与并发隔离。

仅依赖标准库：自带极简 ASGI 调用器（asyncio 并发），无需 httpx / TestClient。
直接运行：python3 tests/test_tracing.py
"""

import asyncio
import io
import json
import os
import re
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="kiln-tracing-test-")
os.environ["KILN_DB_PATH"] = os.path.join(_TMP, "tracing.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.responses import JSONResponse  # noqa: E402

from app import database  # noqa: E402
from app.main import app  # noqa: E402

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class _ASGIResponse:
    """收集一次 ASGI 调用的状态码、响应头与 JSON 响应体。"""

    def __init__(self) -> None:
        self.status_code: int | None = None
        self.headers: dict[bytes, bytes] = {}
        self.body_chunks: list[bytes] = []

    def json(self):
        return json.loads(b"".join(self.body_chunks).decode())


async def call_asgi(method: str, path: str, headers=None, body: bytes = b""):
    """不走网络直接调用 ASGI 应用，返回 _ASGIResponse。"""
    scope_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    if body:
        scope_headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": scope_headers,
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "root_path": "",
    }
    result = _ASGIResponse()
    sent_body = object()

    async def receive():
        # 只投递一次 http.request（本测试体都很小）。
        nonlocal sent_body
        if sent_body is None:
            return {"type": "http.disconnect"}
        sent_body = None
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            result.status_code = message["status"]
            result.headers = dict(message.get("headers", []))
        elif message["type"] == "http.response.body":
            result.body_chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return result


class _Tee:
    """stdout 旁路：既收集日志行供断言，也原样打印便于人工观察。"""

    def __init__(self, real):
        self.real = real
        self.buffer = io.StringIO()

    def write(self, data):
        self.real.write(data)
        self.buffer.write(data)
        return len(data)

    def flush(self):
        self.real.flush()
        self.buffer.flush()

    def lines(self):
        return [ln for ln in self.buffer.getvalue().splitlines() if ln.strip()]


def _last_json_logs(tee: _Tee, count: int):
    lines = tee.lines()
    return [json.loads(ln) for ln in lines[-count:]]


async def main_async() -> None:
    database.init_db()

    # 1. 合法 X-Request-ID 原样沿用；200/404/422/400 均回传该头。
    resp = await call_asgi(
        "GET", "/health", headers={"X-Request-ID": "trace-1_abc.9"}
    )
    # 点号非法 → 必须生成 UUID，而不是沿用。
    assert resp.status_code == 200
    rid = resp.headers[b"x-request-id"].decode()
    assert UUID_RE.fullmatch(rid), rid
    print(f"ok: 含点号的非法 ID 被替换为 UUID: {rid}")

    valid_id = "trace-1_abc-9XYZ0123456789"
    resp = await call_asgi(
        "GET", "/health", headers={"X-Request-ID": valid_id}
    )
    assert resp.status_code == 200
    assert resp.headers[b"x-request-id"].decode() == valid_id
    print("ok: 合法 X-Request-ID 在 200 响应中原样回传")

    # 404 业务错误（PLAN_NOT_FOUND）。
    resp = await call_asgi(
        "GET", "/plans/no-such-plan", headers={"X-Request-ID": valid_id}
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": {
            "error": "PLAN_NOT_FOUND",
            "message": "未找到排程方案: no-such-plan",
        }
    }
    assert resp.headers[b"x-request-id"].decode() == valid_id
    print("ok: 404 业务错误状态码 / 响应体不变，且回传请求 ID")

    # 422 校验错误：状态码与 FastAPI 原生错误体保持不变。
    resp = await call_asgi(
        "POST",
        "/plans",
        headers={"X-Request-ID": valid_id, "Content-Type": "application/json"},
        body=b'{"capacity": 10, "items": []}',
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail[0]["type"] == "too_short"
    assert resp.headers[b"x-request-id"].decode() == valid_id
    print("ok: 422 校验错误状态码 / 原生响应体不变，且回传请求 ID")

    # 400 业务错误（单件超容量）。
    resp = await call_asgi(
        "POST",
        "/plans",
        headers={"X-Request-ID": valid_id, "Content-Type": "application/json"},
        body=json.dumps(
            {
                "capacity": 3,
                "items": [
                    {"item_id": "p1", "volume": 5, "temp_low": 100, "temp_high": 200}
                ],
            }
        ).encode(),
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "ITEM_EXCEEDS_CAPACITY"
    assert resp.headers[b"x-request-id"].decode() == valid_id
    print("ok: 400 业务错误状态码 / 响应体不变，且回传请求 ID")

    # 2. 缺省头时两次请求各自生成不同的 UUID。
    r1 = await call_asgi("GET", "/health")
    r2 = await call_asgi("GET", "/health")
    id1 = r1.headers[b"x-request-id"].decode()
    id2 = r2.headers[b"x-request-id"].decode()
    assert UUID_RE.fullmatch(id1) and UUID_RE.fullmatch(id2) and id1 != id2
    print(f"ok: 缺省头时生成 UUID 且请求间不重复: {id1} / {id2}")

    # 3. 边界长度：64 合法、65 非法；空值非法；含空格 / 非 ASCII 非法。
    rid64 = "a" * 64
    resp = await call_asgi("GET", "/health", headers={"X-Request-ID": rid64})
    assert resp.headers[b"x-request-id"].decode() == rid64
    resp = await call_asgi(
        "GET", "/health", headers={"X-Request-ID": "b" * 65}
    )
    assert UUID_RE.fullmatch(resp.headers[b"x-request-id"].decode())
    resp = await call_asgi("GET", "/health", headers={"X-Request-ID": ""})
    assert UUID_RE.fullmatch(resp.headers[b"x-request-id"].decode())
    resp = await call_asgi(
        "GET", "/health", headers={"X-Request-ID": "has space"}
    )
    assert UUID_RE.fullmatch(resp.headers[b"x-request-id"].decode())
    resp = await call_asgi(
        "GET", "/health", headers={"X-Request-ID": "请求编号"}
    )
    assert UUID_RE.fullmatch(resp.headers[b"x-request-id"].decode())
    print("ok: 长度 1～64 且字符集合法时沿用，其余一律生成 UUID")

    # 4. 未处理异常返回通用 500 且仍带请求 ID。
    from app.main import app as fastapi_app

    @fastapi_app.get("/_test/boom")
    def _boom():  # 仅供本测试触发未处理异常
        raise RuntimeError("boom (测试用，不应出现在响应中)")

    resp = await call_asgi(
        "GET", "/_test/boom", headers={"X-Request-ID": "boom-trace"}
    )
    assert resp.status_code == 500
    assert resp.json() == {
        "error": "INTERNAL_SERVER_ERROR",
        "message": "服务器内部错误",
    }
    assert "boom (测试用" not in json.dumps(resp.json(), ensure_ascii=False)
    assert resp.headers[b"x-request-id"].decode() == "boom-trace"
    print("ok: 未处理异常返回通用 500（不泄漏内部信息），且回传请求 ID")

    # 5. 每个请求一条 stdout JSON 日志：字段齐全、不含请求体。
    tee = _Tee(sys.stdout)
    old_stdout = sys.stdout
    sys.stdout = tee
    try:
        secret_body = json.dumps(
            {
                "capacity": 10,
                "items": [
                    {"item_id": "p1", "volume": 5, "temp_low": 100, "temp_high": 200}
                ],
            }
        ).encode()
        await call_asgi(
            "POST",
            "/plans",
            headers={
                "X-Request-ID": "log-check",
                "Content-Type": "application/json",
            },
            body=secret_body,
        )
        await call_asgi("GET", "/plans/missing", headers={"X-Request-ID": "log-404"})
        await call_asgi(
            "POST",
            "/plans",
            headers={
                "X-Request-ID": "log-422",
                "Content-Type": "application/json",
            },
            body=b"{}",
        )
        await call_asgi("GET", "/_test/boom", headers={"X-Request-ID": "log-500"})
    finally:
        sys.stdout = old_stdout

    logs = _last_json_logs(tee, 4)
    by_id = {entry["request_id"]: entry for entry in logs}
    assert set(by_id) == {"log-check", "log-404", "log-422", "log-500"}, logs
    expected = {
        "log-check": ("POST", "/plans", 201),
        "log-404": ("GET", "/plans/missing", 404),
        "log-422": ("POST", "/plans", 422),
        "log-500": ("GET", "/_test/boom", 500),
    }
    for rid, (method, path, status) in expected.items():
        entry = by_id[rid]
        assert entry["method"] == method
        assert entry["path"] == path
        assert entry["status_code"] == status
        assert isinstance(entry["duration_ms"], (int, float))
        assert entry["duration_ms"] >= 0
        assert set(entry) == {
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
        }, entry
    raw = tee.buffer.getvalue()
    assert "p1" not in raw and '"volume"' not in raw and "secret" not in raw
    print("ok: 每请求一条 JSON 日志（ID/方法/路径/状态码/耗时毫秒），不含请求体")

    # 6. 并发请求的 ID 不能串用：挂一个会让出事件循环的临时路由。
    from fastapi import Request as FastAPIRequest

    all_entered = asyncio.Event()
    gate = asyncio.Event()
    entered_count = 0

    @fastapi_app.get("/_test/echo-rid")
    async def _echo_rid(request: FastAPIRequest):
        nonlocal entered_count
        entered_count += 1
        if entered_count == 10:
            all_entered.set()
        await gate.wait()
        return JSONResponse({"request_id": request.state.request_id})

    async def run_concurrent(prefix: str):
        """10 个请求全部在路由内等待后再统一放行，确保真实并发重叠。"""
        rids = [f"{prefix}-{i}" for i in range(10)]
        tasks = [
            asyncio.create_task(
                call_asgi("GET", "/_test/echo-rid", headers={"X-Request-ID": rid})
            )
            for rid in rids
        ]
        await asyncio.wait_for(all_entered.wait(), timeout=5)
        gate.set()
        return rids, await asyncio.gather(*tasks)

    ids, responses = await run_concurrent("concurrent")
    for sent_rid, response in zip(ids, responses):
        assert response.status_code == 200
        assert response.headers[b"x-request-id"].decode() == sent_rid
        assert response.json()["request_id"] == sent_rid
    assert {r.headers[b"x-request-id"].decode() for r in responses} == set(ids)
    print("ok: 10 个并发请求的 X-Request-ID 在响应头、request.state 中均不串用")

    # 第二批并发：在 tee 收集 stdout 的情况下核对日志归属。
    all_entered.clear()
    gate.clear()
    entered_count = 0
    tee2 = _Tee(sys.stdout)
    sys.stdout = tee2
    try:
        conc_ids, _ = await run_concurrent("log-conc")
    finally:
        sys.stdout = old_stdout
    conc_entries = _last_json_logs(tee2, 10)
    assert {e["request_id"] for e in conc_entries} == set(conc_ids)
    assert all(e["path"] == "/_test/echo-rid" for e in conc_entries)
    print("ok: 并发访问日志与各自请求 ID 一一对应")

    print("全部 tracing 测试通过")


if __name__ == "__main__":
    asyncio.run(main_async())
