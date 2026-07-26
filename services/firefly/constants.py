"""Firefly 反代端点与客户端常量（来自 firefly.adobe.com / FireflyAndroid 抓包）。"""

from __future__ import annotations

# ---- 生成 ----
# 第三方模型（Gemini/GPT/Flux 等）异步出图入口。
GENERATE_3P_URL = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
# Firefly 自有模型（Image 5 等）异步出图入口。
GENERATE_NATIVE_URL = "https://image-v5.ff.adobe.io/v1/images/generate-async"
# 第三方模型参考图片的裸二进制上传入口。
UPLOAD_IMAGE_URL = "https://firefly-3p.ff.adobe.io/v2/storage/image"

# 风控校验用的固定来源头。
WEB_ORIGIN = "https://firefly.adobe.com"
WEB_REFERER = "https://firefly.adobe.com/"

# ---- IMS 鉴权 ----
# firefly.adobe.com 全站使用的公开 client_id，同时作为 API 的 x-api-key。
DEFAULT_CLIENT_ID = "clio-playground-web"
# 凭 Adobe SSO Cookie 换取 access_token。
IMS_TOKEN_CHECK_URL = "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
# App 端 device_token 换票端点。
IMS_TOKEN_V4_URL = "https://adobeid-na1.services.adobe.com/ims/token/v4"
# 用 Android access 换取跳转到 Web client 的 jump URL（桥接第 2 步）。
IMS_JUMPTOKEN_URL = "https://ims-na1.adobelogin.com/ims/jumptoken/v1"
# 用 Bearer 校验 access_token 并读取用户信息。
IMS_PROFILE_URL = "https://ims-na1.adobelogin.com/ims/profile"

# FireflyAndroid 登录抓包（client_secret 嵌在 APK 内，属公开客户端密钥）。
ANDROID_CLIENT_ID = "FireflyAndroid"
ANDROID_CLIENT_SECRET = "p8e-u7JuBP6vaHhLfNn31xDfjdUbyLbqLj0t"
ANDROID_REDIRECT_URI = "fireflymobile://path"

# 桥接第 2 步 jumptoken 的 target_redirect_uri（Web client 回跳地址）。
DEFAULT_REDIRECT_URI = "https://firefly.adobe.com/"

# /ims/check/v6/token 使用的 scope 集合（比授权弹窗多出 creative_production）。
DEFAULT_SCOPES = (
    "AdobeID",
    "firefly_api",
    "openid",
    "pps.read",
    "pps.write",
    "additional_info.projectedProductContext",
    "additional_info.ownerOrg",
    "uds_read",
    "uds_write",
    "ab.manage",
    "read_organizations",
    "additional_info.roles",
    "account_cluster.read",
    "creative_production",
    "tk_platform",
    "tk_platform_sync",
    "profile",
)

# ---- 轮询与限额 ----
DEFAULT_POLL_INTERVAL_SECS = 2.0
DEFAULT_POLL_TIMEOUT_SECS = 180.0
DEFAULT_REQUEST_TIMEOUT_SECS = 60
# token 过期前的提前续期量，避免临界点请求失败。
REFRESH_LEEWAY_SECS = 5 * 60

# curl_cffi 浏览器指纹（与项目其它出站请求保持一致）。
DEFAULT_IMPERSONATE = "chrome110"

# 参考图片限制。
MAX_REFERENCE_IMAGE_BYTES = 10 << 20
SUPPORTED_REFERENCE_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
