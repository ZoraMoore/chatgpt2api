"""本轮图片交付证据：按消息更新、排除输入资产，不保留凭据或执行上游代码。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re

from services.protocol.terminal_image_delivery import _branch, _input_assets, _valid_id, _failed, _POINTER


def delivery_context(backend, request) -> dict:
    saved = request.delivery_context if isinstance(request.delivery_context, dict) else {}
    submitted = saved.get("submitted_message_id") or getattr(backend, "_image_submitted_message_id", "")
    if not _valid_id(submitted):
        return {}
    if (not isinstance(saved.get("input_asset_ids", []), list)
            or not isinstance(saved.get("input_image_hashes", []), list)):
        raise ValueError("invalid image delivery context")
    assets = list(saved.get("input_asset_ids") or [])
    uploaded = getattr(backend, "_image_input_asset_ids", [])
    if isinstance(uploaded, list):
        assets.extend(uploaded)
    hashes = list(saved.get("input_image_hashes") or [])
    for image in request.images or []:
        if isinstance(image, str):
            try:
                hashes.append(hashlib.sha256(base64.b64decode(image, validate=True)).hexdigest())
            except (ValueError, TypeError):
                continue
    if (len(assets) > 40 or len(hashes) > 20 or any(not _valid_id(x) for x in assets)
            or any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", x) for x in hashes)):
        raise ValueError("invalid image delivery context")
    assets = list(dict.fromkeys(assets))
    hashes = list(dict.fromkeys(x.lower() for x in hashes))
    if len(assets) > 20 or len(hashes) > 10:
        raise ValueError("too many input identities")
    result = {"submitted_message_id": submitted, "input_asset_ids": assets, "input_image_hashes": hashes}
    receipt = validated_completion_receipt(getattr(backend, "_image_completion_receipt", None)) or validated_completion_receipt(saved.get("completion_receipt"))
    if receipt and receipt["submitted_message_id"] == submitted:
        result["completion_receipt"] = receipt
    observed = saved.get("observed_evidence")
    current = getattr(backend, "_image_observed_evidence", None)
    if isinstance(current, dict):
        observed = merge_observed_evidence(observed, current)
    if observed is not None:
        checked = validated_observed_evidence(observed)
        if not checked:
            raise ValueError("invalid observed image evidence")
        result["observed_evidence"] = checked
    return result


def validated_observed_evidence(value) -> dict:
    """可落盘的最小工作身份；明确拒绝正文、代码和凭据等额外字段。"""
    fields = {"version", "conversation_id", "tool_ids", "tool_calls", "asset_ids_by_message", "failed_message_ids"}
    if not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int or value["version"] != 1:
        return {}
    if not _valid_id(value["conversation_id"]):
        return {}
    for key in ("tool_ids", "failed_message_ids"):
        rows = value[key]
        if not isinstance(rows, list) or len(rows) > 256 or any(not _valid_id(item) for item in rows):
            return {}
    calls, assets = value["tool_calls"], value["asset_ids_by_message"]
    if (not isinstance(calls, dict) or len(calls) > 256
            or any(not _valid_id(k) or not _valid_id(v) or v == "all" for k, v in calls.items())
            or not isinstance(assets, dict) or len(assets) > 256):
        return {}
    if not set(assets).issubset(value["tool_ids"]) or not set(value["failed_message_ids"]).issubset(set(value["tool_ids"]) | set(calls)):
        return {}
    for rows in assets.values():
        if (not isinstance(rows, list) or len(rows) > 16
                or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in rows)):
            return {}
    if len(json.dumps(value)) > 131072:
        return {}
    return copy.deepcopy(value)


def observed_evidence(messages, conversation_id: str) -> dict:
    tools, calls, assets, failed = [], {}, {}, []
    for message in messages:
        author = message.get("author") or {}
        role, mid = author.get("role"), message.get("id")
        if role == "tool":
            tools.append(mid)
            pointers = []
            for part in (message.get("content") or {}).get("parts", []):
                if isinstance(part, dict) and isinstance(part.get("asset_pointer"), str):
                    match = _POINTER.fullmatch(part["asset_pointer"])
                    if match:
                        pointers.append(match[1])
            if pointers:
                assets[mid] = sorted(set(pointers))
        elif role == "assistant" and message.get("recipient", "all") != "all":
            calls[mid] = message["recipient"]
        else:
            continue
        if _failed(message) or _failed(message.get("metadata") or {}):
            failed.append(mid)
    result = validated_observed_evidence({"version": 1, "conversation_id": conversation_id,
        "tool_ids": list(dict.fromkeys(tools)), "tool_calls": calls, "asset_ids_by_message": assets,
        "failed_message_ids": list(dict.fromkeys(failed))})
    if not result:
        raise ValueError("observed image evidence exceeds limits or is invalid")
    return result


def merge_observed_evidence(previous, current: dict) -> dict:
    current = validated_observed_evidence(current)
    if not current:
        raise ValueError("invalid observed image evidence")
    if previous is None:
        return current
    previous = validated_observed_evidence(previous)
    if not previous or previous["conversation_id"] != current["conversation_id"]:
        raise ValueError("observed image conversation mismatch")
    for key in ("tool_calls", "asset_ids_by_message"):
        if any(k in current[key] and current[key][k] != v for k, v in previous[key].items()):
            raise ValueError("observed image work changed")
        current[key] = previous[key] | current[key]
    for key in ("tool_ids", "failed_message_ids"):
        current[key] = list(dict.fromkeys(previous[key] + current[key]))
    if not validated_observed_evidence(current):
        raise ValueError("observed image evidence exceeds limits")
    return current


def observed_snapshot_reason(document: dict, context: dict) -> str:
    stored = context.get("observed_evidence")
    if stored is None:
        return ""
    stored = validated_observed_evidence(stored)
    if not stored:
        return "invalid_observed_evidence"
    if any(document.get(k) != stored["conversation_id"] for k in ("id", "conversation_id") if k in document):
        return "conversation_mismatch"
    turn = scope_turn(document, context.get("submitted_message_id", ""))
    if not turn:
        return "submission_mismatch"
    actual = observed_evidence(turn, stored["conversation_id"])
    if not set(stored["tool_ids"]).issubset(actual["tool_ids"]):
        return "observed_tools_missing"
    if any(actual["tool_calls"].get(k) != v for k, v in stored["tool_calls"].items()):
        return "observed_calls_missing"
    if any(actual["asset_ids_by_message"].get(k) != v for k, v in stored["asset_ids_by_message"].items()):
        return "snapshot_asset_changed"
    if not set(stored["failed_message_ids"]).issubset(actual["failed_message_ids"]):
        return "snapshot_regressed_failure"
    return ""


def semantic_message(message: dict) -> dict:
    """只比较会改变工作/交付的事实，忽略耗时等观测元数据。"""
    author = message.get("author") or {}
    content = copy.deepcopy(message.get("content") or {})
    parts = content.get("parts", [])
    if isinstance(parts, list):
        content["parts"] = [{"content_type": "image_asset_pointer", "asset_pointer": p.get("asset_pointer", "").split("://")[-1]}
                            if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer"
                            and isinstance(p.get("asset_pointer"), str) else p for p in parts]
    metadata = message.get("metadata") or {}
    keys = ("aggregate_result", "parent_id", "request_id", "turn_exchange_id", "working_turn_id",
            "content_references", "model_dil_v2", "refusal", "is_refusal")
    return {"id": message.get("id"), "role": author.get("role"), "name": author.get("name"),
            "recipient": message.get("recipient", "all"), "channel": message.get("channel"),
            "status": message.get("status"), "end_turn": message.get("end_turn"), "content": content,
            "failed": _failed(message) or _failed(metadata), "is_complete": metadata.get("is_complete", True),
            "metadata": {key: metadata[key] for key in keys if key in metadata}}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def scope_turn(document: dict, submitted_id: str = "") -> list[dict] | None:
    branch = _branch(document) if isinstance(document, dict) else None
    if not branch:
        return None
    users = [i for i, item in enumerate(branch) if item["author"]["role"] == "user"]
    if not users or submitted_id and branch[users[-1]]["id"] != submitted_id or not submitted_id and len(users) != 1:
        return None
    return branch[users[-1]:]


def _work_digest(turn: list[dict]) -> str:
    return _digest([semantic_message(item) for item in turn if item["author"]["role"] == "tool"
                    or item["author"]["role"] == "assistant" and item.get("recipient", "all") != "all"])


def validated_completion_receipt(value) -> dict:
    fields = {"version", "conversation_id", "submitted_message_id", "message_id", "asset_id", "work_sha256", "anchor_id", "anchor_sha256"}
    if not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int or value["version"] != 1:
        return {}
    if any(not _valid_id(value[k]) for k in ("conversation_id", "submitted_message_id", "message_id", "asset_id", "anchor_id")):
        return {}
    if any(not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k]) for k in ("work_sha256", "anchor_sha256")):
        return {}
    return dict(value)


def make_completion_receipt(document: dict, submitted_id: str, selected) -> dict:
    # 只持久化已确认完成的原生图片身份与工作摘要；不保存提示词、代码或令牌。
    if selected.kind != "native_image":
        return {}
    turn = scope_turn(document, submitted_id)
    if not turn:
        return {}
    pointer = selected.image_message["content"]["parts"][0]["asset_pointer"]
    return validated_completion_receipt({"version": 1, "conversation_id": document.get("conversation_id") or document.get("id"),
        "submitted_message_id": turn[0]["id"], "message_id": selected.message_id, "asset_id": pointer.split("://")[-1],
        "work_sha256": _work_digest(turn), "anchor_id": turn[-1]["id"], "anchor_sha256": _digest(semantic_message(turn[-1]))})


def receipt_matches(receipt, document, submitted_id: str) -> bool:
    from services.protocol.terminal_image_delivery import _image_pointer, _successful
    receipt = validated_completion_receipt(receipt)
    if (not receipt or receipt["submitted_message_id"] != submitted_id
            or any(document[k] != receipt["conversation_id"] for k in ("id", "conversation_id") if k in document)):
        return False
    turn = scope_turn(document, submitted_id)
    if not turn or _work_digest(turn) != receipt["work_sha256"]:
        return False
    inputs = _input_assets(turn[0])
    if inputs is None or receipt["asset_id"] in inputs:
        return False
    artifact = next((m for m in turn if m["id"] == receipt["message_id"]), None)
    pointer = _image_pointer(artifact, inputs) if artifact else None
    if not pointer or pointer["asset_pointer"].split("://")[-1] != receipt["asset_id"]:
        return False
    anchor = next((m for m in turn if m["id"] == receipt["anchor_id"]), None)
    if anchor is not None and _digest(semantic_message(anchor)) != receipt["anchor_sha256"]:
        return False
    # 允许历史接口省略已确认的空终态，但不允许其后出现新的对外回复或未完成摘要。
    work_end = max(i for i, m in enumerate(turn) if m["author"]["role"] == "tool")
    for item in turn[work_end + 1:]:
        if item is anchor:
            continue
        if (item["author"]["role"] != "assistant" or item.get("recipient", "all") != "all"
                or item["content"].get("content_type") not in {"thoughts", "reasoning_recap"}
                or not _successful(item)):
            return False
    last = turn[-1]
    if last["author"]["role"] == "tool":
        return _successful(last)
    if last["author"]["role"] == "assistant" and last.get("recipient", "all") == "all":
        if last["content"].get("content_type") in {"thoughts", "reasoning_recap"}:
            return _successful(last)
        return last["id"] == receipt["anchor_id"] and _digest(semantic_message(last)) == receipt["anchor_sha256"]
    return False


class ImageDeliveryEvidence:
    def __init__(self):
        self.messages = {}
        self.sizes = {}
        self.total_bytes = 0
        self.conversation_id = ""
        self.invalid = False
        self.completed_final = None
        self.done_received = False
        self.revision = 0
        self.completion_revision = -1

    def observe(self, message: dict, conversation_id: str) -> None:
        if conversation_id:
            if self.conversation_id and conversation_id != self.conversation_id:
                self.invalid = True
            self.conversation_id = conversation_id
        if not isinstance(message, dict) or not _valid_id(message.get("id")):
            return
        if (not isinstance(message.get("author"), dict) or not isinstance(message.get("content"), dict)
                or not isinstance(message.get("metadata", {}), dict)):
            self.invalid = True
            return
        mid = message["id"]
        size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        self.total_bytes += size - self.sizes.get(mid, 0)
        if self.total_bytes > 8 * 1024 * 1024 or mid not in self.messages and len(self.messages) >= 4096:
            self.invalid = True
            return
        prior = self.messages.get(mid)
        if prior is None or semantic_message(prior) != semantic_message(message):
            self.revision += 1
            self.completed_final = None
        self.sizes[mid] = size
        # 同一消息更新原位；重复旧快照不能改变当前分支顺序或抹去完成证明。
        self.messages[mid] = copy.deepcopy(message)

    def complete(self, event: dict, message: dict) -> None:
        if event.get("type") == "image_review_started":
            self.revision += 1
            self.completed_final = None
            return
        if event.get("type") != "message_stream_complete":
            return
        cid = event.get("conversation_id", self.conversation_id)
        if not self.conversation_id or cid != self.conversation_id:
            self.invalid = True
            return
        if isinstance(message, dict) and _valid_id(message.get("id")):
            self.completed_final = copy.deepcopy(message)
            self.completion_revision = self.revision

    def completed_final_id(self) -> str:
        if not self.invalid and self.done_received and self.completed_final and self.completion_revision == self.revision:
            return self.completed_final["id"]
        return ""

    def document(self, conversation_id: str, context: dict, diagnostic: dict | None = None) -> dict:
        if self.invalid or self.conversation_id and self.conversation_id != conversation_id:
            return {}
        messages = list(self.messages.values())
        submitted = context.get("submitted_message_id", "")
        if submitted and not any(m.get("author", {}).get("role") == "user" for m in messages):
            messages.insert(0, {"id": submitted, "author": {"role": "user"}, "metadata": {},
                               "content": {"content_type": "text", "parts": []}})
        return bind_input_assets({"conversation_id": conversation_id, "messages": messages}, context, diagnostic=diagnostic)

    def snapshot_reason(self, document: dict, context: dict) -> str:
        branch = scope_turn(document, context.get("submitted_message_id", ""))
        if not branch:
            return "invalid_branch"
        by_id = {m["id"]: m for m in branch}
        for mid, observed in self.messages.items():
            role = observed["author"]["role"]
            relevant = role == "tool" or role == "assistant" and (
                observed.get("recipient", "all") != "all" or observed.get("channel") == "final")
            if not relevant:
                continue
            snapshot = by_id.get(mid)
            if snapshot is None:
                return "snapshot_messages_missing"
            if _failed(observed) or _failed(observed.get("metadata") or {}):
                if not (_failed(snapshot) or _failed(snapshot.get("metadata") or {})):
                    return "snapshot_regressed_failure"
            content = observed.get("content") or {}
            parts = content.get("parts", [])
            pointers = {p["asset_pointer"].split("://")[-1] for p in parts if isinstance(p, dict)
                        and p.get("content_type") == "image_asset_pointer" and isinstance(p.get("asset_pointer"), str)}
            current_pointers = {p["asset_pointer"].split("://")[-1] for p in snapshot["content"].get("parts", [])
                                if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer"
                                and isinstance(p.get("asset_pointer"), str)}
            if pointers and pointers != current_pointers:
                return "snapshot_asset_changed"
            if role == "assistant" and observed.get("recipient", "all") == "all":
                old_text = "".join(p for p in parts if isinstance(p, str)) + str(content.get("text") or "")
                new_text = "".join(p for p in snapshot["content"].get("parts", []) if isinstance(p, str)) + str(snapshot["content"].get("text") or "")
                if old_text and not new_text.startswith(old_text):
                    return "snapshot_final_regressed"
        return ""

    def reconcile_snapshot(self, document: dict, context: dict) -> dict:
        """历史可省略空final；只补回已亲自收到且工作摘要完全一致的原始消息。"""
        anchor = self.completed_final
        if not self.completed_final_id() or not isinstance(anchor, dict):
            return document
        content, metadata = anchor.get("content") or {}, anchor.get("metadata") or {}
        if (anchor.get("author", {}).get("role") != "assistant" or anchor.get("recipient", "all") != "all"
                or anchor.get("channel") != "final" or content.get("content_type") != "text"
                or any(not isinstance(p, str) or p.strip() for p in content.get("parts", []))
                or content.get("text", "").strip() or _failed(anchor) or _failed(metadata)
                or metadata.get("model_dil_v2") or metadata.get("content_references")
                or metadata.get("refusal") or metadata.get("is_refusal")):
            return document
        if (not context.get("submitted_message_id")
                or not any(k in document for k in ("id", "conversation_id"))
                or any(document[k] != self.conversation_id for k in ("id", "conversation_id") if k in document)):
            return document
        turn = scope_turn(document, context["submitted_message_id"])
        # 原身份已由GET的当前分支验证；只比较直播工作内容，临时user ID不能抹掉已收到的完成消息。
        live = _branch({"messages": list(self.messages.values())})
        if (not turn or not live or any(item["id"] == anchor["id"] for item in turn)
                or _work_digest(turn) != _work_digest(live)):
            return document
        last = turn[-1]
        if (last["author"]["role"] != "tool" and not (last["author"]["role"] == "assistant"
                and last.get("recipient", "all") == "all"
                and last["content"].get("content_type") in {"thoughts", "reasoning_recap"})):
            return document
        return {key: value for key, value in document.items() if key not in ("mapping", "current_node", "messages")} | {
            "messages": [*turn, copy.deepcopy(anchor)]}


def bind_input_assets(document: dict, context: dict, diagnostic: dict | None = None) -> dict:
    """在选中原轮次的用户消息上补充上传时的身份，metadata回引不能抹去排除证据。"""
    info = diagnostic if diagnostic is not None else {}
    info.clear()
    def reject(reason):
        info["reason"] = reason
        return document if isinstance(document, dict) else {}
    if not isinstance(document, dict):
        return reject("invalid_document")
    branch = _branch(document)
    if not branch:
        return reject("invalid_branch")
    users = [m for m in branch if m["author"]["role"] == "user"]
    submitted = context.get("submitted_message_id", "")
    if submitted:
        if not users:
            return reject("missing_submission")
        if users[-1]["id"] != submitted:
            info["submission_hash"] = _digest(submitted)
            info["observed_submission_hash"] = _digest(users[-1]["id"])
            return reject("submission_mismatch")
    elif len(users) != 1:
        return reject("missing_submission" if not users else "submission_mismatch")
    original = users[-1]
    inputs = _input_assets(original)
    if inputs is None:
        # 只记录结构分类，不把输入指针、文件名或原文写入诊断。
        attachments = original["metadata"].get("attachments", [])
        info["attachments_type"] = type(attachments).__name__
        info["input_pointer_invalid"] = any(isinstance(p, dict) and "asset_pointer" in p
            and (not isinstance(p["asset_pointer"], str) or _POINTER.fullmatch(p["asset_pointer"]) is None)
            for p in original["content"].get("parts", []))
        return reject("input_assets_invalid")
    supplied = context.get("input_asset_ids", [])
    if not isinstance(supplied, list) or len(supplied) > 20 or any(not _valid_id(x) for x in supplied):
        return reject("context_assets_invalid")
    inputs.update(supplied)
    messages = copy.deepcopy(branch[branch.index(original):])
    user = messages[0]
    user["metadata"]["attachments"] = [{"id": value} for value in sorted(inputs)]
    return {key: value for key, value in document.items() if key not in ("mapping", "current_node", "messages")} | {
        "messages": messages}
