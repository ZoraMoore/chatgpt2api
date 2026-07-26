"""Firefly 出图 provider：号池选号 → 三步反代 → 生图计数。

与 OpenAI provider 一样，每次调用只产出 1 张图；上层 conversation.stream_image_outputs_with_pool
负责按 n 并行调度，因此并发、轮转、部分失败等行为与既有生图链路完全一致。
"""

from __future__ import annotations

import base64
from typing import Any

from services.account_service import account_service
from services.config import config
from services.firefly import SOURCE_TYPE
from services.firefly import models as firefly_models
from services.firefly.client import (
    FireflyClient,
    FireflyError,
    FireflyForbiddenError,
    FireflyOverloadedError,
    FireflyTimeoutError,
    FireflyUnauthorizedError,
)
from services.firefly.models import GenerationOptionError
from utils.log import logger

# 单张图最多尝试的账号数（403/401/过载都会换号）。
MAX_ACCOUNT_ATTEMPTS = 3


def _detect_content_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _account_client_id(account: dict[str, Any]) -> str:
    return str(account.get("client_id") or "").strip()


def _upload_reference_images(client: FireflyClient, spec, images: list[str]) -> list[dict[str, str]]:
    """把 base64 参考图上传到 Adobe，转成 referenceBlobs。"""
    blobs: list[dict[str, str]] = []
    usage = firefly_models.reference_image_usage(spec)
    for encoded in images:
        try:
            raw = base64.b64decode(str(encoded or ""), validate=False)
        except Exception as exc:
            raise FireflyError(f"firefly: invalid reference image: {exc}") from exc
        content_type = _detect_content_type(raw)
        if not content_type:
            raise FireflyError("firefly: unsupported reference image type (png/jpeg/webp only)")
        blobs.append({"id": client.upload_image(raw, content_type), "usage": usage})
    return blobs


def _collect_images(client: FireflyClient, result: dict[str, Any], prompt: str) -> list[dict[str, str]]:
    """下载 presignedUrl 并转成 OpenAI 风格的 b64 条目。"""
    items: list[dict[str, str]] = []
    for output in result.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        presigned_url = str((output.get("image") or {}).get("presignedUrl") or "")
        if not presigned_url:
            continue
        raw = client.download(presigned_url)
        if not raw:
            continue
        items.append({
            "b64_json": base64.b64encode(raw).decode("ascii"),
            "revised_prompt": prompt,
        })
    return items


