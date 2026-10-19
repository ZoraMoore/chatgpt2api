"""WebImage 普通会话图片协议；不依赖语音消息、上传流程或 WebRTC。

协议参考 239573049/WebImage@c414463237e172796a62f82b521410b1023520df。
原项目 MIT 许可及版权声明见 third_party/webimage/LICENSE。
保留本地 Sentinel 求解、出口时区和终态交付，不引入参考项目的生成 POST 重试。
"""
from __future__ import annotations

import json
import math
import re
import time
from io import BytesIO
from urllib.parse import urlsplit

from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import RequestException, Timeout
from PIL import Image

from services.config import config
from services.protocol.final_image import FinalImageTracker
from utils.helper import MAX_JSON_IMAGE_BYTES, ensure_ok, iter_sse_payloads, new_uuid, split_image_model


_CODEX_HEADERS = {"originator", "x-openai-codex-window-type", "x-openai-web-frontend",
                  "x-oai-mcp-form-version", "chatgpt-account-id", "oai-did"}


def _conduit_token(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 65536
            or not value.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise RuntimeError("image conversation prepare returned invalid conduit token")
    return value


class WebImageConversationTransport:
    def __init__(self, backend):
        if backend.image_transport != "conversation" or getattr(backend, "_voice_image_session", None) is not None:
            raise RuntimeError("WebImage transport requires an independent conversation backend")
        self.backend = backend
        self.session = backend.session
        # 后端实例由每个图片任务独占；只清理本实例的网页头，不能修改配置或语音实例。
        for name in list(self.session.headers):
            if name.lower() in _CODEX_HEADERS:
                self.session.headers.pop(name)
        self.session.headers.update({"OAI-Device-Id": backend.device_id, "OAI-Session-Id": backend.session_id,
                                     "OAI-Client-Version": backend.client_version,
                                     "OAI-Client-Build-Number": backend.client_build_number})
        self.parent_id = new_uuid()
        self.message_id = new_uuid()
        self.turn_id = new_uuid()

    def headers(self, path, requirements=None, conduit="", *, stream=False):
        headers = dict(self.session.headers)
        headers.update({"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "*/*",
                        "X-OpenAI-Target-Path": path, "X-OpenAI-Target-Route": path})
        if requirements:
            headers["OpenAI-Sentinel-Chat-Requirements-Token"] = requirements.token
            if requirements.proof_token:
                headers["OpenAI-Sentinel-Proof-Token"] = requirements.proof_token
        if conduit:
            headers["X-Conduit-Token"] = conduit
        if stream:
            headers["X-Oai-Turn-Trace-Id"] = self.turn_id
        return headers

    @staticmethod
    def model_settings(model):
        _, base = split_image_model(model)
        # 2.5 是本项目原有公开别名，两者都走 picture_v2 自动路由，不改语音的模型映射。
        if base not in {"gpt-image-2", "gpt-image-2.5"}:
            raise ValueError("unsupported WebImage conversation model")
        effort = str(config.data.get("default_thinking_effort") or "").strip().lower()
        return "auto", effort if effort in {"standard", "extended", "max"} else ""

    def upload(self, value, filename):
        if not isinstance(value, str) or len(value) > MAX_JSON_IMAGE_BYTES * 4 // 3 + 1024:
            raise ValueError("invalid reference image")
        data = self.backend._decode_image_base64(value)
        if not data or len(data) > MAX_JSON_IMAGE_BYTES:
            raise ValueError("invalid reference image size")
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            mime = Image.MIME.get(image.format)
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"} or width < 1 or height < 1:
                raise ValueError("unsupported reference image")
            image.verify()
        path = "/backend-api/files"
        response = self.session.post(self.backend.base_url + path, headers=self.headers(path),
                                     json={"file_name": filename, "file_size": len(data), "use_case": "multimodal",
                                           "width": width, "height": height}, timeout=60)
        try:
            ensure_ok(response, path)
            meta = response.json()
        finally:
            response.close()
        file_id = meta.get("file_id") if isinstance(meta, dict) else None
        if not isinstance(file_id, str) or not re.fullmatch(r"file[-_][A-Za-z0-9_-]{1,200}", file_id):
            raise RuntimeError("invalid uploaded image id")
        upload_url = meta.get("upload_url")
        if not isinstance(upload_url, str) or len(upload_url) > 8192 or any(ord(c) < 33 for c in upload_url):
            raise RuntimeError("invalid image upload URL")
        parsed = urlsplit(upload_url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not (host == "files.oaiusercontent.com" or host.endswith(".oaiusercontent.com")
                        or host.endswith(".blob.core.windows.net"))):
            raise RuntimeError("invalid image upload URL")
        # 文件 PUT 使用签名 URL；不要让 Session 默认 Bearer 泄露给存储域名。
        private_headers = {name: self.session.headers.pop(name) for name in list(self.session.headers)
                           if name.lower() in {"authorization", "cookie", "chatgpt-account-id"}}
        try:
            transfer = self.backend._request_image_transfer(
                "PUT", upload_url, "image_upload", allow_redirects=False,
                headers={"Content-Type": mime, "x-ms-blob-type": "BlockBlob", "x-ms-version": "2020-04-08",
                         "Origin": self.backend.base_url, "Referer": self.backend.base_url + "/",
                         "User-Agent": self.backend.user_agent}, data=data)
            try:
                if not 200 <= transfer.status_code < 300:
                    raise RuntimeError("image upload did not complete")
            finally:
                transfer.close()
        finally:
            self.session.headers.update(private_headers)
        path = f"/backend-api/files/{file_id}/uploaded"
        response = self.session.post(self.backend.base_url + path, headers=self.headers(path), data="{}", timeout=60)
        try:
            ensure_ok(response, path)
        finally:
            response.close()
        return {"file_id": file_id, "file_name": filename, "file_size": len(data), "mime_type": mime,
                "width": width, "height": height}

    def prepare(self, prompt, requirements, model):
        upstream_model, effort = self.model_settings(model)
        path = "/backend-api/f/conversation/prepare"
        payload = {"action": "next", "fork_from_shared_post": False, "parent_message_id": self.parent_id,
                   "model": upstream_model, "client_prepare_state": "success", **self.backend._timezone_fields(),
                   "conversation_mode": {"kind": "primary_assistant"}, "system_hints": ["picture_v2"],
                   "partial_query": {"id": self.message_id, "author": {"role": "user"},
                                     "content": {"content_type": "text", "parts": [prompt]}},
                   "supports_buffering": True, "supported_encodings": ["v1"],
                   "client_contextual_info": {"app_name": "chatgpt.com"}}
        if effort:
            payload["thinking_effort"] = effort
        response = self.session.post(self.backend.base_url + path, headers=self.headers(path, requirements),
                                     json=payload, timeout=60)
        try:
            ensure_ok(response, path)
            body = response.json()
            return _conduit_token(body.get("conduit_token") if isinstance(body, dict) else None)
        finally:
            response.close()

    def payload(self, prompt, model, references):
        upstream_model, effort = self.model_settings(model)
        parts = [{"content_type": "image_asset_pointer", "asset_pointer": f"file-service://{item['file_id']}",
                  "width": item["width"], "height": item["height"], "size_bytes": item["file_size"]}
                 for item in references] + [prompt]
        metadata = {"developer_mode_connector_ids": [], "selected_github_repos": [],
                    "selected_all_github_repos": False, "system_hints": ["picture_v2"],
                    "serialization_metadata": {"custom_symbol_offsets": []}}
        if references:
            metadata["attachments"] = [{"id": item["file_id"], "mimeType": item["mime_type"], "name": item["file_name"],
                                        "size": item["file_size"], "width": item["width"], "height": item["height"]}
                                       for item in references]
        payload = {"action": "next", "messages": [{"id": self.message_id, "author": {"role": "user"},
                    "create_time": time.time(), "content": {"content_type": "multimodal_text" if references else "text",
                                                            "parts": parts}, "metadata": metadata}],
                   "parent_message_id": self.parent_id, "model": upstream_model, "client_prepare_state": "sent",
                   **self.backend._timezone_fields(), "conversation_mode": {"kind": "primary_assistant"},
                   "enable_message_followups": True, "system_hints": ["picture_v2"], "supports_buffering": True,
                   "supported_encodings": ["v1"], "client_contextual_info": {"is_dark_mode": False,
                       "time_since_loaded": 1200, "page_height": 1072, "page_width": 1724, "pixel_ratio": 1.2,
                       "screen_height": 1440, "screen_width": 2560, "app_name": "chatgpt.com"},
                   "paragen_cot_summary_display_override": "allow", "force_parallel_switch": "auto"}
        if effort:
            payload["thinking_effort"] = effort
        return payload

    def generate(self, payload, requirements, conduit, timeout):
        path = "/backend-api/f/conversation"
        previous = self.session.curl_options.get(CurlOpt.TIMEOUT_MS)
        had_timeout = CurlOpt.TIMEOUT_MS in self.session.curl_options
        self.session.curl_options[CurlOpt.TIMEOUT_MS] = max(1, int(timeout * 1000))
        response = None
        # 在调用网络之前置位：发送异常不能证明远端未接单，账号层也不得换号重发。
        self.backend._webimage_generation_sent = True
        self.backend._image_submitted_message_id = self.message_id
        try:
            response = self.session.post(self.backend.base_url + path,
                                         headers=self.headers(path, requirements, conduit, stream=True), json=payload,
                                         timeout=(30, timeout), stream=True)
            if not 200 <= response.status_code < 300:
                body = bytearray()
                for chunk in response.iter_content():
                    body.extend(chunk[:65536 - len(body)])
                    if len(body) >= 65536:
                        break
                response.content = bytes(body)
            ensure_ok(response, path)
            state = FinalImageTracker()
            for value in iter_sse_payloads(response):
                if value == "[DONE]":
                    yield value
                    break
                try:
                    event = json.loads(value)
                except ValueError:
                    event = None
                if isinstance(event, dict):
                    state.update(event)
                    if state.conversation_id:
                        self.backend.image_conversation_id = state.conversation_id
                yield value
        finally:
            if response is not None:
                response.close()
            if had_timeout:
                self.session.curl_options[CurlOpt.TIMEOUT_MS] = previous
            else:
                self.session.curl_options.pop(CurlOpt.TIMEOUT_MS, None)


def stream_webimage_conversation(backend, prompt, model, images):
    from services.openai_backend_api import ImageGenerationTimeoutError

    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
        raise ValueError("invalid image prompt")
    if not isinstance(images, list) or len(images) > 10:
        raise ValueError("invalid reference images")
    timeout = config.image_edit_generation_timeout_secs if images else config.image_generation_timeout_secs
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid image generation timeout")
    transport = WebImageConversationTransport(backend)
    transport.model_settings(model)
    backend._webimage_generation_sent = False
    backend._report_progress("uploading")
    references = [transport.upload(image, f"image_{index}.png") for index, image in enumerate(images, 1)]
    backend._report_progress("bootstrapping")
    # 此路径仅需要当前账号 Bearer，不能被旧浏览器实验开关强制要求 sessionToken。
    backend._native_image_sentinel = False
    backend._bootstrap()
    backend._report_progress("getting_token")
    requirements = backend._get_chat_requirements()
    backend._report_progress("preparing_conversation")
    conduit = transport.prepare(prompt, requirements, model)
    backend._report_progress("starting_generation")
    try:
        backend._report_progress("generating")
        yield from transport.generate(transport.payload(prompt, model, references), requirements, conduit, timeout)
    except RequestException as exc:
        # SSE 队列里的墙钟超时是通用 RequestException(code=28)，不总是 Timeout 子类。
        if isinstance(exc, Timeout) or getattr(exc, "code", None) == 28:
            raise ImageGenerationTimeoutError(f"ChatGPT 普通会话生图超时（{timeout} 秒），未重复提交生成请求。") from exc
        raise
