"""Firefly 三步反代客户端：generate-async → 轮询 → 下载。

请求体形态按 BodyKind 分支，与 firefly.adobe.com / Android App 抓包一致。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from services.firefly import models as firefly_models
from services.firefly.constants import (
    DEFAULT_CLIENT_ID,
    DEFAULT_IMPERSONATE,
    DEFAULT_POLL_INTERVAL_SECS,
    DEFAULT_POLL_TIMEOUT_SECS,
    DEFAULT_REQUEST_TIMEOUT_SECS,
    GENERATE_3P_URL,
    GENERATE_NATIVE_URL,
    MAX_REFERENCE_IMAGE_BYTES,
    SUPPORTED_REFERENCE_IMAGE_TYPES,
    UPLOAD_IMAGE_URL,
    WEB_ORIGIN,
    WEB_REFERER,
)
from services.firefly.models import GenerationOptions, ModelSpec


class FireflyError(Exception):
    """Firefly 上游调用失败。"""


class FireflyOverloadedError(FireflyError):
    """3P 端点返回 system under load / timeout_error（Adobe 侧过载或出口 IP 被降级）。"""


class FireflyForbiddenError(FireflyError):
    """403：token scope 不足或账号无权访问该模型。"""


class FireflyUnauthorizedError(FireflyError):
    """401：access_token 失效。"""


class FireflyTimeoutError(FireflyError):
    """轮询超时仍未出图。"""


def _truncate(text: object, limit: int = 300) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "..."


def _random_nonce() -> str:
    return secrets.token_hex(32)


def _header_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value:
        return str(value)
    # 兜底：部分实现的 headers 大小写敏感。
    try:
        for key, item in headers.items():
            if str(key).lower() == name.lower():
                return str(item)
    except Exception:
        pass
    return ""


def _is_overloaded(status_code: int, body: str) -> bool:
    # 抓包：HTTP 408 + {"error_code":"timeout_error","message":"system under load"}
    if status_code == 200:
        return False
    lowered = str(body or "").lower()
    return (
        "system under load" in lowered
        or "timeout_error" in lowered
        or '"error_code":"timeout' in lowered
    )


class FireflyClient:
    """单账号的 Firefly 出图客户端。

    account 用于复用 chatgpt2api 的每账号代理配置；access_token 由调用方保证有效。
    """

    def __init__(
        self,
        access_token: str,
        client_id: str = "",
        account: dict | None = None,
        arp_session_id: str = "",
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.client_id = str(client_id or "").strip() or DEFAULT_CLIENT_ID
        self.account = account
        # x-arp-session-id：Adobe 的风控指纹包（Arkose + FingerprintJS），
        # 只在账号配了值时才发送——发空值反而比不发更可疑。
        self.arp_session_id = str(arp_session_id or "").strip()
        self._session = None

    # ---- 会话与请求头 ----

    @property
    def session(self):
        if self._session is None:
            from curl_cffi import requests

            from services.proxy_service import proxy_settings

            self._session = requests.Session(**proxy_settings.build_session_kwargs(
                account=self.account,
                impersonate=DEFAULT_IMPERSONATE,
                verify=True,
            ))
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def __enter__(self) -> "FireflyClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _headers(self, content_type: str = "") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "x-api-key": self.client_id,
            "Accept": "*/*",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_REFERER,
            "x-nonce": _random_nonce(),
        }
        if self.arp_session_id:
            headers["x-arp-session-id"] = self.arp_session_id
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _raise_for_status(self, response: Any, stage: str) -> str:
        body = str(getattr(response, "text", "") or "")
        status = int(getattr(response, "status_code", 0) or 0)
        if status in (200, 201, 202):
            return body
        if _is_overloaded(status, body):
            raise FireflyOverloadedError(
                f"firefly: {stage} overloaded (system under load): status={status} body={_truncate(body)}"
            )
        if status == 401:
            raise FireflyUnauthorizedError(f"firefly: {stage} unauthorized: body={_truncate(body, 200)}")
        if status == 403:
            raise FireflyForbiddenError(f"firefly: {stage} forbidden: body={_truncate(body, 200)}")
        raise FireflyError(f"firefly: {stage} status={status} body={_truncate(body)}")

    # ---- 第 1 步：提交生成任务 ----

    def build_body(
        self,
        spec: ModelSpec,
        prompt: str,
        options: GenerationOptions,
        n: int = 1,
        reference_blobs: list[dict[str, str]] | None = None,
        store_inputs: bool = True,
        ground_search: bool = False,
        add_watermark: bool = False,
    ) -> dict[str, Any]:
        """组装与抓包一致的 generate-async 请求体。"""
        count = max(1, int(n or 1))
        blobs = list(reference_blobs or [])
        width = options.width or 1024
        height = options.height or 1024
        metadata = {"module": "text2image", "submodule": "ff-image-generate"}

        body_kind = spec.body_kind or firefly_models.resolve_body_kind(spec.model_id, spec.third_party)

        if body_kind == firefly_models.BODY_KIND_NATIVE or not spec.third_party:
            # 自有模型（image-v5）：无 modelId，用 modelVersion + resolutionLevel。
            body: dict[str, Any] = {
                "n": count,
                "output": {"storeInputs": store_inputs},
                "prompt": prompt,
                "referenceBlobs": blobs,
                "modelSpecificPayload": {"locale": "en-US", "prompt_reasoner": "quality"},
                "modelVersion": spec.model_version,
                "resolutionLevel": options.resolution or "1MP",
                "generationMetadata": metadata,
            }
        elif body_kind == firefly_models.BODY_KIND_GPT_IMAGE:
            # gpt-image：自动比例用 modelSpecificPayload.size=auto，指定比例才带顶层 size。
            model_payload: dict[str, Any] = {}
            if not options.aspect_ratio or options.aspect_ratio == "auto":
                model_payload["size"] = "auto"
            body = {
                "n": count,
                "output": {"storeInputs": store_inputs},
                "prompt": prompt,
                "referenceBlobs": blobs,
                "modelSpecificPayload": model_payload,
                "modelId": spec.model_id,
                "modelVersion": spec.model_version,
                "generationMetadata": metadata,
                "generationSettings": {"detailLevel": options.detail_level or 3},
            }
            if options.aspect_ratio and options.aspect_ratio != "auto":
                body["size"] = {"width": width, "height": height}
        elif body_kind == firefly_models.BODY_KIND_FLUX:
            # flux：带 size，modelSpecificPayload 为空对象，无 groundSearch。
            body = {
                "n": count,
                "output": {"storeInputs": store_inputs},
                "prompt": prompt,
                "size": {"width": width, "height": height},
                "referenceBlobs": blobs,
                "modelSpecificPayload": {},
                "modelId": spec.model_id,
                "modelVersion": spec.model_version,
                "generationMetadata": metadata,
            }
        else:
            # Gemini / 通用 3P：size + addWatermark + groundSearch。
            model_payload = {"parameters": {"addWatermark": add_watermark}}
            if options.aspect_ratio and options.aspect_ratio != "auto":
                model_payload["aspectRatio"] = options.aspect_ratio
            body = {
                "n": count,
                "output": {"storeInputs": store_inputs},
                "prompt": prompt,
                "size": {"width": width, "height": height},
                "referenceBlobs": blobs,
                "modelSpecificPayload": model_payload,
                "modelId": spec.model_id,
                "modelVersion": spec.model_version,
                "generationMetadata": metadata,
                "groundSearch": ground_search,
            }

        if options.seeds:
            body["seeds"] = list(options.seeds)
        return body

    def generate_async(
        self,
        spec: ModelSpec,
        prompt: str,
        options: GenerationOptions,
        n: int = 1,
        reference_blobs: list[dict[str, str]] | None = None,
        store_inputs: bool = True,
        ground_search: bool = False,
        add_watermark: bool = False,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        """提交生成任务，返回 {status_url, task_status, retry_after}。"""
        body = self.build_body(
            spec, prompt, options, n,
            reference_blobs=reference_blobs,
            store_inputs=store_inputs,
            ground_search=ground_search,
            add_watermark=add_watermark,
        )
        endpoint = GENERATE_3P_URL if spec.third_party else GENERATE_NATIVE_URL
        # 3P 常见 408 system under load：有限次退避重试（Adobe 侧过载，非鉴权问题）。
        max_attempts = attempts if attempts is not None else (4 if spec.third_party else 1)

        last_error: Exception | None = None
        for attempt in range(max(1, max_attempts)):
            if attempt > 0:
                time.sleep(2.0 * (2 ** (attempt - 1)))  # 2s, 4s, 8s
            try:
                return self._generate_async_once(endpoint, body)
            except FireflyOverloadedError as exc:
                last_error = exc
                continue
        raise last_error or FireflyError("firefly: generate-async failed")

    def _generate_async_once(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            endpoint,
            headers=self._headers("application/json"),
            data=json.dumps(body),
            timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
        )
        text = self._raise_for_status(response, "generate-async")

        try:
            parsed = json.loads(text) if text else {}
        except Exception:
            parsed = {}
        links = parsed.get("links") if isinstance(parsed, dict) else {}
        links = links if isinstance(links, dict) else {}

        # 轮询地址优先用响应头 override（经 bks 代理，含 host 参数），否则回退 body。
        status_url = _header_value(response, "x-override-status-link")
        if not status_url:
            result_link = links.get("result") if isinstance(links.get("result"), dict) else {}
            status_url = str(result_link.get("href") or "")
        if not status_url:
            raise FireflyError("firefly: no status url in generate response")

        cancel_link = links.get("cancel") if isinstance(links.get("cancel"), dict) else {}
        retry_after = 0
        try:
            retry_after = int(_header_value(response, "retry-after") or 0)
        except ValueError:
            retry_after = 0

        return {
            "status_url": status_url,
            "cancel_url": str(cancel_link.get("href") or ""),
            "task_status": _header_value(response, "x-task-status"),
            "retry_after": retry_after,
        }

    # ---- 第 2 步：轮询 ----

    def poll_once(self, status_url: str) -> tuple[dict[str, Any], int]:
        response = self.session.get(
            status_url,
            headers=self._headers(),
            timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
        )
        text = self._raise_for_status(response, "poll")
        try:
            result = json.loads(text) if text else {}
        except Exception as exc:
            raise FireflyError(f"firefly: unmarshal poll result: {exc}") from exc
        if not isinstance(result, dict):
            raise FireflyError("firefly: unexpected poll result")
        try:
            retry_after = int(_header_value(response, "retry-after") or 0)
        except ValueError:
            retry_after = 0
        return result, retry_after

    @staticmethod
    def _result_done(result: dict[str, Any]) -> bool:
        """progress 可能停在 80，但 outputs 已带 presignedUrl 即视为可用。"""
        if str(result.get("status") or "").upper() == "IN_PROGRESS":
            return False
        for output in result.get("outputs") or []:
            if isinstance(output, dict) and str((output.get("image") or {}).get("presignedUrl") or ""):
                return True
        return False

    def wait_for_result(
        self,
        job: dict[str, Any],
        timeout_secs: float = 0.0,
        interval_secs: float = 0.0,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        status_url = str((job or {}).get("status_url") or "")
        if not status_url:
            raise FireflyError("firefly: invalid job for polling")

        deadline = time.time() + (timeout_secs if timeout_secs > 0 else DEFAULT_POLL_TIMEOUT_SECS)
        interval = interval_secs if interval_secs > 0 else DEFAULT_POLL_INTERVAL_SECS
        if int(job.get("retry_after") or 0) > 0:
            interval = float(job["retry_after"])

        while True:
            result, retry_after = self.poll_once(status_url)
            if self._result_done(result):
                return result
            if progress_callback:
                try:
                    progress_callback("generating")
                except Exception:
                    pass
            if time.time() >= deadline:
                raise FireflyTimeoutError(
                    f"firefly: poll timeout after {timeout_secs or DEFAULT_POLL_TIMEOUT_SECS:.0f}s"
                )
            time.sleep(float(retry_after) if retry_after > 0 else interval)

    # ---- 第 3 步：下载 ----

    def download(self, presigned_url: str, timeout_secs: int = 120) -> bytes:
        """presignedUrl 是 S3 预签名地址，无需 Adobe 鉴权。"""
        if not str(presigned_url or "").strip():
            raise FireflyError("firefly: empty presigned url")
        response = self.session.get(presigned_url, timeout=timeout_secs)
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise FireflyError(f"firefly: download status={status}")
        return bytes(response.content or b"")

    # ---- 参考图上传（图生图） ----

    def upload_image(self, image_bytes: bytes, content_type: str) -> str:
        """上传参考图片裸二进制，返回生成请求使用的图片 ID。"""
        if not image_bytes:
            raise FireflyError("firefly: empty upload image")
        if len(image_bytes) > MAX_REFERENCE_IMAGE_BYTES:
            raise FireflyError("firefly: reference image exceeds 10 MiB")
        normalized_type = str(content_type or "").split(";")[0].strip().lower()
        if normalized_type not in SUPPORTED_REFERENCE_IMAGE_TYPES:
            raise FireflyError(f"firefly: unsupported upload image content type {content_type!r}")

        response = self.session.post(
            UPLOAD_IMAGE_URL,
            headers=self._headers(normalized_type),
            data=image_bytes,
            timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
        )
        text = self._raise_for_status(response, "upload-image")
        try:
            parsed = json.loads(text) if text else {}
        except Exception as exc:
            raise FireflyError(f"firefly: decode upload response: {exc}") from exc
        for image in (parsed.get("images") or []) if isinstance(parsed, dict) else []:
            image_id = str((image or {}).get("id") or "").strip()
            if image_id:
                return image_id
        raise FireflyError("firefly: no image id in upload response")