def generate_single_image(request: Any, index: int, total: int) -> list[Any]:
    """为单张图片选号并出图，返回 conversation.ImageOutput 列表。"""
    # 延迟导入：conversation 会反向引用本模块。
    from services.protocol.conversation import (
        ImageGenerationError,
        ImageOutput,
        format_image_result,
    )

    spec = firefly_models.resolve_model(request.model)
    if spec is None:
        raise ImageGenerationError(
            f"unsupported firefly model: {request.model}，可用模型: "
            + ", ".join(firefly_models.list_model_names()),
            status_code=400,
            error_type="invalid_request_error",
            code="model_not_found",
        )

    try:
        options = firefly_models.resolve_generation_options(
            spec,
            size=request.size,
            quality=request.quality,
        )
    except GenerationOptionError as exc:
        raise ImageGenerationError(
            f"{exc}; supported: {', '.join(exc.supported)}",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_generation_option",
        ) from exc

    prompt = str(request.prompt or "")
    images = list(request.images or [])
    account_email = ""
    attempted_tokens: set[str] = set()
    last_error: Exception | None = None

    for attempt in range(MAX_ACCOUNT_ATTEMPTS):
        if request.progress_callback:
            request.progress_callback("getting_account")
        try:
            token = account_service.get_available_access_token(source_type=SOURCE_TYPE)
        except RuntimeError as exc:
            if last_error is not None:
                raise ImageGenerationError(str(last_error), account_email=account_email) from last_error
            raise ImageGenerationError(
                str(exc) or "no available adobe image quota",
                status_code=429,
                code="insufficient_quota",
                account_email=account_email,
            ) from exc

        attempted_tokens.add(token)
        account = account_service.get_account(token) or {}
        account_email = str(account.get("email") or "").strip()
        client = FireflyClient(
            token,
            client_id=_account_client_id(account),
            account=account,
            arp_session_id=str(account.get("arp_session_id") or ""),
        )
        current_spec = spec

        try:
            reference_blobs = _upload_reference_images(client, current_spec, images) if images else []
            if request.progress_callback:
                request.progress_callback("generating")

            try:
                job = client.generate_async(
                    current_spec, prompt, options, n=1, reference_blobs=reference_blobs,
                )
            except FireflyOverloadedError:
                # 3P 过载：回退到 Firefly 自有 Image 5（与 Go 侧 fallback_native 行为一致）。
                if not current_spec.third_party:
                    raise
                fallback = firefly_models.resolve_model("firefly-image5")
                if fallback is None:
                    raise
                logger.warning({
                    "event": "firefly_third_party_overloaded_fallback",
                    "model": current_spec.public_name,
                    "fallback": fallback.public_name,
                    "account_email": account_email,
                    "index": index,
                })
                current_spec = fallback
                job = client.generate_async(current_spec, prompt, options, n=1, reference_blobs=reference_blobs)

            result = client.wait_for_result(
                job,
                timeout_secs=float(config.image_poll_timeout_secs or 0),
                progress_callback=request.progress_callback,
            )
            items = _collect_images(client, result, prompt)
            if not items:
                raise FireflyError("firefly returned no images")

            formatted = format_image_result(
                items,
                prompt,
                str(request.response_format or "b64_json"),
                request.base_url,
            )
            account_service.mark_image_result(token, True)
            return [ImageOutput(
                kind="result",
                model=str(request.model),
                index=index,
                total=total,
                created=formatted.get("created"),
                data=formatted.get("data") or [],
                account_email=account_email,
            )]

        except FireflyUnauthorizedError as exc:
            # token 失效：强制续期后换号重试（续期成功的话下一轮会重新选到同一账号）。
            account_service.mark_image_result(token, False)
            last_error = exc
            logger.warning({
                "event": "firefly_unauthorized",
                "account_email": account_email,
                "index": index,
                "error": str(exc)[:200],
            })
            account_service.refresh_access_token(token, force=True, event="firefly_image")
            continue

        except FireflyForbiddenError as exc:
            # scope 不足或无权访问该模型：标记异常，换号。
            account_service.mark_image_result(token, False)
            last_error = exc
            logger.warning({
                "event": "firefly_forbidden",
                "account_email": account_email,
                "model": current_spec.public_name,
                "index": index,
                "error": str(exc)[:200],
            })
            account_service.update_account(token, {"status": "异常"}, quiet=True)
            continue

        except FireflyOverloadedError as exc:
            account_service.mark_image_result(token, False)
            last_error = exc
            logger.warning({
                "event": "firefly_overloaded",
                "account_email": account_email,
                "index": index,
                "error": str(exc)[:200],
            })
            continue

        except FireflyTimeoutError as exc:
            account_service.mark_image_result(token, False)
            raise ImageGenerationError(
                str(exc),
                status_code=504,
                error_type="server_error",
                code="image_generation_timeout",
                account_email=account_email,
            ) from exc

        except FireflyError as exc:
            account_service.mark_image_result(token, False)
            logger.warning({
                "event": "firefly_generate_failed",
                "account_email": account_email,
                "index": index,
                "error": str(exc)[:300],
            })
            raise ImageGenerationError(str(exc), account_email=account_email) from exc

        except Exception as exc:
            account_service.mark_image_result(token, False)
            logger.warning({
                "event": "firefly_generate_error",
                "account_email": account_email,
                "index": index,
                "error": str(exc)[:300],
            })
            raise ImageGenerationError(str(exc), account_email=account_email) from exc

        finally:
            client.close()

    raise ImageGenerationError(
        str(last_error) if last_error else "firefly image generation failed after retries",
        account_email=account_email,
    )
