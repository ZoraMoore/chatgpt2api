"""从完整会话终态选择唯一图片产物；不执行工具代码，不扫描代码里的文件名。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from services.protocol.final_image import SANDBOX_IMAGE_RE, validated_sandbox_image_path
from services.protocol.conversation_document import allows_image_terminal_check


_MAX_MESSAGES = 4096
_MAX_OUTPUT = 65536
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_POINTER = re.compile(r"(?:sediment|file-service)://([A-Za-z0-9_-]{1,128})\Z")
_OUTPUT = re.compile(
    r"\s*(?:<<ImageDisplayed>>\s*)?(/mnt/data/[^\s<>\"'()?#]{1,2048})"
    r"(?:\s+\([1-9][0-9]{0,5},\s*[1-9][0-9]{0,5}\))?\s*(?:<<ImageDisplayed>>\s*)?\Z"
)
# 实际观察到的ImageMagick输出：只允许这条弃用提示和一条完整PNG记录。
_IDENTIFY_WARNING = ('WARNING: The convert command is deprecated in IMv7, use "magick" '
                     'instead of "convert" or "magick convert"')
_PNG_IDENTIFY = re.compile(
    r"(/mnt/data/[^\s<>\"'()?#]{1,2048}\.png) PNG "
    r"([1-9][0-9]{0,5})x([1-9][0-9]{0,5}) \2x\3\+0\+0 "
    r"(?:8|16)-bit sRGB [0-9]{1,12}(?:\.[0-9]{1,8})?(?:B|KiB|MiB|GiB) "
    r"[0-9]{1,8}(?:\.[0-9]{1,8})?u [0-9]{1,6}:[0-9]{2}\.[0-9]{1,6}\Z"
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
                or not isinstance(content.get("content_type"), str)
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


def _container_path(call: dict, result: dict) -> str | None:
    """只接受已观测到的容器输出契约，不从shell代码提取文件名。"""
    content, metadata = result['content'], result['metadata']
    call_meta = call['metadata']
    aggregate = metadata.get('aggregate_result')
    code = call['content'].get('text')
    if (result['author'].get('name') != 'container.exec'
            or content.get('content_type') != 'execution_output' or not _successful(result)
            or content.get('parts', []) != []
            or call['author'].get('role') != 'assistant' or call.get('recipient') != 'container.exec'
            or call['content'].get('content_type') != 'code' or not _successful(call)
            or not isinstance(code, str) or not 0 < len(code) <= 262144
            or not isinstance(aggregate, dict)
            or set(aggregate) != {'status', 'messages', 'final_expression_output'}
            or aggregate.get('status') != 'success' or aggregate.get('messages') != []
            or _failed(aggregate)):
        return None
    # 上游container消息的parent_id不指向相邻调用，用三种轮次标识联合绑定执行结果。
    for key in ('request_id', 'turn_exchange_id', 'working_turn_id'):
        if not _valid_id(call_meta.get(key)) or call_meta[key] != metadata.get(key):
            return None
    output = content.get('text')
    if not isinstance(output, str) or aggregate.get('final_expression_output') != output:
        return None
    path = _output_path(output)
    if path:
        return path
    if len(output) > _MAX_OUTPUT:
        return None
    lines = [line for line in output.strip().splitlines() if line.strip()]
    if lines and lines[0] == _IDENTIFY_WARNING:
        lines = lines[1:]
    if len(lines) != 1:
        return None
    match = _PNG_IDENTIFY.fullmatch(lines[0])
    if not match:
        return None
    try:
        return validated_sandbox_image_path(match[1])
    except ValueError:
        return None


class _CompletionTextParser(HTMLParser):
    """仅提取否定判定所需文本；未知展示组件也不应遮住否定词。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.quote_depth = 0

    def handle_data(self, data):
        if not self.quote_depth:
            self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"blockquote", "q", "pre", "code"}:
            self.quote_depth += 1
            self.parts.append("\n")
        elif tag == "br" and not self.quote_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"blockquote", "q", "pre", "code"} and self.quote_depth:
            self.quote_depth -= 1
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def _display_text(value: str) -> str | None:
    if not isinstance(value, str) or len(value) > 262144:
        return None
    parser = _CompletionTextParser()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, TypeError):
        return None
    return "".join(parser.parts)


