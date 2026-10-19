import json
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from typing import Any, Sequence

import pybase64

DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
from utils.helper import new_uuid
from utils.sentinel import SentinelTokenGenerator


CORES = [8, 16, 24, 32]
DOCUMENT_KEYS = ["__reactContainer$fzelfjyxej8", "_reactListening5dehydibo78", "location"]
SCREEN_RESOLUTIONS = [[1920, 1080], [1440, 900], [2560, 1440], [3840, 2160]]


class ScriptSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.data_build = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src")
        if not src:
            return
        self.script_sources.append(src)
        match = re.search(r"c/[^/]*/_", src)
        if match:
            self.data_build = match.group(0)


def parse_pow_resources(html_content: str) -> tuple[list[str], str]:
    parser = ScriptSrcParser()
    parser.feed(html_content)
    script_sources = parser.script_sources or [DEFAULT_POW_SCRIPT]
    data_build = parser.data_build
    if not data_build:
        match = re.search(r'<html[^>]*data-build="([^"]*)"', html_content)
        if match:
            data_build = match.group(1)
    return script_sources, data_build


def _legacy_parse_time(timezone_name: str = "UTC") -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    # Date 字符串必须与会话的 IANA 时区、当日夏令时一致，不能固定 EST/-0500。
    return f"{weekdays[now.weekday()]} {months[now.month - 1]} " + now.strftime("%d %Y %H:%M:%S GMT%z (%Z)")


def build_pow_config(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    *,
    timezone_name: str = "UTC",
    browser_context: dict[str, Any] | None = None,
) -> list[Any]:
    navigator_key = random.choice([
        "registerProtocolHandler−function registerProtocolHandler() { [native code] }",
        "storage−[object StorageManager]",
        "locks−[object LockManager]",
        "appCodeName−Mozilla",
        "permissions−[object Permissions]",
        "share−function share() { [native code] }",
        "webdriver−false",
        "managed−[object NavigatorManagedData]",
        "canShare−function canShare() { [native code] }",
        "vendor−Google Inc.",
        "mediaDevices−[object MediaDevices]",
        "vibrate−function vibrate() { [native code] }",
        "storageBuckets−[object StorageBucketManager]",
        "mediaCapabilities−[object MediaCapabilities]",
        "cookieEnabled−true",
        "virtualKeyboard−[object VirtualKeyboard]",
        "product−Gecko",
        "presentation−[object Presentation]",
        "onLine−true",
        "mimeTypes−[object MimeTypeArray]",
        "credentials−[object CredentialsContainer]",
        "serviceWorker−[object ServiceWorkerContainer]",
        "keyboard−[object Keyboard]",
        "gpu−[object GPU]",
        "doNotTrack",
        "serial−[object Serial]",
        "pdfViewerEnabled−true",
        "language−zh-CN",
        "geolocation−[object Geolocation]",
        "userAgentData−[object NavigatorUAData]",
        "getUserMedia−function getUserMedia() { [native code] }",
        "sendBeacon−function sendBeacon() { [native code] }",
        "hardwareConcurrency−32",
        "windowControlsOverlay−[object WindowControlsOverlay]",
    ])
    window_key = random.choice([
        "0",
        "window",
        "self",
        "document",
        "name",
        "location",
        "customElements",
        "history",
        "navigation",
        "innerWidth",
        "innerHeight",
        "scrollX",
        "scrollY",
        "visualViewport",
        "screenX",
        "screenY",
        "outerWidth",
        "outerHeight",
        "devicePixelRatio",
        "screen",
        "chrome",
        "navigator",
        "onresize",
        "performance",
        "crypto",
        "indexedDB",
        "sessionStorage",
        "localStorage",
        "scheduler",
        "alert",
        "atob",
        "btoa",
        "fetch",
        "matchMedia",
        "postMessage",
        "queueMicrotask",
        "requestAnimationFrame",
        "setInterval",
        "setTimeout",
        "caches",
        "__NEXT_DATA__",
        "__BUILD_MANIFEST",
        "__NEXT_PRELOADREADY",
    ])
    context = browser_context or {}
    cores = context.get("hardware_concurrency", 8)
    if navigator_key.startswith("hardwareConcurrency"):
        navigator_key = f"hardwareConcurrency−{cores}"
    if navigator_key.startswith("language−"):
        navigator_key = "language−" + context.get("language", "zh-CN")
    script_source = random.choice(list(script_sources)) if script_sources else DEFAULT_POW_SCRIPT
    time_origin = context.get("time_origin_ms", time.time() * 1000)
    return [
        context.get("screen_width", 1920) + context.get("screen_height", 1080),
        _legacy_parse_time(timezone_name),
        4294705152,
        1,
        user_agent,
        script_source,
        data_build,
        context.get("language", "zh-CN"),
        ",".join(context.get("languages", ["zh-CN", "zh", "en-US", "en"])),
        random.random(),
        navigator_key,
        random.choice(DOCUMENT_KEYS),
        window_key,
        max(0.0, time.time() * 1000 - time_origin),
        new_uuid(),
        "",
        cores,
        time_origin,
        0, 0, 0, 0, 0, 0,
        0,  # 0 = edge/chrome, 1 = firefox
    ]


