"""记录图片 HTTP 响应的本地发送过程，不把发送完成当作下游业务回执。"""
from __future__ import annotations

import re
import time
from contextvars import ContextVar
from typing import Any
from uuid import uuid4


_REQUEST_ID = re.compile(rb"[A-Za-z0-9_.:-]{1,128}\Z")
_IMAGE_DELIVERY_TRACE: ContextVar[dict[str, str] | None] = ContextVar(
    "image_delivery_trace", default=None
)


def get_image_delivery_trace() -> dict[str, str]:
    """提供可跨异步任务和工作线程关联的副本，调用方不能修改请求上下文。"""
    return dict(_IMAGE_DELIVERY_TRACE.get() or {})


def _client_request_id(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name in (b"x-request-id", b"x-client-request-id"):
        values = [value for key, value in headers if key.lower() == name]
        if not values:
            continue
        # 重复、合并或非法主请求 ID 不降级到另一个头，避免关联歧义及日志注入。
        if len(values) != 1 or not isinstance(values[0], bytes) or not _REQUEST_ID.fullmatch(values[0]):
            return None
        return values[0].decode("ascii")
    return None


class ImageDeliveryTraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/v1/images/"):
            return await self.app(scope, receive, send)

        trace = {"call_id": uuid4().hex}
        client_request_id = _client_request_id(scope.get("headers", []))
        if client_request_id is not None:
            trace["client_request_id"] = client_request_id
        token = _IMAGE_DELIVERY_TRACE.set(trace)
        started = time.monotonic()
        detail: dict[str, Any] = {
            **trace,
            "response_status": None,
            "response_bytes": 0,
            "client_disconnected": False,
            "send_state": "response_incomplete",
        }

        async def traced_receive():
            message = await receive()
            # 只观察应用本来会读取的断开事件，不能额外消费上传流或取消生成。
            if message.get("type") == "http.disconnect":
                detail["client_disconnected"] = True
            return message

        async def traced_send(message):
            try:
                await send(message)
            except BaseException:
                detail["send_state"] = "send_failed"
                raise
            if message.get("type") == "http.response.start":
                detail["response_status"] = message.get("status")
            elif message.get("type") == "http.response.body":
                detail["response_bytes"] += len(message.get("body", b""))
                if not message.get("more_body", False) and detail["send_state"] != "send_failed":
                    # ASGI send 返回只能证明本地发送调用完成，不能证明下游收到或解析成功。
                    detail["send_state"] = "response_send_completed"

        try:
            return await self.app(scope, traced_receive, traced_send)
        except BaseException:
            if detail["send_state"] != "send_failed":
                detail["send_state"] = "app_failed"
            raise
        finally:
            _IMAGE_DELIVERY_TRACE.reset(token)
            detail["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            try:
                # 延迟导入避免生成日志读取 trace 时形成循环依赖。
                from services.log_service import log_service

                log_service.add("delivery", "图片响应传输记录（不代表下游业务确认）", detail)
            except Exception:
                # 观测失败不能改变业务响应，也不能覆盖原始异常；不打印异常正文。
                pass