def _direct_completion_text(text: str) -> str:
    """历史拒绝的引用和代码示例不能取代本次交付结论。"""
    text = re.sub(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[^\n]*(?:\n|$)", "\n", text)
    text = re.sub(r"(?m)^[ \t]*>[^\n]*(?:\n|$)", "\n", text)
    text = re.sub(r"`[^`\n]*`", "\n", text)
    visible = _display_text(text)
    text = visible if visible is not None else text
    return re.sub(r'''“[^“”]*”|「[^「」]*」|『[^『』]*』|‘[^‘’]*’|"[^"\n]*"|(?<!\w)'[^'\n]*'(?!\w)''', "\n", text)


def _direct_refusal_match(pattern: str, text: str, *, current_context: bool = False) -> bool:
    for match in re.finditer(pattern, text, re.I):
        leading = text[max(0, match.start() - 160):match.start()]
        prefix = re.split(r"[\n。！？；;，,]", leading)[-1][-80:]
        # “不是无法完成”是在否定拒绝；用户要求和历史转述也不是本次拒绝。
        if re.search(r"(?:并非|并不是|不是|并不(?:是|会)?|没有说|未曾说|用户(?:说|要求|提示)|原话|引用|示例|例如|比如|此前|之前|曾经|上次|如果|假如|假设|若)[^\n。！？；;，,]{0,80}$", prefix):
            continue
        if current_context and not re.search(r"(?:目前|当前|本次|这次|此次|由于)[^\n。！？；;]{0,120}$", leading):
            continue
        if re.search(r"(?:没有|并未|不会|不曾|未曾)违反", match.group()):
            continue
        return True
    return False


def _final_delivery_rejection_reason(message: dict, *, completed: bool = False) -> str | None:
    """返回对外终态拒绝原因；completed 仅兼容本轮已确认完成的 final。"""
    if (not isinstance(message, dict) or not isinstance(message.get("author"), dict)
            or message["author"].get("role") != "assistant"
            or message.get("recipient", "all") != "all"
            or message.get("status") != "finished_successfully"
            or message.get("channel") not in (None, "final", "commentary")
            or (message.get("end_turn") is not True
                and (not completed or message.get("channel") == "commentary"))):
        return None
    content = message.get("content")
    if not isinstance(content, dict) or content.get("content_type") not in ("text", "multimodal_text"):
        return None
    parts = content.get("parts", [])
    if not isinstance(parts, list) or any(not isinstance(p, (str, dict)) for p in parts):
        return None
    text = "\n".join(p for p in parts if isinstance(p, str))
    if not isinstance(content.get("text", ""), str) or len(text) + len(content.get("text", "")) > 262144:
        return "quality_rejected"
    text += "\n" + content.get("text", "")
    metadata = message.get("metadata") or {}
    dil = metadata.get("model_dil_v2") if isinstance(metadata, dict) else None
    fallback = dil.get("fallbackMarkdown") if isinstance(dil, dict) else None
    if fallback is not None:
        if not isinstance(fallback, str) or len(text) + len(fallback) > 262144:
            return "quality_rejected"
        text += "\n" + fallback
    text = _direct_completion_text(text)
    # 策略拒绝可能以已结束的 commentary 返回，且不会补齐此前的工具返回。
    policy_patterns = (
        r"(?:生成的)?(?:图片|图像)[^\n。]{0,100}违反(?:了)?[^\n。]{0,160}(?:防护限制|内容政策|安全政策)",
        # 实际终态也会以“该提示可能违反了我们的内容政策”拒绝，不能继续等待未返回的工具。
        # 提示与违反之间仅接受明确肯定表述，避免把“不违反”和普通政策承诺当成拒绝。
        r"(?:该|此|本次|这次)(?:提示(?:语|词)?|请求)(?:可能|或许|似乎)?违反(?:了)?"
        r"(?:我们(?:的)?|相关)?(?:内容政策|安全政策)",
    )
    if any(_direct_refusal_match(pattern, text) for pattern in policy_patterns):
        return "content_policy_rejected"
    if isinstance(metadata, dict) and (metadata.get("refusal") or metadata.get("is_refusal") is True):
        return "quality_rejected"
    # “不输出不合格成品”也可能只是质量承诺，必须有本轮实际失败上下文。
    withholding_patterns = (
        r"不将[^\n。]{0,100}(?:未达标|不合格|修复痕迹|被重绘)[^\n。]{0,100}作为[^\n。]{0,40}(?:成品|终稿)[^\n。]{0,40}(?:交付|输出)",
        r"(?:不能|无法|不可)[^\n。]{0,120}作为(?:合格|最终)?(?:成品|图片|图像|终稿)[^\n。]{0,40}(?:输出|交付)",
        r"不(?:输出|交付)[^\n。]{0,140}(?:不合格|未达标|修复痕迹|被重绘)[^\n。]{0,80}(?:成品|图片|图像|终稿)",
    )
    current_failure_pattern = (
        r"(?:无法|未能|不能)[^\n。]{0,240}(?:完成|确认|修复|清除|保护|保证)"
        r"|(?:图片|图像|结果|成品)(?:确实|仍然|依然|还是)?(?:未达标|不合格)"
    )
    if (_direct_refusal_match(current_failure_pattern, text, current_context=True)
            and any(_direct_refusal_match(pattern, text) for pattern in withholding_patterns)):
        return "quality_rejected"
    # 上游成功结束的是消息，仍可能明确告知图片不合格；不能据状态码交付草稿。
    patterns = (
        r"(?:本次|这次|此次)?生成未能满足[^\n。]{0,300}(?:要求|交付)",
        r"(?:不能|无法|不应|不可)作为[^\n。]{0,100}(?:合格|最终)[^\n。]{0,100}交付",
        r"(?:未能|无法)(?:完成|生成|提供|交付)[^\n。]{0,120}(?:图片|图像|修改|编辑|终稿)",
        r"(?:图片|图像|本次生成)(?:生成|处理|修改|编辑)?失败",
        r"(?:图片|图像)(?:尚未|还未|没有)(?:生成|完成)",
        r"(?:仍在|还在|正在)(?:处理|修改|生成|编辑)",
        r"(?:画像|イメージ)[^\n。]{0,120}(?:生成|作成|編集|処理)[^\n。]{0,40}(?:できません|できなかった|失敗)",
        r"\b(?:cannot|can't|unable to|failed to)\s+(?:generate|create|edit|deliver|provide)\b",
        r"\b(?:does not|doesn't|did not|didn't|fails? to)\s+meet\s+(?:the|your)\s+requirements?\b",
    )
    return "quality_rejected" if any(_direct_refusal_match(pattern, text) for pattern in patterns) else None


def final_delivery_rejected(message: dict, *, completed: bool = False) -> bool:
    """兼容布尔判定接口，终态选择器另外保留策略与质量拒绝的具体原因。"""
    return _final_delivery_rejection_reason(message, completed=completed) is not None


def _final_assets(final: dict) -> tuple[list[tuple[str, str]], bool]:
    """正文交付优先；仅兼容声明式fallback，普通引用索引不能扩充交付列表。"""
    content = final["content"]
    parts = content.get("parts", [])
    text = "\n".join(part for part in parts if isinstance(part, str))

    def parse(value):
        assets = []
        for match in SANDBOX_IMAGE_RE.finditer(value):
            assets.append(("sandbox", validated_sandbox_image_path(match[1])))
        for match in re.finditer(r"(?:file-service|sediment)://([A-Za-z0-9_-]{1,128})(?=[\s)\]\"<>]|$)", value):
            assets.append(("native", match[1]))
        return list(dict.fromkeys(assets))

    assets = parse(text)
    for part in parts:
        if isinstance(part, dict) and part.get("content_type") == "image_asset_pointer":
            pointer = part.get("asset_pointer")
            match = _POINTER.fullmatch(pointer) if isinstance(pointer, str) else None
            if not match:
                raise ValueError("invalid final pointer")
            assets.append(("native", match[1]))
    assets = list(dict.fromkeys(assets))
    if not assets:
        dil = final["metadata"].get("model_dil_v2")
        fallback = dil.get("fallbackMarkdown") if isinstance(dil, dict) else None
        if isinstance(fallback, str) and len(fallback) <= 262144:
            assets = parse(fallback)
    return assets, bool(text.strip())


def _inspection(message: dict) -> bool:
    name = message["author"].get("name")
    return isinstance(name, str) and (name in {"open_image", "container.open_image"} or name.endswith(".open_image"))


def _pending_calls(turn: list[dict]) -> bool:
    pending = []
    for item in turn:
        role = item["author"]["role"]
        recipient = item.get("recipient", "all")
        if role == "assistant" and recipient != "all":
            pending.append(recipient)
        elif role == "tool":
            name = item["author"].get("name")
            if name in pending and (item.get("status") == "finished_successfully" or _failed(item)):
                pending.remove(name)
    return bool(pending)


def select_terminal_image_delivery(
    document, *, conversation_id: str, submitted_message_id: str = "",
    expected_final_message_id: str = "", expected_image_message_id: str = "",
    required_tool_message_ids=(), required_tool_calls: dict[str, str] | None = None,
    completed_final_message_id: str = "", completion_receipt: dict | None = None,
    diagnostic: dict | None = None,
) -> TerminalImageDelivery | None:
    """以本轮完成证据和产物来源选图；完成说明的语言和长度不决定成功。"""
    info = diagnostic if diagnostic is not None else {}
    info.clear()

    def reject(reason):
        info["reason"] = reason
        return None

    def accept(result, source):
        info.update(reason="ready", completion_source=source, result_source=result.kind)
        return result

    if not isinstance(document, dict) or not _valid_id(conversation_id):
        return reject("invalid_document")
    ids = [document[key] for key in ("conversation_id", "id") if key in document]
    if not ids or any(value != conversation_id for value in ids):
        return reject("conversation_mismatch")
    if not allows_image_terminal_check(document):
        return reject("document_in_progress" if document.get("async_status") == 3 else "unsupported_document_state")
    if any(not isinstance(value, str) or value and not _valid_id(value)
           for value in (submitted_message_id, expected_final_message_id, expected_image_message_id, completed_final_message_id)):
        return reject("invalid_message_identity")
    if (not isinstance(required_tool_message_ids, (list, tuple, set, frozenset))
            or len(required_tool_message_ids) > _MAX_MESSAGES
            or any(not _valid_id(value) for value in required_tool_message_ids)):
        return reject("invalid_observed_tools")
    if required_tool_calls is not None and (
            not isinstance(required_tool_calls, dict) or len(required_tool_calls) > _MAX_MESSAGES
            or any(not _valid_id(key) or not _valid_id(value) or value == "all"
                   for key, value in required_tool_calls.items())):
        return reject("invalid_observed_calls")
    messages = _branch(document)
    if not messages:
        return reject("invalid_branch")
    users = [i for i, item in enumerate(messages) if item["author"].get("role") == "user"]
    if submitted_message_id:
        selected = [i for i in users if messages[i]["id"] == submitted_message_id]
        if len(selected) != 1 or selected[0] != users[-1]:
            return reject("submission_mismatch")
        start = selected[0]
    elif len(users) == 1:
        start = users[0]
    else:
        return reject("ambiguous_submission")
    turn = messages[start:]
    calls = {item["id"]: item.get("recipient") for item in turn
             if item["author"].get("role") == "assistant" and item.get("recipient", "all") != "all"}
    if any(calls.get(key) != value for key, value in (required_tool_calls or {}).items()):
        return reject("observed_calls_missing")
    tool_indexes = [i for i, item in enumerate(turn) if item["author"].get("role") == "tool"]
    info["tool_count"] = len(tool_indexes)
    if not set(required_tool_message_ids).issubset({turn[i]["id"] for i in tool_indexes}):
        return reject("observed_tools_missing")
    last = turn[-1]
    formal_final = (last["author"].get("role") == "assistant" and last.get("recipient", "all") == "all"
                    and last.get("channel") in ("final", None) and last.get("end_turn") is True
                    and _successful(last) and last["content"].get("content_type") in ("text", "multimodal_text")
                    and ("parts" in last["content"] or "text" in last["content"])
                    and last["metadata"].get("bidi_voice_fem_message") is not True)
    if expected_final_message_id and last["id"] != expected_final_message_id:
        return reject("final_identity_mismatch")
    refusal_reason = _final_delivery_rejection_reason(last, completed=formal_final)
    if refusal_reason:
        return reject(refusal_reason)
    if _pending_calls(turn):
        return reject("calls_pending")
    production = [i for i in tool_indexes if not _inspection(turn[i])]
    # 明确失败先于等待终态，避免失败的新工作被归为可重查的缺少完成信号。
    from services.protocol.final_image import is_image_generation_failure
    if production:
        if any(_failed(turn[i]) or _failed(turn[i]["metadata"]) for i in tool_indexes if i >= production[-1]):
            return reject("last_tool_failed")
        if is_image_generation_failure(turn[production[-1]]):
            return reject("generation_failed")
    if any(turn[i].get("status") not in {"finished_successfully", "failed", "error", "cancelled", "canceled"}
           for i in tool_indexes):
        return reject("tool_incomplete")

    completion_source = "final" if formal_final else ""
    if not completion_source and completed_final_message_id:
        indexes = [i for i, item in enumerate(turn) if item["id"] == completed_final_message_id]
        if len(indexes) == 1:
            tail = turn[indexes[0] + 1:]
            harmless_tail = all(item["author"]["role"] == "assistant"
                and item.get("recipient", "all") == "all" and _successful(item)
                and item["content"].get("content_type") in {"thoughts", "reasoning_recap"} for item in tail)
            if (harmless_tail and all(_successful(turn[i]) for i in tool_indexes)
                    and not _failed(turn[indexes[0]]) and not _failed(turn[indexes[0]]["metadata"])):
                completion_source = "stream_complete"
    if not completion_source and completion_receipt:
        from services.protocol.image_delivery import receipt_matches
        if receipt_matches(completion_receipt, document, submitted_message_id or turn[0]["id"]):
            completion_source = "stored_completion"
    if not completion_source:
        return reject("completion_missing" if tool_indexes else "generation_pending")
    refusal_reason = _final_delivery_rejection_reason(last, completed=True)
    if refusal_reason:
        return reject(refusal_reason)
    if not tool_indexes:
        return reject("no_image_generated")
    if not production:
        return reject("no_image_generated")
    index = production[-1]
    if any(item["author"]["role"] == "assistant" and not _successful(item)
           for item in turn[index + 1:-1]):
        return reject("tool_incomplete")
    # 查看图片不是新产物；但查看失败或未完成仍不能证明交付完成。
    if any(not _successful(turn[i]) for i in tool_indexes if i >= index):
        return reject("last_tool_failed")
    if is_image_generation_failure(turn[index]):
        return reject("generation_failed")
    inputs = _input_assets(turn[0])
    if inputs is None:
        return reject("invalid_input_assets")
    images = [(i, pointer) for i in production if (pointer := _image_pointer(turn[i], inputs)) is not None]
    if expected_image_message_id and (not images or turn[images[-1][0]]["id"] != expected_image_message_id):
        return reject("image_identity_mismatch")
    info["candidate_count"] = len(images)
    if completion_source == "stored_completion":
        # 恢复确认的是既有交付决定，不能重新挑最新图片从而把已选的A换成B。
        matches = [(i, pointer) for i, pointer in images
                   if turn[i]["id"] == completion_receipt["message_id"]
                   and pointer["asset_pointer"].split("://")[-1] == completion_receipt["asset_id"]]
        if len(matches) != 1 or not images or images[-1][0] != index:
            return reject("receipt_artifact_mismatch")
        selected_index, pointer = matches[0]
        item = turn[selected_index]
        image = {"id": item["id"], "author": dict(item["author"]),
                 "content": {"content_type": "multimodal_text", "parts": [pointer]}}
        return accept(TerminalImageDelivery("native_image", item["id"], image_message=image), completion_source)
    final = last if last["author"]["role"] == "assistant" and last["content"].get("content_type") in {"text", "multimodal_text"} else None
    try:
        assets, _ = _final_assets(final) if final else ([], False)
    except (ValueError, TypeError):
        return reject("invalid_final_reference")
    if assets:
        if len(assets) != 1:
            return reject("ambiguous_final_assets")
        kind, asset = assets[0]
        if kind == "sandbox":
            return accept(TerminalImageDelivery("sandbox_final", final["id"], path=asset), completion_source)
        if asset in inputs:
            return reject("input_asset_selected")
        matches = [(i, pointer) for i, pointer in images if _POINTER.fullmatch(pointer["asset_pointer"])[1] == asset]
        if len(matches) != 1 or not images or images[-1][0] != index:
            return reject("unbound_final_asset")
        selected_index, pointer = matches[0]
        item = turn[selected_index]
        image = {"id": item["id"], "author": dict(item["author"]),
                 "content": {"content_type": "multimodal_text", "parts": [pointer]}}
        return accept(TerminalImageDelivery("native_image", item["id"], image_message=image), completion_source)
    # 文案、语言和展示组件只用于显示，下载仍只使用本轮工具的可信文件身份。
    if final:
        text = "".join(part for part in final["content"].get("parts", []) if isinstance(part, str))
        if re.search(r"请选择.*(?:第一|第二|哪|其中)|choose (?:one|between)", text, re.I):
            return reject("ambiguous_final_choice")
    if images and images[-1][0] == index:
        result = turn[index]
        image = {"id": result["id"], "author": dict(result["author"]),
                 "content": {"content_type": "multimodal_text", "parts": [images[-1][1]]}}
        return accept(TerminalImageDelivery("native_image", result["id"], image_message=image), completion_source)
    result = turn[index]
    if images and index > images[-1][0]:
        name = result["author"].get("name")
        call = next((item for item in reversed(turn[:index])
                     if item["author"]["role"] == "assistant" and item.get("recipient") == name), None)
        if call:
            path = _python_path(call, result)
            if path:
                return accept(TerminalImageDelivery("python_output", result["id"], path=path), completion_source)
            path = _container_path(call, result)
            if path:
                return accept(TerminalImageDelivery("container_output", result["id"], path=path), completion_source)
        return reject("postprocess_artifact_unconfirmed")
    pointers = [p for p in result["content"].get("parts", []) if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer"]
    if len(pointers) > 1:
        return reject("ambiguous_tool_assets")
    if pointers and any(isinstance(p.get("asset_pointer"), str) and p["asset_pointer"].split("://")[-1] in inputs for p in pointers):
        return reject("input_asset_selected")
    return reject("artifact_missing")
