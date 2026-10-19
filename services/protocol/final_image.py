"""只解析上游消息与文件引用，不执行上游返回的 Python 或 DIL 代码。"""
from __future__ import annotations

import copy
import json
import re
from pathlib import PurePosixPath
from urllib.parse import unquote


SANDBOX_IMAGE_RE = re.compile(
    r'sandbox:(/mnt/data/[^\r\n<>"?]*?\.(?:png|jpe?g|webp|gif))(?=[)\]\s"<>]|$)', re.I
)


def validated_sandbox_image_path(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid sandbox image path")
    path = unquote(value)
    if (not path.startswith("/mnt/data/") or ".." in path.split("/")
            or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or re.search(r"%[0-9a-fA-F]{2}", path)
            or PurePosixPath(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}):
        raise ValueError("invalid sandbox image path")
    return str(PurePosixPath(path))


class FinalImageTracker:
    """跟踪本轮最终交付，避免语音转述覆盖已完成的图片链接。"""

    def __init__(self):
        self.current: dict = {}
        self.final: dict = {}
        self.generated = False
        self.postprocessing = False
        self.stream_complete = False
        self.skipped_mainline = False
        self.conversation_id = ""
        self.last_image_message_id = ""
        self.code_tool_after_image = False
        self.review_requested = False
        self.review_reason = ""
        self.expected_final_path = ""
        self.voice_transport = False
        self.voice_turn_complete = False
        self.voice_final_preserved = False
        self.tools: dict[str, int] = {}
        self._seen_tool_messages: set[str] = set()
        self._seen_execution_messages: set[str] = set()
        self._seen_thought_messages: set[str] = set()
        self.upstream_model_slug = ""
        self._has_resolved_model = False
        self._patch_path = ""
        self._patch_operation = ""
        self._batch_path = ""
        self._batch_operation = ""

    def update(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        self.voice_transport = self.voice_transport or event.get("_voice_transport") is True
        if event.get("type") == "voice_turn_complete":
            self.voice_transport = True
            self.voice_turn_complete = True
            self.stream_complete = True
        value = event.get("v")
        self.conversation_id = str(event.get("conversation_id") or self.conversation_id)
        if isinstance(value, dict):
            self.conversation_id = str(value.get("conversation_id") or self.conversation_id)
        if event.get("type") == "image_review_started":
            self.postprocessing = True
            self.review_requested = True
            self.review_reason = str(event.get("reason") or "")
            if event.get("expected_final_path"):
                self.expected_final_path = validated_sandbox_image_path(event["expected_final_path"])
            self.final = {}
            self.stream_complete = False
        if event.get("type") == "message_stream_complete":
            self.stream_complete = True
        message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
        if isinstance(message, dict):
            self.current = copy.deepcopy(message)
            # 省略的 p/o 只在同一条消息内继承，不能把旧消息的补丁写入新消息。
            self._patch_path = ""
            self._patch_operation = ""
            self._batch_path = ""
            self._batch_operation = ""
            self.stream_complete = False
            self.voice_turn_complete = False
            self._observe()
            return
        if "v" not in event:
            return
        path = event.get("p", self._patch_path)
        operation = event.get("o", self._patch_operation)
        # 老版本的事件表可能省略 patch 包装字段；只有明确的操作列表才补全它。
        # thoughts 等值数组即使元素是字典，也不能作为补丁逐项执行。
        if (not operation and "p" not in event and isinstance(value, list) and value
                and all(isinstance(item, dict) and isinstance(item.get("p"), str)
                        and isinstance(item.get("o"), str)
                        and item["o"] in {"append", "add", "replace"} for item in value)):
            operation = "patch"
        if (not isinstance(path, str) or not isinstance(operation, str)
                or operation not in {"patch", "append", "add", "replace"}):
            return
        self._patch_path = path
        self._patch_operation = operation
        if operation == "patch" and isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "v" in item:
                    child_path = item.get("p", self._batch_path)
                    child_op = item.get("o", self._batch_operation)
                    if (isinstance(child_path, str) and isinstance(child_op, str)
                            and child_op in {"append", "add", "replace"}):
                        self._batch_path = child_path
                        self._batch_operation = child_op
                        self._patch({"p": child_path, "o": child_op, "v": item["v"]})
        elif operation != "patch":
            self._patch({"p": path, "o": operation, "v": value})
        self._observe()

    def _observe(self) -> None:
        message = self.current
        author = message.get("author")
        author = author if isinstance(author, dict) else {}
        role = author.get("role")
        content = message.get("content")
        content = content if isinstance(content, dict) else {}
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        message_id = message.get("id")
        valid_id = isinstance(message_id, str) and 0 < len(message_id) <= 128
        tool = author.get("name")
        valid_tool = isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", tool)
        if role == "assistant":
            for key in ("resolved_model_slug", "model_slug"):
                model = metadata.get(key)
                if (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model)
                        and (key == "resolved_model_slug" or not self._has_resolved_model)):
                    self.upstream_model_slug = model
                    self._has_resolved_model = key == "resolved_model_slug"
                    break
            if content.get("content_type") == "thoughts" and valid_id:
                self._seen_thought_messages.add(message_id)
        if role == "assistant" and content.get("content_type") == "code":
            text = content.get("text")
            if isinstance(text, str) and len(text) <= 1024 and "skipped_mainline" in text:
                try:
                    marker = json.loads(text)
                    if isinstance(marker, dict) and marker.get("skipped_mainline") is True:
                        self.skipped_mainline = True
                except ValueError:
                    pass
        parts = content.get("parts")
        parts = parts if isinstance(parts, list) else []
        code_tools = {"container.exec", "python"}
        inspection_tools = code_tools | {"container.open_image"}
        if (role == "tool" and isinstance(tool, (str, type(None))) and tool not in inspection_tools
                and content.get("content_type") != "execution_output"
                and any(isinstance(p, dict) and p.get("content_type") == "image_asset_pointer" for p in parts)):
            self.generated = True
            message_id = str(message.get("id") or "")
            if message_id != self.last_image_message_id:
                self.code_tool_after_image = False
                self.last_image_message_id = message_id
        if role == "tool":
            if self.voice_transport:
                # 语音工具之后必须出现新的最终助手消息，不能交付工具之前的旧链接。
                self.final = {}
            if valid_tool and valid_id and message_id not in self._seen_tool_messages:
                self._seen_tool_messages.add(message_id)
                self.tools[tool] = self.tools.get(tool, 0) + 1
            # 别名不能靠名称猜测；execution_output 是上游明确的执行结果类型。
            # open_image 只证明查看了图片，既不生成新一轮图片，也不证明执行过代码。
            execution = bool(valid_tool and (tool in code_tools or
                             (content.get("content_type") == "execution_output" and tool != "container.open_image")))
            if execution and valid_id and message_id not in self._seen_execution_messages:
                self._seen_execution_messages.add(message_id)
                if self.generated:
                    self.code_tool_after_image = True
            if execution:
                self.postprocessing = True
        recipient = message.get("recipient")
        if self.voice_transport and (role == "user" or
                role == "assistant" and isinstance(recipient, str) and recipient != "all"):
            # 新的用户轮次或工具调用使旧交付失效，不能把上一轮图片当作本轮结果。
            self.final = {}
        if ((isinstance(recipient, str) and recipient in inspection_tools)
                or (valid_tool and tool in inspection_tools)):
            self.postprocessing = True
        # 部分网页流不发送 channel=final，而以 end_turn 标记最终文本。
        # 先保留候选，只有终态检查通过后才允许下载。
        if (role == "assistant" and message.get("channel") in ("final", None)
                and (content.get("content_type") == "text"
                     or self.voice_transport and content.get("content_type") == "multimodal_text")
                and message.get("recipient", "all") == "all"):
            if self._is_voice_narration_after_image(message):
                # 上游会在带 sandbox 链接的 text/final 后追加无链接的语音转述。
                # 保留真实产物的消息 ID，后续下载仍必须通过原有终态及图片校验。
                self.voice_final_preserved = True
            else:
                self.final = self.current
        elif self.final is self.current:
            self.final = {}

    def _is_voice_narration_after_image(self, message: dict) -> bool:
        if (not self.voice_transport or (message.get("content") or {}).get("content_type") != "multimodal_text"
                or not self.final or message.get("id") == self.final.get("id")
                or self.final.get("status") != "finished_successfully"):
            return False
        try:
            return bool(self._message_paths(self.final)) and not self._message_paths(message)
        except ValueError:
            # 非法路径仍交给原有验证报错，不能以旧图片掩盖最新消息的异常。
            return False

    def _patch(self, operation: dict) -> None:
        path = operation.get("p", "")
        op = operation.get("o")
        if (not isinstance(path, str) or not path.startswith("/message/") or len(path) > 2048
                or path.count("/") > 64 or not self.current or "v" not in operation
                or not isinstance(op, str) or op not in {"append", "add", "replace"}):
            return
        keys = [p.replace("~1", "/").replace("~0", "~") for p in path[len("/message/"):].split("/")]
        target = self.current
        try:
            for key in keys[:-1]:
                if isinstance(target, list):
                    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", key):
                        return
                    target = target[int(key)]
                else:
                    target = target.setdefault(key, {})
            key = keys[-1]
            if isinstance(target, list):
                if not re.fullmatch(r"0|[1-9][0-9]{0,9}", key):
                    return
                key = int(key)
            value = copy.deepcopy(operation.get("v"))
            if op == "append":
                old = target[key] if isinstance(target, list) else target.get(key)
                if isinstance(old, dict) and isinstance(value, dict):
                    old.update(value)
                elif isinstance(old, (str, list)) and isinstance(value, type(old)):
                    target[key] = old + value
                elif old is None:
                    target[key] = value
            elif op == "add" and isinstance(target, list):
                if key <= len(target):
                    target.insert(key, value)
            elif op in {"add", "replace"}:
                target[key] = value
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            # 不完整的非图片元数据不影响主消息；不会把其中的路径拿来执行。
            return

    def paths(self) -> list[str]:
        return self._message_paths(self.final)

    @staticmethod
    def _message_paths(message: dict) -> list[str]:
        if not message:
            return []
        content = message.get("content")
        content = content if isinstance(content, dict) else {}
        parts = content.get("parts", [])
        parts = parts if isinstance(parts, list) else []
        text = "\n".join(p for p in parts if isinstance(p, str))
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        dil = metadata.get("model_dil_v2")
        dil = dil if isinstance(dil, dict) else {}
        # 正文优先；DIL 仅读取声明性链接数据，不解释或运行 code 字段。
        text += "\n" + json.dumps({"fallback": dil.get("fallbackMarkdown"),
                                    "appData": dil.get("appData"),
                                    "references": metadata.get("content_references")}, ensure_ascii=False)
        paths = []
        for match in SANDBOX_IMAGE_RE.finditer(text):
            path = validated_sandbox_image_path(match.group(1))
            if path not in paths:
                paths.append(path)
        if len(paths) > 10:
            raise ValueError("too many final sandbox images")
        return paths

    def is_complete(self) -> bool:
        return bool(self.stream_complete and self.final.get("status") == "finished_successfully"
                    and (self.final.get("end_turn") is True or self.voice_turn_complete))

    def execution_summary(self, result_source: str, quality_status: str = "") -> dict:
        """日志仅保留可核验的工具计数与交付来源，不包含代码、凭据或内部推理。"""
        result = {"conversation_id": self.conversation_id, "tools": dict(self.tools),
                "code_tool_after_image": self.code_tool_after_image,
                "review_requested": self.review_requested, "review_reason": self.review_reason,
                "skipped_mainline": self.skipped_mainline, "result_source": result_source,
                "thought_message_count": len(self._seen_thought_messages),
                "stream_complete": self.stream_complete,
                "voice_final_preserved": self.voice_final_preserved,
                "upstream_model_slug": self.upstream_model_slug}
        if quality_status:
            result["quality_status"] = quality_status
        return result
