from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.firefly import SOURCE_TYPE as FIREFLY_SOURCE_TYPE
from services.firefly import models as firefly_models
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL, WEB_IMAGE_MODELS


def list_models() -> dict[str, Any]:
    backend = OpenAIBackendAPI()
    try:
        result = backend.list_models()
    except Exception:
        # OpenAI 后端不可达（如出口到 chatgpt.com 超时）时，仍要能列出本地渠道
        # （Adobe Firefly / 动态图像）模型，不能让整个 /v1/models 挂掉。
        result = {"object": "list", "data": []}
    finally:
        backend.close()
    data = result.get("data")
    if not isinstance(data, list):
        data = []
        result = {"object": "list", "data": data}
    seen = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    web_image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
    ]
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if web_image_accounts:
        dynamic_models.update(WEB_IMAGE_MODELS)
    # 号池里有 Adobe 账号时暴露 Firefly 反代模型（firefly- 前缀）。
    if any(
        isinstance(account, dict)
        and account_service._normalize_source_type(account.get("source_type")) == FIREFLY_SOURCE_TYPE
        for account in accounts
    ):
        dynamic_models.update(firefly_models.list_model_names())
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append({
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt2api",
                "permission": [],
                "root": model,
                "parent": None,
            })
    return result
