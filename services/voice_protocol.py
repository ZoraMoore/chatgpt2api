"""纯代码语音协议辅助函数；不包含账号存储和任务调度。"""
from __future__ import annotations
import json
import re
import ipaddress
from datetime import datetime
from zoneinfo import ZoneInfo
from aiortc.sdp import SessionDescription

def sdp_summary(sdp: str) -> dict:
    """仅保留协商结构，排除 ICE 密码、证书指纹和地址。"""
    lines = sdp.splitlines()
    candidates = []
    for line in lines:
        fields = line.split()
        if not line.startswith("a=candidate:") or len(fields) < 8:
            continue
        try:
            global_address = ipaddress.ip_address(fields[4]).is_global
        except ValueError:
            global_address = None
        candidates.append({"transport": fields[2].lower(), "public_address": global_address,
                           "port": fields[5], "type": fields[7]})
    return {"media": [line.split()[:4] for line in lines if line.startswith("m=")],
            "directions": [line[2:] for line in lines if line in {"a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"}],
            "candidate_count": sum(line.startswith("a=candidate:") for line in lines),
            "candidates": candidates,
            "ice_lite": "a=ice-lite" in lines,
            "setup": [line[8:] for line in lines if line.startswith("a=setup:")],
            "bundle": [line[2:] for line in lines if line.startswith("a=group:BUNDLE")],
            "sctp": [line for line in lines if line.startswith(("a=sctp-port:", "a=max-message-size:"))]}

def normalize_bundle_candidates(answer: str) -> tuple[str, list[dict], int]:
    """兼容只在 BUNDLE 主媒体段声明候选的 SDP，保留原始属性与凭据。"""
    description = SessionDescription.parse(answer)
    summary = [{"mid": media.rtp.muxId, "kind": media.kind, "candidates": len(media.ice_candidates),
                "end_of_candidates": media.ice_candidates_complete} for media in description.media]
    bundle = next((group for group in description.group if group.semantic == "BUNDLE" and group.items), None)
    if not bundle:
        return answer, summary, 0
    primary = next((media for media in description.media if media.rtp.muxId == bundle.items[0]), None)
    if not primary or not primary.ice_candidates or not primary.ice.usernameFragment or not primary.ice.password:
        return answer, summary, 0
    missing = {media.rtp.muxId for media in description.media
               if media.rtp.muxId in bundle.items and not media.ice_candidates
               and media.ice.usernameFragment == primary.ice.usernameFragment
               and media.ice.password == primary.ice.password}
    if not missing:
        return answer, summary, 0
    sections: list[list[str]] = [[]]
    for line in answer.splitlines():
        if line.startswith("m="):
            sections.append([])
        sections[-1].append(line)
    source = next((section for section in sections if "a=mid:" + primary.rtp.muxId in section), [])
    candidates = [line for line in source if line.startswith("a=candidate:")]
    if not candidates:
        return answer, summary, 0
    for section in sections:
        if any("a=mid:" + mid in section for mid in missing):
            section.extend(candidates)
    return "\r\n".join(line for section in sections for line in section) + "\r\n", summary, len(missing)

def unpack_event(raw: object) -> dict:
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        return {}
    try:
        outer = json.loads(raw)
        value = json.loads(outer["data"]) if isinstance(outer, dict) and outer.get("type") == "data_message" else outer
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, KeyError):
        return {}

def pack_event(kind: str, payload: dict) -> str:
    return json.dumps({"type": "data_message", "data": json.dumps({"type": kind, "payload": payload}, separators=(",", ":"))}, separators=(",", ":"))

def visible_text(content: dict) -> str:
    """语音文本可能是 multimodal_text 中的文本片段，只读取面向用户的文本。"""
    if not isinstance(content, dict) or content.get("content_type") not in {"text", "multimodal_text"}:
        return ""
    parts = content.get("parts") or []
    values = []
    for part in parts:
        if isinstance(part, str):
            values.append(part)
        elif isinstance(part, dict) and part.get("content_type") in {"text", "audio_transcription"} and isinstance(part.get("text"), str):
            values.append(part["text"])
    if not values and isinstance(content.get("text"), str):
        values.append(content["text"])
    return "".join(values)

def session_payload(route: str, model: str, session_id: str) -> dict:
    if route not in {"vp", "wm"} or model not in {"gpt-6-thinking", "gpt-6", "auto", "gpt-4o"}:
        raise ValueError("invalid_voice_mode")
    # 按网页 Fei 构造器区分普通 advanced 与 wingman，不能把 bidi 元数据当作模型名。
    offset = -int(datetime.now(ZoneInfo("America/Los_Angeles")).utcoffset().total_seconds() / 60)
    value = {"voice": "maple", "voice_session_id": session_id, "voice_status_request_id": session_id,
             "timezone": "America/Los_Angeles", "timezone_offset_min": offset, "user_locale": "zh-CN",
             "voice_mode": "advanced" if route == "vp" else "wingman", "model_slug": model if route == "vp" else "",
             "requested_default_model": model if route == "vp" else "", "client_tools": [],
             "history_and_training_disabled": False, "conversation_mode": {"kind": "primary_assistant"},
             "chat_mode": "chat", "backend_model": model, "backend_thinking_effort": "standard",
             "enable_message_streaming": True, "model_speaks_first": False, "deprecate_celsius": False}
    if route == "vp":
        value["thinking_effort"] = "standard"
    else:
        value["backend_reasoning_effort"] = "instant"
        value["model_slug_advanced"] = ""
    return value
