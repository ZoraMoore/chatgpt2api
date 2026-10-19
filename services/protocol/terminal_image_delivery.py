"""从完整会话终态选择唯一图片产物；不执行工具代码，不扫描代码里的文件名。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from services.protocol.final_image import validated_sandbox_image_path


_MAX_MESSAGES = 4096
_MAX_OUTPUT = 65536
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_POINTER = re.compile(r"(?:sediment|file-service)://([A-Za-z0-9_-]{1,128})\Z")
_OUTPUT = re.compile(
    r"\s*(?:<<ImageDisplayed>>\s*)?(/mnt/data/[^\s<>\"'()?#]{1,2048})"
    r"(?:\s+\([1-9][0-9]{0,5},\s*[1-9][0-9]{0,5}\))?\s*(?:<<ImageDisplayed>>\s*)?\Z"
)
_FAILURE_STATES = {"error", "failed", "failure", "interrupted", "cancelled", "canceled", "timeout", "timed_out",
                   "content_filter", "refusal"}


@dataclass(frozen=True)
class TerminalImageDelivery:
    kind: str
    message_id: str
    image_message: dict | None = None
    path: str = ""


def _valid_id(value) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _failed(value: dict) -> bool:
    for key in ("error", "errors", "exception", "in_kernel_exception", "system_exception"):
        if key in value and value[key] is not None and value[key] is not False:
            return True
    for key in ("interrupted", "is_interrupted", "cancelled", "canceled", "timeout_triggered", "is_error"):
        if value.get(key) not in (None, False):
            return True
    for key in ("status", "reasoning_status", "finish_reason"):
        if isinstance(value.get(key), str) and value[key].lower() in _FAILURE_STATES:
            return True
    details = value.get("finish_details")
    return details is not None and (not isinstance(details, dict) or not isinstance(details.get("type"), str)
                                    or details["type"].lower() in _FAILURE_STATES)


def _successful(message: dict) -> bool:
    metadata = message["metadata"]
    return (message.get("status") == "finished_successfully" and not _failed(message)
            and not _failed(metadata) and metadata.get("is_complete", True) is True)


def _branch(document: dict) -> list[dict] | None:
    page = document.get("page_info", {})
    if (not isinstance(page, dict) or any(page.get(k, False) is not False
                                         for k in ("has_previous_page", "has_next_page"))):
        return None
    if "mapping" in document:
        mapping = document["mapping"]
        current = document.get("current_node")
        if not isinstance(mapping, dict) or not 0 < len(mapping) <= _MAX_MESSAGES or not _valid_id(current):
            return None
        result, seen = [], set()
        while current is not None:
            if not _valid_id(current) or current in seen or current not in mapping:
                return None
            seen.add(current)
            node = mapping[current]
            if not isinstance(node, dict) or "parent" not in node or "message" not in node:
                return None
            item = node["message"]
            if item is not None:
                if not isinstance(item, dict) or item.get("id") != current:
                    return None
                result.append(item)
            elif node["parent"] is not None:
                return None
            current = node["parent"]
        result.reverse()
    else:
        result = document.get("messages")
        if not isinstance(result, list) or not 0 < len(result) <= _MAX_MESSAGES:
            return None
        if "current_node" in document and (
                not isinstance(result[-1], dict) or document["current_node"] != result[-1].get("id")):
            return None
    seen = set()
    normalized = []
    for item in result:
        if not isinstance(item, dict) or not _valid_id(item.get("id")) or item["id"] in seen:
            return None
        seen.add(item["id"])
        author, content, metadata = item.get("author"), item.get("content"), item.get("metadata", {})
        if (not isinstance(author, dict) or not isinstance(content, dict) or not isinstance(metadata, dict)
                or len(content) > 128 or len(metadata) > 128
                or author.get("role") not in ("user", "assistant", "tool", "system", "developer")):
            return None
        parts = content.get("parts", [])
        if not isinstance(parts, list) or len(parts) > 128:
            return None
        if any(not isinstance(part, (str, dict)) or isinstance(part, str) and len(part) > 262144 for part in parts):
            return None
        if "text" in content and (not isinstance(content["text"], str) or len(content["text"]) > 262144):
            return None
        normalized.append({**item, "metadata": metadata})
    return normalized


def _input_assets(user: dict) -> set[str] | None:
    result = set()
    for part in user["content"].get("parts", []):
        if isinstance(part, dict) and "asset_pointer" in part:
            pointer = part["asset_pointer"]
            match = _POINTER.fullmatch(pointer) if isinstance(pointer, str) else None
            if not match:
                return None
            result.add(match[1])
    attachments = user["metadata"].get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 128:
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict):
            return None
        for key in ("id", "library_file_id"):
            value = attachment.get(key)
            if value is not None:
                if not _valid_id(value):
                    return None
                result.add(value)
    return result


def _image_pointer(message: dict, inputs: set[str]) -> dict | None:
    name = message["author"].get("name")
    title = message["metadata"].get("image_gen_title")
    generated = (isinstance(title, str) and 0 < len(title) <= 2048 and bool(title.strip())
                 or message["metadata"].get("async_task_type") == "image_gen")
    if (message["author"].get("role") != "tool" or not _valid_id(name)
            or name in {"python", "open_image", "container"} or name.startswith("container.")
            or name.endswith(".open_image") or not _successful(message) or not generated
            or message["content"].get("content_type") != "multimodal_text"):
        return None
    pointers = [part for part in message["content"].get("parts", [])
                if isinstance(part, dict) and part.get("content_type") == "image_asset_pointer"]
    if len(pointers) != 1:
        return None
    pointer = pointers[0].get("asset_pointer")
    match = _POINTER.fullmatch(pointer) if isinstance(pointer, str) else None
    if not match or match[1] in inputs:
        return None
    return {"content_type": "image_asset_pointer", "asset_pointer": pointer}


def _output_path(text) -> str | None:
    if not isinstance(text, str) or len(text) > _MAX_OUTPUT or text.count("<<ImageDisplayed>>") > 1:
        return None
    match = _OUTPUT.fullmatch(text)
    if not match:
        return None
    try:
        return validated_sandbox_image_path(match[1])
    except ValueError:
        return None


def _python_path(call: dict, result: dict) -> str | None:
    content, metadata = result["content"], result["metadata"]
    aggregate = metadata.get("aggregate_result")
    code = call["content"].get("text")
    if (result["author"].get("name") != "python" or content.get("content_type") != "execution_output"
            or not _successful(result) or metadata.get("is_complete") is not True
            or call["author"].get("role") != "assistant" or call.get("recipient") != "python"
            or call["content"].get("content_type") != "code" or not _successful(call)
            or not isinstance(code, str) or not code or len(code) > 262144
            or not isinstance(aggregate, dict) or aggregate.get("status") != "success"
            or aggregate.get("code") != code or _failed(aggregate)):
        return None
    path = _output_path(content.get("text"))
    records, jupyter = aggregate.get("messages"), aggregate.get("jupyter_messages")
    if (not path or not isinstance(records, list) or not 0 < len(records) <= 256
            or not isinstance(jupyter, list) or len(jupyter) > 256):
        return None
    stdout = []
    for record in records:
        if not isinstance(record, dict) or _failed(record):
            return None
        kind = record.get("message_type")
        if kind == "stream" and record.get("stream_name") == "stdout":
            text = record.get("text")
            if not isinstance(text, str) or len(text) > _MAX_OUTPUT:
                return None
            stdout.append(text)
        elif kind != "image":
            return None
    # 双重输出证据约束到同一次执行；图片预览本身不能替代明确的最终文件路径。
    if sum(map(len, stdout)) > _MAX_OUTPUT or _output_path("".join(stdout)) != path:
        return None
    expression = aggregate.get("final_expression_output")
    if expression is not None and (not isinstance(expression, str) or expression.strip() and _output_path(expression) != path):
        return None
    stdout = []
    for record in jupyter:
        if not isinstance(record, dict) or _failed(record):
            return None
        kind, body = record.get("msg_type"), record.get("content", {})
        if not isinstance(body, dict) or _failed(body):
            return None
        if kind == "stream":
            text = body.get("text")
            if body.get("name") != "stdout" or not isinstance(text, str) or len(text) > _MAX_OUTPUT:
                return None
            stdout.append(text)
        elif kind == "status":
            if body.get("execution_state") not in ("busy", "idle"):
                return None
        elif kind == "execute_result":
            data = body.get("data")
            if not isinstance(data, dict) or _output_path(data.get("text/plain")) != path:
                return None
        elif kind not in ("execute_input", "display_data"):
            return None
    if stdout and (sum(map(len, stdout)) > _MAX_OUTPUT or _output_path("".join(stdout)) != path):
        return None
    return path


def select_terminal_image_delivery(
    document, *, conversation_id: str, submitted_message_id: str = "",
    expected_final_message_id: str = "", expected_image_message_id: str = "",
    required_tool_message_ids=(), required_tool_calls: dict[str, str] | None = None,
) -> TerminalImageDelivery | None:
    """只接受本轮空 final 之后已确定的最后工具产物；无法证明时返回 None。"""
    if (not isinstance(document, dict) or not _valid_id(conversation_id)
            or document.get("async_status") is not None):
        return None
    ids = [document[key] for key in ("conversation_id", "id") if key in document]
    if not ids or any(value != conversation_id for value in ids):
        return None
    if any(not isinstance(value, str) or value and not _valid_id(value)
           for value in (submitted_message_id, expected_final_message_id, expected_image_message_id)):
        return None
    if (not isinstance(required_tool_message_ids, (list, tuple, set, frozenset))
            or len(required_tool_message_ids) > _MAX_MESSAGES
            or any(not _valid_id(value) for value in required_tool_message_ids)):
        return None
    if required_tool_calls is not None and (
            not isinstance(required_tool_calls, dict) or len(required_tool_calls) > _MAX_MESSAGES
            or any(not _valid_id(key) or not _valid_id(value) or value == "all"
                   for key, value in required_tool_calls.items())):
        return None
    messages = _branch(document)
    if not messages:
        return None
    users = [i for i, item in enumerate(messages) if item["author"].get("role") == "user"]
    if submitted_message_id:
        selected = [i for i in users if messages[i]["id"] == submitted_message_id]
        if len(selected) != 1 or selected[0] != users[-1]:
            return None
        start = selected[0]
    elif len(users) == 1:
        start = users[0]
    else:
        return None
    turn = messages[start:]
    calls = {item["id"]: item.get("recipient") for item in turn
             if item["author"].get("role") == "assistant" and item.get("recipient", "all") != "all"}
    if any(calls.get(key) != value for key, value in (required_tool_calls or {}).items()):
        return None
    final = turn[-1]
    if (final["author"].get("role") != "assistant" or final.get("channel") != "final"
            or final.get("recipient", "all") != "all" or final.get("end_turn") is not True
            or not _successful(final) or final["metadata"].get("bidi_voice_fem_message") is True
            or final["content"].get("content_type") != "text"
            or "parts" not in final["content"]
            or any(not isinstance(part, str) or part.strip() for part in final["content"].get("parts", []))
            or final["content"].get("text", "").strip()
            or expected_final_message_id and final["id"] != expected_final_message_id):
        return None
    tool_indexes = [i for i, item in enumerate(turn) if item["author"].get("role") == "tool"]
    if not tool_indexes or not set(required_tool_message_ids).issubset({turn[i]["id"] for i in tool_indexes}):
        return None
    index = tool_indexes[-1]
    # 终态前只允许已经完成的思考/摘要；任何新工具调用、正文或系统操作都使候选失效。
    for item in turn[index + 1:-1]:
        if (item["author"].get("role") != "assistant" or item.get("recipient", "all") != "all"
                or item["content"].get("content_type") not in ("thoughts", "reasoning_recap")
                or not _successful(item)):
            return None
    inputs = _input_assets(turn[0])
    if inputs is None:
        return None
    images = [(i, pointer) for i in tool_indexes if (pointer := _image_pointer(turn[i], inputs)) is not None]
    if not images or expected_image_message_id and turn[images[-1][0]]["id"] != expected_image_message_id:
        return None
    result = turn[index]
    if images[-1][0] == index:
        # 不返回原消息 metadata/正文，避免下载器把输入图或其他引用重新加入候选。
        image = {"id": result["id"], "author": dict(result["author"]),
                 "content": {"content_type": "multimodal_text", "parts": [images[-1][1]]}}
        return TerminalImageDelivery("native_image", result["id"], image_message=image)
    if index > 0 and images[-1][0] < index:
        path = _python_path(turn[index - 1], result)
        if path:
            return TerminalImageDelivery("python_output", result["id"], path=path)
    return None