def _pow_generate(seed: str, difficulty: str, config: list[Any], limit: int = 500000) -> tuple[str, bool]:
    if (not isinstance(seed, str) or not 1 <= len(seed) <= 4096
            or not seed.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in seed)):
        raise ValueError("invalid proof seed")
    if not isinstance(difficulty, str) or not re.fullmatch(r"[0-9a-fA-F]{1,8}", difficulty):
        raise ValueError("invalid proof difficulty")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500000:
        raise ValueError("invalid proof attempt limit")
    if not isinstance(config, list) or not 10 <= len(config) <= 64:
        raise ValueError("invalid proof configuration")
    if len(json.dumps(config, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("proof configuration is too large")

    # 当前网页 SDK 按 FNV 混合哈希的十六进制前缀校验；旧 SHA3 答案即使
    # finalize 返回 200，也不代表后续生成请求已通过校验。复用现有 Sentinel 实现。
    target = difficulty.lower()
    candidate = list(config)
    started = time.monotonic()
    for nonce in range(limit):
        elapsed = time.monotonic() - started
        # 上游难度是不可信输入，同时限制尝试次数和墙钟时间，避免长期占用生图线程。
        if elapsed >= 3:
            break
        candidate[3] = nonce
        candidate[9] = round(elapsed * 1000)
        encoded = pybase64.b64encode(
            json.dumps(candidate, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        digest = SentinelTokenGenerator._fnv1a_32(seed + encoded)
        if digest[:len(target)] <= target:
            return encoded, True
    return "", False


def build_legacy_requirements_token(
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    *,
    timezone_name: str = "UTC",
    browser_context: dict[str, Any] | None = None,
) -> str:
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build, timezone_name=timezone_name, browser_context=browser_context)
    return "gAAAAAC" + pybase64.b64encode(
        json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()


def build_proof_token(
    seed: str,
    difficulty: str,
    user_agent: str,
    script_sources: Sequence[str] | None = None,
    data_build: str = "",
    *,
    timezone_name: str = "UTC",
    browser_context: dict[str, Any] | None = None,
) -> str:
    config = build_pow_config(user_agent, script_sources=script_sources, data_build=data_build, timezone_name=timezone_name, browser_context=browser_context)
    answer, solved = _pow_generate(seed, difficulty, config)
    if not solved:
        raise RuntimeError(f"failed to solve proof token: difficulty={difficulty}")
    # HAR 和当前 SDK 都要求成功标记；不能只拼接答案的 base64。
    return "gAAAAAB" + answer + "~S"
