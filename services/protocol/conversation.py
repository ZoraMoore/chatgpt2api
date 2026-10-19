from __future__ import annotations

import base64
import json
from io import BytesIO
import re
import threading
import time
from concurrent.futures import as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import tiktoken
from PIL import Image

from services.account_service import account_service
from services.image_quota import ImageQuotaExceeded, detect_message_limit, raise_image_quota
from services.config import config
from services.image_execution import image_work_pool
from services.image_storage_service import image_storage_service
from services.protocol.final_image import FinalImageTracker
from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from services.openai_backend_api import (
    ImageContentPolicyError,
    ImageGenerationTimeoutError,
    ImagePollTimeoutError,
    OpenAIBackendAPI,
)
from utils.helper import (
    IMAGE_MODELS,
    extract_image_from_message_content,
    is_codex_image_model,
    is_firefly_image_model,
    is_supported_image_model,
    split_image_model,
)
from utils.image_tokens import count_image_content_tokens
from utils.log import logger


class ImageGenerationError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        error_type: str = "server_error",
        code: str | None = "upstream_error",
        param: str | None = None,
        account_email: str = "",
        conversation_id: str = "",
        image_execution: list[dict[str, Any]] | None = None,
        restore_at: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.param = param
        self.account_email = account_email
        self.conversation_id = conversation_id
        self.image_execution = image_execution or []
        self.restore_at = restore_at

    def to_openai_error(self) -> dict[str, Any]:
        error_dict = {
            "error": {
                "message": str(self) if self.code == "image_quota_exceeded" else public_image_error_message(str(self)),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        if self.account_email:
            error_dict["error"]["account_email"] = self.account_email
        if self.restore_at:
            error_dict["error"]["restore_at"] = self.restore_at
        return error_dict


def image_quota_error(exc: ImageQuotaExceeded) -> ImageGenerationError:
    from datetime import datetime, timezone
    restore = datetime.fromtimestamp(exc.restore_at, timezone.utc).isoformat()
    return ImageGenerationError(str(exc), status_code=429, error_type="rate_limit_error", code="image_quota_exceeded",
        conversation_id=exc.conversation_id, restore_at=restore, image_execution=[{"image_quota_limit": {
            "source": exc.record.get("source"), "occurred_at": exc.record.get("occurred_at"), "restore_at": restore}}])


def public_image_error_message(message: str) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    if any(item in lower for item in ("backend-api/", "status=", "body=", "chatgpt.com", "upstreamhttperror")):
        return "The image generation request failed. Please try again later."
    return text or "The image generation request failed. Please try again later."


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


def is_tls_connection_error(message: str) -> bool:
    """检测 TLS/SSL 连接错误，这类错误通常可以通过重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (35)" in text
        or "tls connect error" in text
        or "openssl_internal" in text
        or "ssl: wrong_version_number" in text
        or "ssl: certificate_verify_failed" in text
        or "connection aborted" in text
        or "remote disconnected" in text
        or "connection reset by peer" in text
    )


def is_connection_timeout_error(message: str) -> bool:
    """检测连接超时错误（如 curl 28），这类错误可通过同账号短等待重试解决。"""
    text = str(message or "").lower()
    return (
        "curl: (28)" in text
        or "operation timed out" in text
        or "connection timed out" in text
        or "read timed out" in text
        or "connect timeout" in text
    )


def image_stream_error_message(message: str) -> str:
    text = str(message or "")
    if is_token_invalid_error(text):
        return "image generation failed"
    if is_tls_connection_error(text):
        return "upstream image connection failed, please retry later"
    if is_connection_timeout_error(text):
        return "upstream connection timed out, please retry later"
    return text or "image generation failed"


REFERENCED_IMAGE_IDS_RE = re.compile(r'"referenced_image_ids"\s*:\s*\[([^\]]+)\]')
# 检测模型返回的部分工具调用 JSON（如 {"size":"1920x1088","n":1}）
# 这些 JSON 包含图片生成工具的参数，但没有实际生成图片
TOOL_PARAMS_JSON_RE = re.compile(
    r'\{\s*"size"\s*:\s*"\d+x\d+"\s*,\s*"n"\s*:\s*\d+\s*\}'
)


def is_model_text_reply_instead_of_image(message: str) -> bool:
    """检测模型是否返回了文本回复（包含工具调用 JSON）而非实际生成图片。

    当上游 ChatGPT 未能触发图片生成工具时，会返回一段描述性文本，
    其中可能包含 JSON 参数（如 prompt、referenced_image_ids、size/n 等）。
    这种情况应被视为「上游未生成图片」而非「内容策略违规」。

    检测两种模式：
    1. 完整的工具调用 JSON（含 referenced_image_ids）
    2. 部分的工具参数 JSON（如 {"size":"1920x1088","n":1}）
    """
    if not message:
        return False
    if REFERENCED_IMAGE_IDS_RE.search(message):
        return True
    # 检测部分工具参数 JSON（模型返回了工具参数但未触发工具）
    if TOOL_PARAMS_JSON_RE.search(message):
        return True
    return False


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(image_data: bytes, base_url: str | None = None) -> str:
    return image_storage_service.save(image_data, base_url).url


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(prompt: str, size: str | None, quality: str = "auto") -> str:
    hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    return f"{prompt.strip()}\n\n{''.join(hints)}" if hints else prompt


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        if response_format == "b64_json":
            result_item = {
                "b64_json": b64_json,
                "revised_prompt": revised_prompt,
            }
            if config.image_management_enabled:
                result_item["url"] = save_image_bytes(base64.b64decode(b64_json), base_url)
            data.append(result_item)
        else:
            data.append({
                "url": save_image_bytes(base64.b64decode(b64_json), base_url),
                "revised_prompt": revised_prompt,
            })
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if message and not data:
        result["message"] = message
    return result


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[str], None] | None
    # 入队时捕获，确保后台切换后同一请求的多张图片仍使用相同传输。
    image_transport: str = field(default_factory=lambda: config.image_transport)


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""
    execution: dict[str, Any] = field(default_factory=dict)

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.execution:
            chunk["_image_execution"] = [self.execution]
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    # 仅从空白段起点匹配。上游长空白增量若不跟标点，原表达式会从每个
    # 空白字符重新回溯，长时间占用 GIL，连健康检查和超时处理也会被阻塞。
    text = re.sub(r"(?<!\s)\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") == "/message/content/parts/0":
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(state: ConversationState, payload: str, event: dict[str, Any] | None = None) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Accept file_id / sediment_id when any of:
    #   1) event is a complete image_gen tool message
    #   2) prior server_ste_metadata already flipped tool_invoked True (in an image_gen turn),
    #      BUT only for non-user messages — user messages contain the uploaded input image
    #      which must NOT be treated as a generated output.
    #   3) patch event whose payload references asset_pointer / file-service://,
    #      BUT only when the event is not a user message.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (state.tool_invoked is True and not is_user_msg)
        or (is_patch_event and not is_user_msg and ("asset_pointer" in payload or "file-service://" in payload))
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        **extra,
    }


def iter_conversation_payloads(payloads: Iterator[str], history_text: str = "",
                               history_messages: list[str] | None = None) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        update_conversation_state(state, payload, event)
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(build_image_prompt(prompt, size, quality)) if image_model else prompt
    if image_model:
        backend._image_quota_prompt = final_prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
    )
    yield from iter_conversation_payloads(payloads, history_text, history_messages)


def text_backend() -> OpenAIBackendAPI:
    return OpenAIBackendAPI(access_token=account_service.get_text_access_token())


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    emitted = False
    while True:
        if token and token in attempted_tokens:
            raise RuntimeError("no available text account")
        if token:
            attempted_tokens.add(token)
        active_backend = None
        try:
            active_backend = OpenAIBackendAPI(access_token=token)
            for event in conversation_events(
                active_backend,
                messages=request.messages,
                model=request.model,
                prompt=request.prompt,
                thinking_effort=request.thinking_effort,
            ):
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    yield delta
            account_service.mark_text_used(token)
            return
        except Exception as exc:
            error_message = str(exc)
            if token and not emitted and is_token_invalid_error(error_message):
                refreshed_token = account_service.refresh_access_token(token, force=True, event="text_stream")
                if refreshed_token and refreshed_token != token and refreshed_token not in attempted_tokens:
                    token = refreshed_token
                else:
                    account_service.remove_invalid_token(token, "text_stream")
                    token = account_service.get_text_access_token(attempted_tokens)
                if token:
                    continue
            raise
        finally:
            if active_backend is not None:
                active_backend.close()


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def _get_detailed_error_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> str:
    """从 /backend-api/tasks/ 接口获取结构化错误信息。

    当 SSE 流检测到 moderation 拦截时，轮询 tasks 接口获取详细错误文本。
    使用结构化字段（metadata.is_error, author.role, content.content_type）判断，
    而非依赖易变的文本匹配。

    参数：
    - `backend`：OpenAIBackendAPI 实例。
    - `conversation_id`：会话 ID。
    - `timeout_secs`：请求超时秒数。
    - `wait_secs`：等待任务创建的秒数。设为 0 可跳过等待。

    返回：
    - 详细错误信息文本，如果未找到则返回空字符串。
    """
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return ""

        for task in tasks:
            is_error, error_msg, metadata = backend.check_task_error(task)
            if is_error and error_msg:
                logger.info({
                    "event": "image_task_structured_error",
                    "conversation_id": conversation_id,
                    "error_msg": error_msg,
                    "metadata": metadata,
                })
                return error_msg
        return ""
    except ImageQuotaExceeded:
        raise
    except Exception as exc:
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": str(exc),
        })
        return ""


def _remove_image_conversation_later(backend: OpenAIBackendAPI, conversation_id: str) -> None:
    if not config.image_remove_conversation_after_result or not conversation_id:
        return

    def _run() -> None:
        try:
            backend.delete_conversation(conversation_id)
            logger.info({"event": "image_conversation_removed", "conversation_id": conversation_id})
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_remove_failed",
                "conversation_id": conversation_id,
                "error": str(exc),
            })

    threading.Thread(target=_run, name=f"remove-image-conversation-{conversation_id}", daemon=True).start()


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
        *,
        resume_conversation_id: str = "",
        poll_timeout_secs: float | None = None,
        resume_has_reference: bool = False,
        _resume_snapshot: dict | None = None,
        _resume_image_urls: list[str] | None = None,
        allow_terminal_recovery: bool = True,
) -> Iterator[ImageOutput]:
    backend._image_quota_checks = True
    backend._image_quota_prompt = request.prompt
    last: dict[str, Any] = {}
    final_images = FinalImageTracker()
    delivery_document = None
    observed_tool_calls: dict[str, str] = {}
    bound_probe_conversation = ""
    terminal_observation_safe = True
    has_reference = bool(request.images) or resume_has_reference
    if resume_conversation_id:
        # 续轮询只读取原会话，不重新发送用户消息或生成请求；共用最终产物判定。
        started = time.monotonic()
        resume_deadline = started + (poll_timeout_secs or config.image_poll_timeout_secs)
        document = _resume_snapshot if _resume_snapshot is not None else backend._get_conversation(
            resume_conversation_id, timeout_secs=min(60.0, poll_timeout_secs or config.image_poll_timeout_secs),
        )
        delivery_document = document
        messages = document.get("messages")
        if not isinstance(messages, list):
            messages = [(node or {}).get("message") for node in (document.get("mapping") or {}).values()]
        payloads = [json.dumps({"conversation_id": resume_conversation_id})]
        payloads.extend(json.dumps({"conversation_id": resume_conversation_id, "message": message})
                        for message in messages if isinstance(message, dict))
        payloads.append("[DONE]")
        events = iter_conversation_payloads(iter(payloads))
        poll_timeout_secs = max(0.001, resume_deadline - time.monotonic())
    else:
        events = conversation_events(
            backend,
            prompt=request.prompt,
            model=request.model,
            images=request.images or [],
            size=request.size,
            quality=request.quality,
        )
    for event in events:
        last = event
        raw = event.get("raw")
        if isinstance(raw, dict):
            final_images.update(raw)
            message = final_images.current
            if final_images.conversation_id and final_images.conversation_id != bound_probe_conversation and not resume_conversation_id:
                account_service.bind_image_probe_conversation(getattr(backend, "access_token", ""), final_images.conversation_id)
                bound_probe_conversation = final_images.conversation_id
            evidence = detect_message_limit(message, final_images.conversation_id or event.get("conversation_id") or "",
                user_text=getattr(backend, "_image_quota_prompt", request.prompt), fallback_id=getattr(backend, "_image_submitted_message_id", ""))
            if evidence is not None:
                raise_image_quota(backend, evidence)
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            message_id, recipient = message.get("id"), message.get("recipient")
            if (author.get("role") == "assistant" and isinstance(message_id, str)
                    and isinstance(recipient, str) and recipient != "all"):
                if (not 0 < len(message_id) <= 128 or not 0 < len(recipient) <= 128
                        or len(observed_tool_calls) >= 4096 and message_id not in observed_tool_calls):
                    terminal_observation_safe = False
                else:
                    observed_tool_calls[message_id] = recipient
        if event.get("done"):
            final_images.stream_complete = True
        if event.get("type") == "conversation.delta":
            yield ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                text=str(event.get("delta") or ""),
                upstream_event_type="conversation.delta",
            )
            continue
        if event.get("type") == "conversation.event":
            raw = event.get("raw")
            raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
            yield ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                upstream_event_type=raw_type,
            )

    conversation_id = str(last.get("conversation_id") or "")
    # 读回文档不能遗漏直播已经看到的工具工作，或回退到更早的一张图。
    observed_final_id = final_images.final.get("id", "")
    observed_image_id = final_images.last_image_message_id
    observed_tool_ids = tuple(final_images._seen_tool_messages)
    def final_error(message: str) -> ImageGenerationError:
        return ImageGenerationError(message, conversation_id=conversation_id,
                                    image_execution=[final_images.execution_summary("unavailable")])
    # 部分网页增量只给出最终链接，省略完整消息快照。仅在存在后处理信号时
    # 补读一次已存储会话，以取得 interpreter/download 必需的最终消息 ID。
    final_hint = "sandbox:/mnt/data/" in str(last.get("text") or "") or last.get("turn_use_case") == "data analysis"
    try:
        needs_snapshot = not final_images.is_complete() or not final_images.paths()
    except ValueError as exc:
        raise final_error("最终修正版图片路径无效，未返回中间图片。") from exc
    if conversation_id and (final_images.postprocessing or final_hint) and needs_snapshot and not resume_conversation_id:
        try:
            document = backend._get_conversation(conversation_id)
            delivery_document = document
            messages = document.get("messages")
            if not isinstance(messages, list):
                messages = [(node or {}).get("message") for node in (document.get("mapping") or {}).values()]
            snapshot = FinalImageTracker()
            for item in messages:
                if isinstance(item, dict):
                    snapshot.update({"message": item, "_voice_transport": document.get("voice_turn_complete") is True})
            if document.get("voice_turn_complete") is True:
                snapshot.update({"type": "voice_turn_complete"})
            snapshot.stream_complete = True
            snapshot.postprocessing = snapshot.postprocessing or final_images.postprocessing
            snapshot.conversation_id = conversation_id
            snapshot.review_requested = final_images.review_requested
            snapshot.review_reason = final_images.review_reason
            snapshot.expected_final_path = final_images.expected_final_path
            snapshot.code_tool_after_image = snapshot.code_tool_after_image or final_images.code_tool_after_image
            snapshot.skipped_mainline = snapshot.skipped_mainline or final_images.skipped_mainline
            for tool, count in final_images.tools.items():
                snapshot.tools[tool] = max(count, snapshot.tools.get(tool, 0))
            if snapshot.final:
                final_images = snapshot
        except ImageQuotaExceeded:
            raise
        except Exception as exc:
            raise final_error("无法确认后处理的最终图片，未返回中间图片。") from exc
    try:
        final_paths = final_images.paths()
    except ValueError as exc:
        raise final_error("最终修正版图片路径无效，未返回中间图片。") from exc
    result_source = "sandbox"
    download_message_id = final_images.final.get("id", "")
    if (allow_terminal_recovery and terminal_observation_safe and not final_paths and delivery_document is not None and request.image_transport == "conversation"
            and not final_images.voice_transport and not final_images.review_requested and final_images.is_complete()):
        submitted_id = getattr(backend, "_image_submitted_message_id", "")
        terminal = select_terminal_image_delivery(
            delivery_document, conversation_id=conversation_id,
            submitted_message_id=submitted_id if isinstance(submitted_id, str) else "",
            expected_final_message_id=observed_final_id,
            expected_image_message_id=observed_image_id,
            required_tool_message_ids=observed_tool_ids,
            required_tool_calls=observed_tool_calls,
        )
        if terminal is not None and terminal.kind == "python_output":
            # 仅取最后成功执行明确输出的文件，不从代码或历史文件列表猜测路径。
            final_paths = [terminal.path]
            download_message_id = terminal.message_id
            result_source = "python_terminal"
        elif terminal is not None and terminal.kind == "native_image" and not (has_reference and config.image_require_reference_postprocess):
            try:
                file_ids, sediment_ids = [], []
                for part in terminal.image_message["content"]["parts"]:
                    match = re.fullmatch(r"(file-service|sediment)://([A-Za-z0-9_-]{1,200})", part["asset_pointer"])
                    if match is None:
                        raise ValueError("invalid terminal image reference")
                    (file_ids if match[1] == "file-service" else sediment_ids).append(match[2])
                # 只解析选中的图片工具结果，禁止轮询再混入历史图或输入图。
                urls = backend._resolve_image_urls(conversation_id, file_ids, sediment_ids)
                if not isinstance(urls, list) or len(urls) != 1:
                    raise ValueError("ambiguous terminal image URL")
                if request.progress_callback:
                    request.progress_callback("receiving_image")
                images = backend.download_image_bytes(urls)
                if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], bytes) or not 0 < len(images[0]) <= 50 * 1024 * 1024:
                    raise ValueError("invalid terminal image data")
                with Image.open(BytesIO(images[0])) as image:
                    if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                        raise ValueError("unsupported terminal image format")
                    image.verify()
                data = format_image_result(
                    [{"b64_json": base64.b64encode(images[0]).decode("ascii")}],
                    request.prompt, request.response_format, request.base_url, int(time.time()),
                )["data"]
                if not data:
                    raise ValueError("empty terminal image result")
            except ImageQuotaExceeded:
                raise
            except Exception as exc:
                raise final_error("最终图片下载或校验失败，未返回中间图片。") from exc
            _remove_image_conversation_later(backend, conversation_id)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data,
                              conversation_id=conversation_id,
                              execution=final_images.execution_summary("image_tool_terminal"))
            return
    if (not final_paths and final_images.review_requested and final_images.expected_final_path
            and final_images.is_complete() and final_images.code_tool_after_image):
        final_paths = [final_images.expected_final_path]
        result_source = "sandbox_contract"
    if final_paths or final_images.postprocessing or final_hint:
        if not final_paths or not final_images.is_complete():
            raise final_error("图片后处理尚未完整交付最终图片，未返回中间图片。")
        try:
            if request.progress_callback:
                request.progress_callback("receiving_image")
            images = backend.download_final_sandbox_images(conversation_id, download_message_id, final_paths)
            data = format_image_result(
                [{"b64_json": base64.b64encode(image).decode("ascii")} for image in images],
                request.prompt, request.response_format, request.base_url, int(time.time()),
            )["data"]
            if not data:
                raise RuntimeError("empty final image result")
        except ImageQuotaExceeded:
            raise
        except Exception as exc:
            raise final_error("最终修正版图片下载失败，未返回中间图片。") from exc
        logger.info({"event": "image_final_sandbox_result", "conversation_id": conversation_id,
                     "image_count": len(data), "postprocessing": final_images.postprocessing,
                     "result_source": result_source})
        _remove_image_conversation_later(backend, conversation_id)
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id,
                          execution=final_images.execution_summary(result_source))
        return
    if (has_reference and config.image_require_reference_postprocess and final_images.generated
            and not final_images.code_tool_after_image):
        raise final_error("上游只生成了初始图片，没有在同一轮调用 Python/容器完成参考图修正；未返回中间图片。")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    logger.info({
        "event": "image_stream_resolve_start",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": last.get("turn_use_case"),
    })
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")
    if message and not file_ids and not sediment_ids and last.get("blocked"):
        # 尝试从 /backend-api/tasks/ 获取详细错误信息
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id)
        error_text = detailed_error or message or "Image generation was rejected by upstream policy."
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=error_text, conversation_id=conversation_id)
        return
    should_poll_for_image = bool(has_reference or resume_conversation_id) or last.get("turn_use_case") == "image gen"
    if message and not file_ids and not sediment_ids and not should_poll_for_image:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
    # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开，
    # 图片可能正在异步生成中。需要使用更积极的轮询策略来获取结果。
    is_text_reply = bool(message and is_model_text_reply_instead_of_image(message))
    if is_text_reply:
        logger.info({
            "event": "image_detected_text_reply_with_ids",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    # 当检测到文本回复但 conversation_id 丢失时，尝试从最近对话列表中恢复
    # SSE 流太短时（模型返回文本而非触发图片工具），conversation_id 可能未被捕获，
    # 但图片已在上游异步生成。通过列出最近对话来恢复 conversation_id。
    if is_text_reply and not conversation_id:
        try:
            import time as _time
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, _time.time(), timeout_secs=5.0,
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_conversation_id_recovered",
                    "conversation_id": conversation_id,
                    "message_preview": message[:200],
                })
        except ImageQuotaExceeded:
            raise
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })

    # 在轮询图片之前，先检查 /backend-api/tasks/ 是否有 moderation 拦截
    # 这样可以避免不必要的长时间轮询超时
    # 注意：当 should_poll_for_image 为 True 或检测到文本回复时，
    # 即使 tasks 报告了"错误"，也不能直接返回——因为上游可能将工具调用的 JSON 参数
    # （如 {"size":"1792x1024","n":1}）标记为 is_error，而实际上图片正在异步生成中。
    # 此时应继续轮询图片。
    detailed_error = ""
    if not file_ids and not sediment_ids and conversation_id and not resume_conversation_id:
        detailed_error = _get_detailed_error_from_tasks(backend, conversation_id, timeout_secs=5.0, wait_secs=1.0)
        if detailed_error and not should_poll_for_image and not is_text_reply:
            logger.info({
                "event": "image_task_error_before_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })
            yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=detailed_error, conversation_id=conversation_id)
            return
        if detailed_error and (should_poll_for_image or is_text_reply):
            logger.info({
                "event": "image_task_error_skipped_for_poll",
                "conversation_id": conversation_id,
                "error": detailed_error,
            })

    # 文本回复可能代表图片仍在异步生成，但单次请求必须受全局超时限制，
    # 避免上游卡住时长时间占用一个图片任务。
    poll_timeout = poll_timeout_secs if poll_timeout_secs is not None else config.image_poll_timeout_secs

    try:
        image_urls = _resume_image_urls if _resume_image_urls is not None else backend.resolve_conversation_image_urls(
            conversation_id, file_ids, sediment_ids, poll_timeout_secs=poll_timeout,
        )
    except (ImageContentPolicyError, ImagePollTimeoutError) as exc:
        # 当检测到文本回复时，task error 不应直接判定为内容策略违规，
        # 因为图片可能仍在后台异步生成中
        if is_text_reply and isinstance(exc, ImageContentPolicyError):
            logger.warning({
                "event": "image_text_reply_task_error_ignored",
                "conversation_id": conversation_id,
                "error": str(exc),
            })
            image_urls = []
        else:
            raise
    except ImageQuotaExceeded:
        raise
    except Exception as exc:
        # 当检测到文本回复时，首次轮询的临时网络错误不应直接中断，
        # 因为图片可能仍在后台异步生成中，后续 retry poll 会继续尝试。
        if is_text_reply and conversation_id:
            logger.warning({
                "event": "image_text_reply_first_poll_error_ignored",
                "conversation_id": conversation_id,
                "error": repr(exc)[:300],
            })
            image_urls = []
        else:
            raise

    if image_urls:
        if resume_conversation_id and _resume_snapshot is None:
            # 轮询期间模型可能才开始后处理；交付前重读快照，复用上面的最终产物判断。
            remaining = resume_deadline - time.monotonic()
            if remaining <= 0:
                raise ImagePollTimeoutError("续轮询超时，尚未确认最终图片。")
            snapshot = backend._get_conversation(resume_conversation_id, timeout_secs=min(60.0, remaining))
            yield from stream_image_outputs(
                backend, request, index, total, resume_conversation_id=resume_conversation_id,
                poll_timeout_secs=max(0.001, resume_deadline - time.monotonic()), resume_has_reference=has_reference,
                _resume_snapshot=snapshot, _resume_image_urls=image_urls,
                allow_terminal_recovery=allow_terminal_recovery,
            )
            return
        if resume_conversation_id and any(
                isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant"
                and message.get("status") == "in_progress" for message in messages):
            raise ImagePollTimeoutError("上游仍在处理原会话，请稍后继续等待。")
        if request.progress_callback:
            request.progress_callback("receiving_image")
        image_items = [
            {"b64_json": base64.b64encode(image_data).decode("ascii")}
            for image_data in backend.download_image_bytes(image_urls)
        ]
        data = format_image_result(
            image_items,
            request.prompt,
            request.response_format,
            request.base_url,
            int(time.time()),
        )["data"]
        if data:
            _remove_image_conversation_later(backend, conversation_id)
            yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id,
                                  execution=final_images.execution_summary(
                                      "image_tool", "unverified_reference" if has_reference else "valid_image"))
        return

    if resume_conversation_id:
        raise ImagePollTimeoutError("原会话图片仍未完成，可稍后继续等待。")

    if message:
        # 检测模型是否返回了文本描述（含 referenced_image_ids）而非实际生成图片
        # 这说明模型已发起图片生成工具调用，但 SSE 在工具完成前断开。
        # 此时应再尝试轮询图片结果，而不是直接把文本当作最终输出。
        # 当 is_text_reply 但 conversation_id 丢失时，尝试从最近对话列表恢复
        if is_text_reply and not conversation_id:
            try:
                import time as _time
                recovered_id = backend.find_conversation_by_prompt(
                    request.prompt, _time.time(), timeout_secs=5.0,
                )
                if recovered_id:
                    conversation_id = recovered_id
                    logger.info({
                        "event": "image_text_reply_conversation_id_recovered",
                        "conversation_id": conversation_id,
                        "message_preview": message[:200],
                    })
            except ImageQuotaExceeded:
                raise
            except Exception as exc:
                logger.warning({
                    "event": "image_text_reply_conversation_id_recovery_failed",
                    "error": repr(exc)[:300],
                })
        if is_text_reply and conversation_id:
            logger.info({
                "event": "image_model_text_reply_retry_poll",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
            # 仅进行一次受配置超时限制的补查，避免图片任务无限等待。
            retry_poll_timeout = config.image_poll_timeout_secs
            MAX_POLL_RETRIES = 1
            for poll_attempt in range(1, MAX_POLL_RETRIES + 1):
                try:
                    polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                        conversation_id,
                        retry_poll_timeout,
                        file_ids,
                        sediment_ids,
                    )
                    file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                    sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                    break  # 轮询成功，退出重试循环
                except ImageQuotaExceeded:
                    raise
                except Exception as exc:
                    error_str = str(exc)
                    is_transient = (
                        isinstance(exc, ImagePollTimeoutError)
                        or is_tls_connection_error(error_str)
                        or "upstream" in error_str.lower()
                        or "connection" in error_str.lower()
                        or "timeout" in error_str.lower()
                    )
                    logger.warning({
                        "event": "image_model_text_reply_poll_failed",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "error": repr(exc)[:300],
                        "is_transient": is_transient,
                    })
                    # 如果还有重试次数且不是超时/内容违规错误，继续重试
                    if poll_attempt < MAX_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                        # 递增退避：30s, 60s, 90s
                        backoff = 30.0 * poll_attempt
                        logger.info({
                            "event": "image_model_text_reply_poll_retry",
                            "conversation_id": conversation_id,
                            "poll_attempt": poll_attempt,
                            "backoff_secs": backoff,
                        })
                        time.sleep(backoff)
                        continue
                    # 超时错误或重试次数用尽，停止重试
                    break

            if file_ids or sediment_ids:
                image_urls = backend.resolve_conversation_image_urls(
                    conversation_id, file_ids, sediment_ids, poll=False,
                )
                if image_urls:
                    if request.progress_callback:
                        request.progress_callback("receiving_image")
                    image_items = [
                        {"b64_json": base64.b64encode(image_data).decode("ascii")}
                        for image_data in backend.download_image_bytes(image_urls)
                    ]
                    data = format_image_result(
                        image_items,
                        request.prompt,
                        request.response_format,
                        request.base_url,
                        int(time.time()),
                    )["data"]
                    if data:
                        _remove_image_conversation_later(backend, conversation_id)
                        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id,
                                  execution=final_images.execution_summary(
                                      "image_tool", "unverified_reference" if request.images else "valid_image"))
                        return
        elif is_text_reply:
            logger.warning({
                "event": "image_model_text_reply_no_image",
                "conversation_id": conversation_id,
                "message_preview": message[:200],
            })
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
        return

    # 兜底：当 message 为空且图片 URL 解析失败时，先尝试一次短延迟重试轮询
    # 然后抛出明确错误而非让调用方得到 "upstream completed without generating images" 这种模糊报错
    logger.warning({
        "event": "image_stream_no_result_fallback",
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "should_poll_for_image": should_poll_for_image,
    })
    # 当 should_poll_for_image 为 True 但 conversation_id 丢失时，尝试恢复
    if should_poll_for_image and not conversation_id:
        try:
            import time as _time
            recovered_id = backend.find_conversation_by_prompt(
                request.prompt, _time.time(), timeout_secs=5.0,
            )
            if recovered_id:
                conversation_id = recovered_id
                logger.info({
                    "event": "image_fallback_conversation_id_recovered",
                    "conversation_id": conversation_id,
                })
        except ImageQuotaExceeded:
            raise
        except Exception as exc:
            logger.warning({
                "event": "image_fallback_conversation_id_recovery_failed",
                "error": repr(exc)[:300],
            })
    if should_poll_for_image and conversation_id:
        # SSE 可能早于图片完成而关闭；仅进行一次受配置超时限制的补查。
        retry_poll_timeout = config.image_poll_timeout_secs
        MAX_FALLBACK_POLL_RETRIES = 1
        for poll_attempt in range(1, MAX_FALLBACK_POLL_RETRIES + 1):
            retry_wait_secs = min(30.0 * poll_attempt, config.image_poll_initial_wait_secs * poll_attempt)
            logger.info({
                "event": "image_stream_retry_poll_after_wait",
                "conversation_id": conversation_id,
                "retry_wait_secs": retry_wait_secs,
                "poll_attempt": poll_attempt,
            })
            time.sleep(retry_wait_secs)
            try:
                polled_file_ids, polled_sediment_ids = backend._poll_image_results(
                    conversation_id,
                    retry_poll_timeout,
                    file_ids,
                    sediment_ids,
                )
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
                break  # 轮询成功，退出重试循环
            except ImageQuotaExceeded:
                raise
            except Exception as exc:
                error_str = str(exc)
                is_transient = (
                    isinstance(exc, ImagePollTimeoutError)
                    or is_tls_connection_error(error_str)
                    or "upstream" in error_str.lower()
                    or "connection" in error_str.lower()
                    or "timeout" in error_str.lower()
                )
                logger.warning({
                    "event": "image_stream_retry_poll_failed",
                    "conversation_id": conversation_id,
                    "poll_attempt": poll_attempt,
                    "error": repr(exc)[:300],
                    "is_transient": is_transient,
                })
                # 如果还有重试次数且不是超时/内容违规错误，继续重试
                if poll_attempt < MAX_FALLBACK_POLL_RETRIES and not isinstance(exc, (ImagePollTimeoutError, ImageContentPolicyError)):
                    # 递增退避：30s, 60s
                    backoff = 30.0 * poll_attempt
                    logger.info({
                        "event": "image_stream_retry_poll_retry",
                        "conversation_id": conversation_id,
                        "poll_attempt": poll_attempt,
                        "backoff_secs": backoff,
                    })
                    time.sleep(backoff)
                    continue
                # 超时错误或重试次数用尽，停止重试
                break
        
        if file_ids or sediment_ids:
            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if image_urls:
                if request.progress_callback:
                    request.progress_callback("receiving_image")
                image_items = [
                    {"b64_json": base64.b64encode(image_data).decode("ascii")}
                    for image_data in backend.download_image_bytes(image_urls)
                ]
                data = format_image_result(
                    image_items,
                    request.prompt,
                    request.response_format,
                    request.base_url,
                    int(time.time()),
                )["data"]
                if data:
                    _remove_image_conversation_later(backend, conversation_id)
                    yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data, conversation_id=conversation_id,
                                  execution=final_images.execution_summary(
                                      "image_tool", "unverified_reference" if request.images else "valid_image"))
                    return
        
        # 重试后仍然失败，yield 错误消息
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation completed upstream but the result could not be retrieved. "
                               "The image may still be processing. Please try again in a moment.",
                          conversation_id=conversation_id)
    elif message:
        yield ImageOutput(kind="message", model=request.model, index=index, total=total, text=message, conversation_id=conversation_id)
    else:
        # conversation_id 也为空时（SSE 流极短、未捕获到会话 ID），
        # 仍然 yield 一条消息，避免 stream_image_outputs_with_pool 产生
        # "upstream completed without generating images" 模糊报错
        yield ImageOutput(kind="message", model=request.model, index=index, total=total,
                          text="Image generation started upstream but the response was incomplete. "
                               "Please try again.",
                          conversation_id=conversation_id)


def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    images = _codex_response_images(list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
    )))
    if not images:
        raise ImageGenerationError("No image result found in response")
    data = format_image_result(
        [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
    )["data"]
    if data:
        yield ImageOutput(kind="result", model=request.model, index=index, total=total, data=data)
        return
    raise ImageGenerationError("No image result found in response")


def _generate_single_image(request: ConversationRequest, index: int, total: int) -> list[ImageOutput]:
    return image_work_pool.run(_generate_single_image_impl, request, index, total)


def _generate_single_image_impl(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。
    """
    # Adobe Firefly 反代：走独立 provider（自己选号、计数），其余调度行为与本函数一致。
    if is_firefly_image_model(request.model):
        from services.firefly.provider import generate_single_image as generate_firefly_image

        return generate_firefly_image(request, index, total)

    # 模型返回文本而非图片的最大重试次数
    MAX_TEXT_REPLY_RETRIES = 3
    text_reply_retry_count = 0
    account_email = ""

    while True:
        try:
            if request.progress_callback:
                request.progress_callback("getting_account")
            plan_type, _ = split_image_model(request.model)
            codex_model = is_codex_image_model(request.model)
            token = account_service.get_available_access_token(
                plan_type=plan_type,
                source_type="codex" if codex_model else None,
                plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
            )
        except RuntimeError as exc:
            raise ImageGenerationError(str(exc) or "image generation failed", account_email=account_email) from exc

        emitted_for_token = False
        returned_message = False
        returned_result = False
        account = account_service.get_account(token) or {}
        account_email = str(account.get("email") or "").strip()
        logger.debug({
            "event": "image_account_lookup",
            "token_prefix": token[:12] + "..." if len(token) > 12 else token,
            "account_email": account_email,
            "account_found": bool(account),
            "index": index,
        })
        backend = None
        succeeded = False
        outputs: list[ImageOutput] = []

        def attach_context(exc):
            exc.account_email = account_email
            voice = getattr(backend, "_voice_image_session", None)
            snapshot = voice.diagnostic_snapshot() if voice is not None else None
            if isinstance(snapshot, dict):
                # 管理日志可用诊断编号关联传输状态，不写入 token、SDP 或用户消息。
                executions = [item for item in getattr(exc, "image_execution", [])
                              if not isinstance(item, dict) or "voice_transport" not in item]
                exc.image_execution = [*executions, {"voice_transport": snapshot}]
            conversation_id = getattr(exc, "conversation_id", "")
            if not conversation_id:
                conversation_id = getattr(backend, "image_conversation_id", "")
            if not isinstance(conversation_id, str) or not conversation_id:
                conversation_id = next((item.conversation_id for item in reversed(outputs) if item.conversation_id), "")
            if conversation_id:
                exc.conversation_id = conversation_id
                if not codex_model:
                    exc.resume_account_ref = account_service.image_account_ref(token)
            return exc

        try:
            backend = OpenAIBackendAPI(access_token=token)
            backend.image_transport = request.image_transport
            if request.progress_callback:
                backend.progress_callback = request.progress_callback
            stream_fn = stream_codex_image_outputs if is_codex_image_model(request.model) else stream_image_outputs
            for output in stream_fn(backend, request, index, total):
                if account_email and not output.account_email:
                    output.account_email = account_email
                if output.kind == "message" and request.message_as_error:
                    raise attach_context(ImageGenerationError(
                        output.text or "Image generation was rejected by upstream policy.",
                        status_code=400,
                        error_type="invalid_request_error",
                        code="content_policy_violation",
                        account_email=account_email,
                        conversation_id=output.conversation_id,
                    ))
                emitted_for_token = True
                returned_message = output.kind == "message"
                returned_result = returned_result or output.kind == "result"
                outputs.append(output)
            if returned_message:
                return outputs
            if not returned_result:
                if emitted_for_token:
                    conv_id = outputs[-1].conversation_id if outputs else ""
                    raise attach_context(ImageGenerationError(
                        "upstream completed without generating images",
                        status_code=400,
                        error_type="invalid_request_error",
                        code="no_image_generated",
                        account_email=account_email,
                        conversation_id=conv_id,
                    ))
                return outputs
            succeeded = True
            return outputs
        except ImageQuotaExceeded as exc:
            raise attach_context(image_quota_error(exc)) from exc
        except ImagePollTimeoutError as exc:
            attach_context(exc)
            raise
        except ImageGenerationTimeoutError as exc:
            logger.warning({
                "event": "image_generation_timeout",
                "request_token": token,
                "account_email": account_email,
                "error": str(exc),
                "index": index,
            })
            raise attach_context(ImageGenerationError(
                str(exc),
                status_code=504,
                error_type="server_error",
                code="image_generation_timeout",
                account_email=account_email,
            )) from exc
        except ImageContentPolicyError as exc:
            logger.warning({
                "event": "image_stream_content_policy_error",
                "request_token": token,
                "account_email": account_email,
                "error": str(exc),
                "index": index,
            })
            raise attach_context(ImageGenerationError(
                str(exc) or "Image generation was rejected by upstream policy.",
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
                account_email=account_email,
                conversation_id=getattr(exc, "conversation_id", ""),
            )) from exc
        except ImageGenerationError as exc:
            attach_context(exc)
            if account_email and not getattr(exc, "account_email", ""):
                exc.account_email = account_email
            error_text = str(exc)
            # 如果是模型返回文本而非图片，尝试换账号重试
            if (is_model_text_reply_instead_of_image(error_text) and not emitted_for_token
                    and not getattr(getattr(backend, "_voice_image_session", None), "sent", False)
                    and not getattr(backend, "_webimage_generation_sent", False)):
                text_reply_retry_count += 1
                if text_reply_retry_count <= MAX_TEXT_REPLY_RETRIES:
                    logger.warning({
                        "event": "image_model_text_reply_retry",
                        "request_token": token,
                        "account_email": account_email,
                        "retry_count": text_reply_retry_count,
                        "index": index,
                        "error": error_text[:200],
                    })
                    continue
                logger.warning({
                    "event": "image_model_text_reply_exhausted_retries",
                    "request_token": token,
                    "account_email": account_email,
                    "retry_count": text_reply_retry_count,
                    "index": index,
                })
                raise attach_context(ImageGenerationError(
                    "Image generation failed: the upstream model returned a text description "
                    "instead of generating an image. Please try again later.",
                    status_code=502,
                    error_type="server_error",
                    code="upstream_text_reply",
                    account_email=account_email,
                    conversation_id=getattr(exc, "conversation_id", ""),
                )) from exc
            logger.warning({
                "event": "image_stream_generation_error",
                "request_token": token,
                "account_email": account_email,
                "error": error_text,
                "index": index,
            })
            raise
        except Exception as exc:
            last_error = str(exc)
            logger.warning({
                "event": "image_stream_fail",
                "request_token": token,
                "account_email": account_email,
                "error": last_error,
                "index": index,
            })
            if (not emitted_for_token and is_token_invalid_error(last_error)
                    and not getattr(getattr(backend, "_voice_image_session", None), "sent", False)
                    and not getattr(backend, "_webimage_generation_sent", False)):
                refreshed_token = account_service.refresh_access_token(token, force=True, event="image_stream")
                if refreshed_token and refreshed_token != token:
                    token = refreshed_token
                    continue
                account_service.remove_invalid_token(token, "image_stream")
                continue
            # 连接异常不能证明上游尚未接单；禁止重放整个生成 POST。
            # 已取得会话标识时保留原账号引用，由续轮询读取既有结果。
            raise attach_context(ImageGenerationError(
                image_stream_error_message(last_error), account_email=account_email,
            )) from exc
        finally:
            try:
                if backend is not None:
                    backend.close()
            finally:
                # 语音会话关闭后再一次性结算并释放槽，避免同账号新任务与旧连接重叠。
                account_service.mark_image_result(token, succeeded)


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    if not is_supported_image_model(request.model):
        from services.firefly.models import list_model_names as list_firefly_model_names

        supported = sorted(IMAGE_MODELS) + list_firefly_model_names()
        raise ImageGenerationError("unsupported image model,supported models: " + ", ".join(supported))

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(request, 1, 1)
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        for index in range(1, request.n + 1):
            outputs = _generate_single_image(request, index, request.n)
            for output in outputs:
                yield output
        return

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    # 每张图片一个线程，同时启动
    futures = {}
    results: dict[int, list[ImageOutput]] = {}
    errors: dict[int, Exception] = {}
    try:
        for index in range(1, request.n + 1):
            future = image_work_pool.submit(_generate_single_image_impl, request, index, request.n)
            futures[future] = index

        # 按完成顺序收集结果
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                errors[index] = exc
                logger.warning({
                    "event": "image_parallel_generation_error",
                    "index": index,
                    "error": str(exc)[:300],
                })

    finally:
        for future in futures:
            future.cancel()

    # yield 结果：跳过索引顺序限制，不再让低索引失败阻塞高索引成功结果
    emitted = False
    last_error = ""
    # 先 yield 所有成功的结果
    for index in range(1, request.n + 1):
        if index in results:
            for output in results[index]:
                emitted = True
                yield output
        elif index in errors:
            last_error = str(errors[index])
            if not emitted:
                logger.warning({
                    "event": "image_parallel_failure_before_success",
                    "failed_index": index,
                    "error": last_error[:200],
                })

    # 如果有失败但也有成功，记录警告
    if emitted:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })

    if not emitted:
        if not last_error:
            last_error = "no account in the pool could generate images — check account quota and rate-limit status"
        raise ImageGenerationError(image_stream_error_message(last_error), conversation_id="")


def stream_image_chunks(outputs: Iterable[ImageOutput]) -> Iterator[dict[str, Any]]:
    for output in outputs:
        yield output.to_chunk()


def collect_image_outputs(outputs: Iterable[ImageOutput]) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    executions: list[dict[str, Any]] = []
    conversation_id = ""
    for output in outputs:
        created = created or output.created
        if output.conversation_id:
            conversation_id = output.conversation_id
        if output.execution:
            executions.append(output.execution)
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
        elif output.kind == "result":
            data.extend(output.data)

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    if conversation_id:
        result["_conversation_id"] = conversation_id
    if executions:
        result["_image_execution"] = executions
    return result
