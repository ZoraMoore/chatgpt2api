#!/usr/bin/env python3
"""
Adobe Firefly provider 自测脚本（不联网，不写真实数据）。

覆盖：模型解析与路由、OpenAI 参数映射、generate-async 请求体形态、
IMS token 解析，以及「选号 → 出图 → 生图计数」的号池闭环。

用法：
  python scripts/test_firefly.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# config 在导入时强制要求 auth-key；自测不涉及鉴权，给个占位值即可。
os.environ.setdefault("CHATGPT2API_AUTH_KEY", "firefly-selftest-placeholder-key")

# 本脚本只跑内存里的逻辑，不需要 git / postgres 存储后端；缺这两个可选依赖时用空壳顶替，
# 免得为了跑自测去装一堆用不上的包。
if "git" not in sys.modules:
    try:
        import git  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("git")
        stub.Repo = object
        stub.__path__ = []  # 让 git.exc 这类子模块导入也能成立
        exc_stub = types.ModuleType("git.exc")
        for name in ("GitCommandError", "InvalidGitRepositoryError", "NoSuchPathError"):
            error_type = type(name, (Exception,), {})
            setattr(exc_stub, name, error_type)
            setattr(stub, name, error_type)
        stub.exc = exc_stub
        sys.modules["git"] = stub
        sys.modules["git.exc"] = exc_stub

# pybase64 只是 base64 的加速实现，缺失时用标准库顶替（API 一致）。
try:
    import pybase64  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pybase64"] = base64

from services.firefly import auth as firefly_auth
from services.firefly import models as firefly_models
from services.firefly.client import FireflyClient
from utils.helper import is_codex_image_model, is_firefly_image_model, is_supported_image_model

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [ok] {name}")
        return
    FAILURES.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  [FAIL] {name}{': ' + detail if detail else ''}")


def test_model_routing() -> None:
    print("\n[1] 模型解析与 provider 路由")
    check("gpt-image- 前缀被识别", is_firefly_image_model("gpt-image-adobe-2"))
    check("firefly 模型属于受支持生图模型", is_supported_image_model("gpt-image-gemini-flash"))
    check("firefly 模型不会被当成 codex", not is_codex_image_model("gpt-image-adobe-2"))
    # 与自有模型共用 gpt-image- 前缀，这两条是防止 Adobe 号池顶掉自有渠道的底线。
    check("自有 gpt-image-2 不路由到 firefly", not is_firefly_image_model("gpt-image-2"))
    check("自有 gpt-image-2 仍受支持", is_supported_image_model("gpt-image-2"))
    check("自有 codex-gpt-image-2 不路由到 firefly", not is_firefly_image_model("codex-gpt-image-2"))
    check("旧的 firefly- 名字已不再路由", not is_firefly_image_model("firefly-gpt-image-2"))

    spec = firefly_models.resolve_model("gpt-image-adobe-2")
    check("gpt-image-adobe-2 解析成功", spec is not None)
    check("解析到 gpt-image/2", bool(spec) and (spec.model_id, spec.model_version) == ("gpt-image", "2"))
    check("gpt-image-adobe-1.5 解析到 gpt-image/1.5",
          (firefly_models.resolve_model("gpt-image-adobe-1.5") or spec).model_version == "1.5")
    check("别名 gpt-image-nano-banana 指向 gemini-flash",
          (firefly_models.resolve_model("gpt-image-nano-banana") or spec).model_version == "nano-banana-3")
    check("未知模型返回 None", firefly_models.resolve_model("gpt-image-not-exist") is None)

    names = firefly_models.list_model_names()
    check("模型名全部带 gpt-image- 前缀", all(name.startswith("gpt-image-") for name in names), str(names))
    check("对外名不与自有 gpt-image-2 撞名", "gpt-image-2" not in names, str(names))
    check("模型列表包含 6 个内置模型", len(names) == 6, str(names))
    check("对外名含 gpt-image-Nano Banana 3", "gpt-image-Nano Banana 3" in names, str(names))
    check("对外名含 gpt-image-Nano Banana Pro", "gpt-image-Nano Banana Pro" in names, str(names))
    check("对外名可解析到 nano-banana-3",
          (firefly_models.resolve_model("gpt-image-Nano Banana 3") or spec).model_version == "nano-banana-3")
    check("对外名大小写不敏感",
          (firefly_models.resolve_model("gpt-image-nano banana 3") or spec).model_version == "nano-banana-3")
    check("Pro 解析到 nano-banana-2",
          (firefly_models.resolve_model("gpt-image-Nano Banana Pro") or spec).model_version == "nano-banana-2")
    # 对外名带空格，无空格写法必须等价，方便不便传空格的客户端。
    check("无空格写法等价",
          (firefly_models.resolve_model("gpt-image-nano-banana-3") or spec).model_version == "nano-banana-3")
    check("无空格 Pro 写法等价",
          (firefly_models.resolve_model("gpt-image-nano-banana-pro") or spec).model_version == "nano-banana-2")
    check("别名 gpt-image-gemini-flash 可用",
          (firefly_models.resolve_model("gpt-image-gemini-flash") or spec).model_version == "nano-banana-3")
    check("别名 gpt-image-nano-banana-2 可用",
          (firefly_models.resolve_model("gpt-image-nano-banana-2") or spec).model_version == "nano-banana-2")


def test_generation_options() -> None:
    print("\n[2] OpenAI size/quality → Firefly 参数映射")
    gpt_image = firefly_models.resolve_model("gpt-image-adobe-2")
    gemini = firefly_models.resolve_model("gpt-image-gemini-flash")

    options = firefly_models.resolve_generation_options(gpt_image, size="1024x1024", quality="high")
    check("1024x1024 → 1:1", options.aspect_ratio == "1:1", options.aspect_ratio)
    check("quality=high → 4K", options.resolution == "4K", options.resolution)
    check("4K → detailLevel 3", options.detail_level == 3, str(options.detail_level))
    check("1:1 → 1024x1024", (options.width, options.height) == (1024, 1024), f"{options.width}x{options.height}")

    options = firefly_models.resolve_generation_options(gpt_image, size="1792x1024", quality="low")
    check("1792x1024 → 16:9", options.aspect_ratio == "16:9", options.aspect_ratio)
    check("quality=low → 1K/detail 1", (options.resolution, options.detail_level) == ("1K", 1))

    options = firefly_models.resolve_generation_options(gpt_image, size="auto", quality="auto")
    check("size=auto → aspect auto", options.aspect_ratio == "auto", options.aspect_ratio)
    check("quality=auto → 模型默认 1K", options.resolution == "1K", options.resolution)

    options = firefly_models.resolve_generation_options(gemini, size="2048x2048", quality="auto")
    check("gemini 2048 → 2K", options.resolution == "2K", options.resolution)
    check("gemini 2K → 2048x2048", (options.width, options.height) == (2048, 2048))

    try:
        firefly_models.resolve_generation_options(gpt_image, aspect_ratio="7:3")
        check("非法比例应报错", False)
    except firefly_models.GenerationOptionError as exc:
        check("非法比例报错并给出候选", bool(exc.supported))


def test_request_bodies() -> None:
    print("\n[3] generate-async 请求体形态（对齐抓包）")
    client = FireflyClient("token", account=None)

    native = firefly_models.resolve_model("gpt-image-firefly-image5")
    body = client.build_body(native, "小猫", firefly_models.resolve_generation_options(native))
    check("自有模型无 modelId", "modelId" not in body)
    check("自有模型带 modelVersion=image5", body.get("modelVersion") == "image5")
    check("自有模型带 resolutionLevel", body.get("resolutionLevel") == "1MP", str(body.get("resolutionLevel")))
    check("自有模型带 prompt_reasoner", body["modelSpecificPayload"].get("prompt_reasoner") == "quality")

    gpt_image = firefly_models.resolve_model("gpt-image-adobe-2")
    options = firefly_models.resolve_generation_options(gpt_image, size="auto")
    body = client.build_body(gpt_image, "小猫", options)
    check("gpt-image auto 用 modelSpecificPayload.size=auto", body["modelSpecificPayload"].get("size") == "auto")
    check("gpt-image auto 不带顶层 size", "size" not in body)
    check("gpt-image 带 detailLevel", body["generationSettings"].get("detailLevel") in (1, 2, 3))

    options = firefly_models.resolve_generation_options(gpt_image, size="1344x768", quality="medium")
    body = client.build_body(gpt_image, "小猫", options)
    check("gpt-image 指定比例带顶层 size", body.get("size") == {"width": 1344, "height": 768}, json.dumps(body.get("size")))
    check("gpt-image 指定比例时 payload 无 size", "size" not in body["modelSpecificPayload"])

    flux = firefly_models.resolve_model("gpt-image-flux-pro-2")
    body = client.build_body(flux, "小猫", firefly_models.resolve_generation_options(flux, size="1024x1024"))
    check("flux modelSpecificPayload 为空对象", body["modelSpecificPayload"] == {})
    check("flux 无 groundSearch", "groundSearch" not in body)
    check("flux 带 size", body.get("size") == {"width": 1024, "height": 1024})

    gemini = firefly_models.resolve_model("gpt-image-gemini-flash")
    options = firefly_models.resolve_generation_options(gemini, aspect_ratio="16:9", resolution="1K")
    body = client.build_body(gemini, "小狗", options, add_watermark=True, ground_search=True)
    check("gemini 带 addWatermark", body["modelSpecificPayload"]["parameters"]["addWatermark"] is True)
    check("gemini 带 aspectRatio", body["modelSpecificPayload"].get("aspectRatio") == "16:9")
    check("gemini 带 groundSearch", body.get("groundSearch") is True)

    body = client.build_body(gemini, "小狗", options, reference_blobs=[{"id": "abc", "usage": "general"}])
    check("参考图写入 referenceBlobs", body["referenceBlobs"] == [{"id": "abc", "usage": "general"}])
    check("nano banana 参考图 usage=general", firefly_models.reference_image_usage(gemini) == "general")
    check("gpt-image 参考图 usage=subject", firefly_models.reference_image_usage(gpt_image) == "subject")
    client.close()


def _fake_access_token(scope: str, created_ms: int, expires_ms: int) -> str:
    payload = {
        "id": "x", "type": "access_token", "client_id": "clio-playground-web",
        "user_id": "U123", "scope": scope,
        "created_at": str(created_ms), "expires_in": str(expires_ms),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_token_parsing() -> None:
    print("\n[4] IMS access_token 解析")
    now_ms = int(time.time() * 1000)
    valid = _fake_access_token("AdobeID,openid,firefly_api,creative_production", now_ms, 86400000)
    expired = _fake_access_token("AdobeID,openid,firefly_api", now_ms - 90000000, 86400000)
    weak = _fake_access_token("AdobeID,openid,creative_sdk", now_ms, 86400000)

    check("有效 token 未过期", not firefly_auth.token_expired(valid))
    check("过期 token 被识别", firefly_auth.token_expired(expired))
    check("leeway 生效", firefly_auth.token_expired(valid, leeway_secs=86400 + 60))
    check("firefly_api scope 通过", firefly_auth.has_generate_scope(valid)[0])
    check("creative_sdk scope 被拒", not firefly_auth.has_generate_scope(weak)[0])
    check("user_id 可解析", firefly_auth.parse_access_token_claims(valid).get("user_id") == "U123")


def test_bridge_and_required() -> None:
    """device_token 桥接成带 firefly_api 的 Web token；x-arp-session-id 必填。"""
    print("\n[5] device_token 桥接 + x-arp-session-id 必填")
    from services.firefly import auth as fa
    from services.firefly import pool as firefly_pool
    from services.firefly.constants import (
        IMS_JUMPTOKEN_URL, IMS_PROFILE_URL, IMS_TOKEN_CHECK_URL, IMS_TOKEN_V4_URL,
    )

    now_ms = int(time.time() * 1000)
    android_tok = _fake_access_token("creative_sdk,AdobeID,openid", now_ms, 86400000)
    web_tok = _fake_access_token("AdobeID,firefly_api,creative_production,openid", now_ms, 86400000)

    class FakeCookies:
        def __init__(self): self._d = {}
        def items(self): return list(self._d.items())
        def get_dict(self): return dict(self._d)

    class FakeResp:
        def __init__(self, status=200, payload=None):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload) if payload is not None else ""
            self.content = b""
        def json(self): return self._payload

    class FakeSession:
        def __init__(self):
            self.cookies = FakeCookies()
            self.calls = []
            self.check_cookie_header = None
        def post(self, url, **kw):
            self.calls.append(url)
            if url == IMS_TOKEN_V4_URL:
                return FakeResp(payload={"access_token": android_tok, "device_token": "dev-new", "userId": "U1", "email": "a@b.com"})
            if url == IMS_JUMPTOKEN_URL:
                assert str(kw.get("headers", {}).get("Authorization", "")).startswith("Bearer ")
                assert kw.get("allow_redirects") is False
                return FakeResp(payload={"jump": "https://adobeid.example/jump?token=x"})
            if url == IMS_TOKEN_CHECK_URL:
                self.check_cookie_header = kw.get("headers", {}).get("Cookie")
                return FakeResp(payload={"access_token": web_tok})
            return FakeResp(404, {})
        def get(self, url, **kw):
            self.calls.append(url)
            if url == IMS_PROFILE_URL:
                return FakeResp(payload={"email": "a@b.com", "userId": "U1", "account_type": "type1"})
            self.cookies._d["ims_sid"] = "SID"
            self.cookies._d["aux_sid"] = "AUX"
            return FakeResp(302)
        def close(self): pass

    created: list = []
    fa._session = lambda account=None: created.append(FakeSession()) or created[-1]

    result = fa.bridge_device_to_web_access("dev-token")
    check("桥接换出带 firefly_api 的 Web token", fa.has_generate_scope(result["access_token"])[0], result.get("scope"))
    check("桥接产出 SSO Cookie", "ims_sid" in (result.get("session_cookies") or ""))
    steps = created[-1].calls
    check("经过 device→jumptoken→jump→check 四步",
          steps[0] == IMS_TOKEN_V4_URL and steps[1] == IMS_JUMPTOKEN_URL and steps[-1] == IMS_TOKEN_CHECK_URL, str(steps))
    check("check 步骤带上了 SSO Cookie 头", bool(created[-1].check_cookie_header and "ims_sid" in created[-1].check_cookie_header))

    payload = firefly_pool.build_account_payload({"device_token": "dev-token", "arp_session_id": "arp-x", "quota": 50})
    check("导入后状态正常（不再异常）", payload.get("status") == "正常", str(payload.get("status")))
    check("导入 payload 带 firefly_api scope", "firefly_api" in (payload.get("scope") or ""))
    check("导入保存了桥接产出的 Cookie", bool(payload.get("session_cookies")))
    check("导入保存 arp", payload.get("arp_session_id") == "arp-x")
    check("落库的是 Web token 非 Android token", fa.has_generate_scope(payload["access_token"])[0])

    try:
        firefly_pool.build_account_payload({"device_token": "dev-token", "quota": 10})
        check("缺 x-arp-session-id 报错", False)
    except ValueError as exc:
        check("缺 x-arp-session-id 报错", "arp" in str(exc).lower(), str(exc))


def test_pool_roundtrip() -> None:
    """闭环：选号 → 出图 → 计数，全部在内存里跑，不写文件、不联网。"""
    print("\n[6] 号池闭环（选号 / 出图 / 生图计数）")
    from services.account_service import account_service
    from services.firefly import provider as firefly_provider
    from services.protocol import conversation
    from services.protocol.conversation import ConversationRequest

    class MemoryStorage:
        def load_accounts(self): return []
        def save_accounts(self, accounts): return None
        def load_auth_keys(self): return []
        def save_auth_keys(self, keys): return None
        def health_check(self): return {"ok": True}
        def get_backend_info(self): return {"type": "memory"}

    account_service.storage = MemoryStorage()
    account_service._accounts = {}
    account_service._image_inflight = {}

    token = _fake_access_token("AdobeID,firefly_api,creative_production", int(time.time() * 1000), 86400000)
    account_service._accounts[token] = account_service._normalize_account({
        "access_token": token,
        "source_type": "adobe",
        "type": "firefly",
        "email": "tester@example.com",
        "quota": 5,
        "status": "正常",
        "device_token": "device-token-x",
        "arp_session_id": "eyJzaWQiOiJmYWtlLXNlc3Npb24taWQifQ==",
    })
    # 远程校验在离线自测里直接放行。
    account_service.fetch_remote_info = lambda access_token, event="", defer_invalid_removal=True: \
        account_service.get_account(access_token)

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    class FakeClient:
        last_body: dict | None = None
        last_arp: str | None = None

        def __init__(self, access_token, client_id="", account=None, arp_session_id=""):
            self.access_token = access_token
            self.account = account
            FakeClient.last_arp = arp_session_id

        def generate_async(self, spec, prompt, options, n=1, reference_blobs=None, **kwargs):
            FakeClient.last_body = {"model": spec.public_name, "prompt": prompt, "n": n}
            return {"status_url": "https://example.invalid/status", "retry_after": 0}

        def wait_for_result(self, job, timeout_secs=0.0, progress_callback=None):
            return {"outputs": [{"seed": 1, "image": {"id": "img1", "presignedUrl": "https://example.invalid/img.png"}}]}

        def download(self, url, timeout_secs=120):
            return png

        def upload_image(self, data, content_type):
            return "uploaded-id"

        def close(self):
            return None

    firefly_provider.FireflyClient = FakeClient
    conversation.save_image_bytes = lambda data, base_url=None: "https://example.invalid/saved.png"

    request = ConversationRequest(
        model="gpt-image-gemini-flash",
        prompt="一只在草地上奔跑的小狗",
        n=1,
        size="1024x1024",
        response_format="b64_json",
    )
    outputs = firefly_provider.generate_single_image(request, 1, 1)

    check("产出 1 个 result 输出", len(outputs) == 1 and outputs[0].kind == "result")
    check("data 带 b64_json", bool(outputs[0].data) and "b64_json" in outputs[0].data[0])
    check("回填账号邮箱", outputs[0].account_email == "tester@example.com")
    check("上游收到的模型正确（别名被规范化到对外名）",
          (FakeClient.last_body or {}).get("model") == "gpt-image-Nano Banana 3", str((FakeClient.last_body or {}).get("model")))
    check("账号的 x-arp-session-id 传给了客户端",
          FakeClient.last_arp == "eyJzaWQiOiJmYWtlLXNlc3Npb24taWQifQ==", str(FakeClient.last_arp))

    account = account_service.get_account(token) or {}
    check("成功计数 +1", int(account.get("success") or 0) == 1, str(account.get("success")))
    check("额度扣减 5 → 4", int(account.get("quota") or 0) == 4, str(account.get("quota")))
    check("并发槽已释放", int(account.get("image_inflight") or 0) == 0)
    check("最近使用时间已刷新", bool(account.get("last_used_at")))

    # 失败路径：上游报错应计入 fail 且不扣额度。
    class FailingClient(FakeClient):
        def generate_async(self, spec, prompt, options, n=1, reference_blobs=None, **kwargs):
            from services.firefly.client import FireflyError
            raise FireflyError("firefly: generate-async status=500")

    firefly_provider.FireflyClient = FailingClient
    try:
        firefly_provider.generate_single_image(request, 1, 1)
        check("失败时抛出 ImageGenerationError", False)
    except conversation.ImageGenerationError:
        check("失败时抛出 ImageGenerationError", True)

    account = account_service.get_account(token) or {}
    check("失败计数 +1", int(account.get("fail") or 0) == 1, str(account.get("fail")))
    check("失败不扣额度", int(account.get("quota") or 0) == 4, str(account.get("quota")))

    # Adobe 账号不应被 OpenAI 的刷新链路处理。
    check("adobe 账号被正确识别", account_service._is_firefly_account(account))

    stats = __import__("services.firefly.pool", fromlist=["pool"]).stats()
    check("分池统计 total=1", stats["total"] == 1, json.dumps(stats, ensure_ascii=False))
    check("分池统计生图数=1", stats["total_success"] == 1)
    listed = __import__("services.firefly.pool", fromlist=["pool"]).list_accounts()
    check("列表脱敏 device_token", listed and "device_token" not in listed[0] and listed[0]["has_device_token"])


def test_arp_session_header() -> None:
    """x-arp-session-id：配了才发，没配不发（与 Go 侧 headers.go 行为一致）。"""
    print("\n[7] x-arp-session-id 请求头")
    with_arp = FireflyClient("token", arp_session_id="eyJzaWQiOiJhYmMifQ==")
    headers = with_arp._headers("application/json")
    check("配置后注入请求头", headers.get("x-arp-session-id") == "eyJzaWQiOiJhYmMifQ==")
    check("不影响其它固定头", headers.get("x-api-key") == "clio-playground-web" and headers.get("Origin"))
    with_arp.close()

    without_arp = FireflyClient("token")
    check("未配置时不发该头", "x-arp-session-id" not in without_arp._headers())
    without_arp.close()

    blank_arp = FireflyClient("token", arp_session_id="   ")
    check("空白值视为未配置", "x-arp-session-id" not in blank_arp._headers())
    blank_arp.close()


def test_credential_update() -> None:
    """号池里替换 x-arp-session-id / 改额度。"""
    print("\n[8] Adobe 账号编辑")
    from services.account_service import account_service
    from services.firefly import pool as firefly_pool

    token = next(
        item["access_token"] for item in account_service.list_accounts()
        if account_service._normalize_source_type(item.get("source_type")) == "adobe"
    )
    updated = firefly_pool.update_credentials(token, {"arp_session_id": "eyJzaWQiOiJuZXcifQ==", "quota": 9})
    check("额度已更新", updated.get("quota") == 9, str(updated.get("quota")))
    check("列表不回传 arp 明文", "arp_session_id" not in updated)
    check("回传预览片段", updated.get("arp_session_id_preview") == "eyJzaWQiOiJuZXci", str(updated.get("arp_session_id_preview")))
    check("底层已写入新值",
          (account_service.get_account(token) or {}).get("arp_session_id") == "eyJzaWQiOiJuZXcifQ==")

    try:
        firefly_pool.update_credentials(token, {"arp_session_id": ""})
        check("arp 不能清空", False)
    except ValueError:
        check("arp 不能清空", True)

    try:
        firefly_pool.update_credentials(token, {})
        check("空更新应报错", False)
    except ValueError:
        check("空更新应报错", True)

    try:
        firefly_pool.update_credentials("not-in-pool", {"quota": 1})
        check("未知账号应报 LookupError", False)
    except LookupError:
        check("未知账号应报 LookupError", True)


def test_import_export() -> None:
    """导入/导出对称性：adobe 凭证不能被误当成 OpenAI 账号。"""
    print("\n[9] 号池导入 / 导出")
    from services.account_service import account_service

    payload = account_service._prepare_account_payload({
        "type": "adobe", "access_token": "tok-a", "device_token": "dev-a", "email": "a@example.com",
    })
    check("导出件 type=adobe 归入 adobe 分池", payload["source_type"] == "adobe", str(payload))
    check("adobe 不再占用套餐类型字段", payload["type"] == "firefly", str(payload.get("type")))

    payload = account_service._prepare_account_payload({"access_token": "tok-b", "device_token": "dev-b"})
    check("带 device_token 自动归入 adobe 分池", payload["source_type"] == "adobe")

    payload = account_service._prepare_account_payload({"access_token": "tok-c", "refresh_token": "r", "type": "Plus"})
    check("普通 OpenAI 账号不受影响", payload.get("source_type") is None and payload["type"] == "Plus")

    exported = account_service.build_export_items()
    adobe_items = [item for item in exported if item.get("type") == "adobe"]
    check("adobe 账号可导出", len(adobe_items) == 1, json.dumps(exported, ensure_ascii=False)[:200])
    check("导出保留 device_token", adobe_items and adobe_items[0].get("device_token") == "device-token-x")


def main() -> int:
    print("=" * 60)
    print("Adobe Firefly provider 自测")
    print("=" * 60)
    test_model_routing()
    test_generation_options()
    test_request_bodies()
    test_token_parsing()
    test_bridge_and_required()
    test_pool_roundtrip()
    test_arp_session_header()
    test_credential_update()
    test_import_export()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
