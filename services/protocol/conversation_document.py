"""兼容网页会话的平铺 messages 与旧 mapping 结构。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterator


def conversation_timestamp(value: object) -> float:
    """新版历史列表返回 ISO 时间，旧会话消息仍可能返回 Unix 秒数。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            return number if math.isfinite(number) else 0.0
        except OverflowError:
            return 0.0
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return 0.0
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            return 0.0


def iter_conversation_messages(document: object) -> Iterator[tuple[str, dict]]:
    if not isinstance(document, dict):
        raise ValueError("invalid conversation document")
    # /backend-api/conversations/{id} 已返回平铺列表。继续只查 mapping 会把
    # 已完成的图片误判为未生成，直到轮询超时；两种结构同时存在时以新版为准。
    messages = document.get("messages")
    if isinstance(messages, list):
        if len(messages) > 10000:
            raise ValueError("conversation message limit exceeded")
        entries = ((None, message) for message in messages)
    else:
        mapping = document.get("mapping")
        if mapping is None:
            return
        if not isinstance(mapping, dict) or len(mapping) > 10000:
            raise ValueError("invalid conversation mapping")
        entries = ((key, node.get("message")) for key, node in mapping.items() if isinstance(node, dict))
    for fallback_id, message in entries:
        if not isinstance(message, dict):
            continue
        message_id = message.get("id") or fallback_id
        if (not isinstance(message_id, str) or len(message_id) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in message_id)):
            message_id = ""
        yield message_id, message
