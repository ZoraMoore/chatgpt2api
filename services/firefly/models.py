"""Firefly 模型表与生成参数解析。

对外模型名统一带 ``firefly-`` 前缀（如 ``firefly-gpt-image-2``），避免与
chatgpt2api 自有的 ``gpt-image-2`` 撞名；前缀同时作为 provider 路由依据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MODEL_PREFIX = "firefly-"

# generate-async 请求体形态（App/Web 抓包确认的四种差异）。
BODY_KIND_NATIVE = "native"
BODY_KIND_GEMINI = "gemini"
BODY_KIND_GPT_IMAGE = "gpt-image"
BODY_KIND_FLUX = "flux"
BODY_KIND_GENERIC_3P = "3p-generic"


class GenerationOptionError(ValueError):
    """模型不支持该生成参数，附带合法取值供调用方修正。"""

    def __init__(self, field_name: str, value: str, model: str, supported: list[str]) -> None:
        super().__init__(f"{field_name} {value!r} is not supported by model {model}")
        self.field_name = field_name
        self.value = value
        self.model = model
        self.supported = list(supported)


@dataclass(frozen=True)
class ModelSpec:
    """一个可用于反代的 Firefly 生成模型。"""

    alias: str
    display_name: str
    model_id: str
    model_version: str
    third_party: bool
    body_kind: str
    aspect_ratios: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    default_aspect_ratio: str = ""
    default_resolution: str = ""

    @property
    def public_name(self) -> str:
        """对外暴露的 OpenAI 模型名。"""
        return self.alias if self.alias.startswith(MODEL_PREFIX) else f"{MODEL_PREFIX}{self.alias}"


@dataclass
class GenerationOptions:
    """校验并规范化后的生成参数。"""

    aspect_ratio: str = ""
    resolution: str = ""
    width: int = 0
    height: int = 0
    detail_level: int = 0
    seeds: list[int] = field(default_factory=list)


_GPT_IMAGE_2_ASPECT_RATIOS = (
    "auto", "21:9", "16:9", "5:4", "4:3", "3:2", "1:1", "4:5", "3:4", "2:3", "9:16",
)
_NANO_BANANA_3_ASPECT_RATIOS = (
    "auto", "8:1", "4:1", "21:9", "16:9", "5:4", "4:3", "3:2", "1:1",
    "4:5", "3:4", "2:3", "9:16", "1:4", "1:8",
)

BUILTIN_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        alias="firefly-image5",
        display_name="Firefly Image 5",
        model_id="firefly-image",
        model_version="image5",
        third_party=False,
        body_kind=BODY_KIND_NATIVE,
    ),
    ModelSpec(
        alias="Nano Banana 3",
        display_name="Gemini 3.1 (w/ Nano Banana 2)",
        model_id="gemini-flash",
        model_version="nano-banana-3",
        third_party=True,
        body_kind=BODY_KIND_GEMINI,
        aspect_ratios=_NANO_BANANA_3_ASPECT_RATIOS,
        resolutions=("512", "1K", "2K", "4K"),
        default_aspect_ratio="1:1",
        default_resolution="1K",
    ),
    ModelSpec(
        alias="Nano Banana Pro",
        display_name="Gemini 3 (w/ Nano Banana Pro)",
        model_id="gemini-flash",
        model_version="nano-banana-2",
        third_party=True,
        body_kind=BODY_KIND_GEMINI,
        aspect_ratios=_GPT_IMAGE_2_ASPECT_RATIOS,
        resolutions=("1K", "2K", "4K"),
        default_aspect_ratio="1:1",
        default_resolution="1K",
    ),
    ModelSpec(
        alias="gpt-image-2",
        display_name="GPT Image 2",
        model_id="gpt-image",
        model_version="2",
        third_party=True,
        body_kind=BODY_KIND_GPT_IMAGE,
        aspect_ratios=_GPT_IMAGE_2_ASPECT_RATIOS,
        resolutions=("1K", "2K", "4K"),
        default_aspect_ratio="auto",
        default_resolution="1K",
    ),
    ModelSpec(
        alias="gpt-image-1.5",
        display_name="GPT Image 1.5",
        model_id="gpt-image",
        model_version="1.5",
        third_party=True,
        body_kind=BODY_KIND_GPT_IMAGE,
    ),
    ModelSpec(
        alias="flux-pro-2",
        display_name="Flux Pro 2",
        model_id="flux",
        model_version="fluxPro-2",
        third_party=True,
        body_kind=BODY_KIND_FLUX,
    ),
)

# 别名索引（键统一小写、统一剥掉 firefly- 前缀后再登记）。
_ALIAS_INDEX: dict[str, ModelSpec] = {}


def _register(key: str, spec: ModelSpec) -> None:
    _ALIAS_INDEX[key.strip().lower()] = spec


for _spec in BUILTIN_MODELS:
    _register(_spec.alias, _spec)
    _register(_spec.public_name, _spec)
    _register(_spec.display_name, _spec)

# 简写与历史别名，方便调用方书写、且不破坏旧的模型名（改名前的 gemini-flash / nano-banana-2）。
# _target 用新对外名的小写形式（_register 已把 key 统一小写）。
for _short, _target in (
    ("nano-banana", "nano banana 3"),
    ("nano-banana-3", "nano banana 3"),
    ("gemini-flash", "nano banana 3"),
    ("gemini-nano-banana", "nano banana 3"),
    ("nano-banana-2", "nano banana pro"),
    ("nano-banana-pro", "nano banana pro"),
    ("gemini-nano-banana-2", "nano banana pro"),
    ("gpt-image", "gpt-image-2"),
    ("flux", "flux-pro-2"),
    ("image5", "firefly-image5"),
):
    _register(_short, _ALIAS_INDEX[_target])
    _register(f"{MODEL_PREFIX}{_short}", _ALIAS_INDEX[_target])


def is_firefly_model(model: object) -> bool:
    """判断模型名是否路由到 Firefly provider（必须带 firefly- 前缀）。"""
    normalized = str(model or "").strip().lower()
    return normalized.startswith(MODEL_PREFIX) and normalized in _ALIAS_INDEX


def resolve_model(model: object) -> ModelSpec | None:
    normalized = str(model or "").strip().lower()
    return _ALIAS_INDEX.get(normalized)


def list_models() -> list[ModelSpec]:
    return list(BUILTIN_MODELS)


def list_model_names() -> list[str]:
    return [spec.public_name for spec in BUILTIN_MODELS]


def resolve_body_kind(model_id: str, third_party: bool) -> str:
    if not third_party:
        return BODY_KIND_NATIVE
    return {
        "gpt-image": BODY_KIND_GPT_IMAGE,
        "flux": BODY_KIND_FLUX,
        "gemini-flash": BODY_KIND_GEMINI,
    }.get(str(model_id or "").strip().lower(), BODY_KIND_GENERIC_3P)


# ---- OpenAI 参数 → Firefly 参数 ----

_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$", re.IGNORECASE)

# OpenAI quality → Firefly resolution 档位。
_QUALITY_RESOLUTIONS = {
    "low": "1K",
    "standard": "1K",
    "medium": "2K",
    "hd": "2K",
    "high": "4K",
}

_GPT_IMAGE_2_ASPECT_SIZES = {
    "21:9": (1536, 672),
    "16:9": (1344, 768),
    "5:4": (1120, 896),
    "4:3": (1200, 896),
    "3:2": (1248, 832),
    "1:1": (1024, 1024),
    "4:5": (896, 1120),
    "3:4": (896, 1200),
    "2:3": (832, 1248),
    "9:16": (768, 1344),
}

_NANO_BANANA_RESOLUTION_SIZES = {
    "512": (512, 512),
    "1K": (1024, 1024),
    "2K": (2048, 2048),
    "4K": (4096, 4096),
}

_GPT_IMAGE_2_DETAIL_LEVELS = {"1K": 1, "2K": 2, "4K": 3}

# 常见宽高比的近似匹配表，用于把 OpenAI 的 WxH 折算成 Firefly 的 aspect_ratio。
_ASPECT_RATIO_VALUES = {
    "8:1": 8.0, "4:1": 4.0, "21:9": 21 / 9, "16:9": 16 / 9, "5:4": 1.25,
    "4:3": 4 / 3, "3:2": 1.5, "1:1": 1.0, "4:5": 0.8, "3:4": 0.75,
    "2:3": 2 / 3, "9:16": 9 / 16, "1:4": 0.25, "1:8": 0.125,
}


def normalize_aspect_ratio(value: object) -> str:
    text = str(value or "").strip().replace("：", ":").replace(" ", "")
    if not text:
        return ""
    if text.lower() == "auto":
        return "auto"
    return text


def normalize_resolution(value: object) -> str:
    return str(value or "").strip().upper()


def _closest_aspect_ratio(width: int, height: int, candidates: tuple[str, ...]) -> str:
    """把像素尺寸折算成模型支持的最接近的宽高比。"""
    if width <= 0 or height <= 0:
        return ""
    target = width / height
    options = [item for item in candidates if item in _ASPECT_RATIO_VALUES]
    if not options:
        return ""
    return min(options, key=lambda item: abs(_ASPECT_RATIO_VALUES[item] - target))


def _resolution_for_pixels(width: int, height: int, candidates: tuple[str, ...]) -> str:
    """按较长边挑选最接近的分辨率档位。"""
    longest = max(width, height)
    if longest <= 0:
        return ""
    ladder = [(item, _NANO_BANANA_RESOLUTION_SIZES.get(item, (0, 0))[0]) for item in candidates]
    ladder = [(name, px) for name, px in ladder if px > 0]
    if not ladder:
        return ""
    return min(ladder, key=lambda pair: abs(pair[1] - longest))[0]


def resolve_generation_options(
    spec: ModelSpec,
    size: object = None,
    quality: object = None,
    aspect_ratio: object = None,
    resolution: object = None,
    detail_level: int = 0,
    seeds: list[int] | None = None,
) -> GenerationOptions:
    """把 OpenAI 的 size/quality（或显式 aspect_ratio/resolution）映射为 Firefly 参数。

    优先级：显式 aspect_ratio/resolution > size/quality 推导 > 模型默认值。
    """
    normalized_aspect = normalize_aspect_ratio(aspect_ratio)
    normalized_resolution = normalize_resolution(resolution)

    size_text = str(size or "").strip().lower()
    pixel_width = pixel_height = 0
    if size_text and size_text != "auto":
        matched = _SIZE_RE.match(size_text)
        if matched:
            pixel_width, pixel_height = int(matched.group(1)), int(matched.group(2))
        else:
            # 也接受直接传 "16:9" 这类比例串。
            candidate = normalize_aspect_ratio(size_text)
            if candidate in _ASPECT_RATIO_VALUES or candidate == "auto":
                normalized_aspect = normalized_aspect or candidate
    elif size_text == "auto" and not normalized_aspect:
        normalized_aspect = "auto"

    if not normalized_aspect and pixel_width and pixel_height and spec.aspect_ratios:
        normalized_aspect = _closest_aspect_ratio(pixel_width, pixel_height, spec.aspect_ratios)

    if not normalized_resolution:
        quality_text = str(quality or "").strip().lower()
        if quality_text and quality_text != "auto":
            normalized_resolution = _QUALITY_RESOLUTIONS.get(quality_text, "")
        if not normalized_resolution and pixel_width and pixel_height and spec.resolutions:
            normalized_resolution = _resolution_for_pixels(pixel_width, pixel_height, spec.resolutions)

    options = GenerationOptions(seeds=list(seeds or []), detail_level=max(0, int(detail_level or 0)))

    if not spec.aspect_ratios and not spec.resolutions:
        # 未登记能力的模型：直接透传像素尺寸。
        options.width, options.height = pixel_width, pixel_height
        return options

    if not normalized_aspect:
        normalized_aspect = spec.default_aspect_ratio
    if not normalized_resolution:
        normalized_resolution = spec.default_resolution

    if normalized_aspect and spec.aspect_ratios and normalized_aspect not in spec.aspect_ratios:
        raise GenerationOptionError("aspect_ratio", normalized_aspect, spec.public_name, list(spec.aspect_ratios))
    if normalized_resolution and spec.resolutions and normalized_resolution not in spec.resolutions:
        raise GenerationOptionError("resolution", normalized_resolution, spec.public_name, list(spec.resolutions))

    options.aspect_ratio = normalized_aspect
    options.resolution = normalized_resolution

    if spec.body_kind == BODY_KIND_GPT_IMAGE:
        options.detail_level = options.detail_level or _GPT_IMAGE_2_DETAIL_LEVELS.get(normalized_resolution, 0)
        if normalized_aspect and normalized_aspect != "auto":
            options.width, options.height = _GPT_IMAGE_2_ASPECT_SIZES.get(normalized_aspect, (0, 0))
    elif spec.body_kind == BODY_KIND_GEMINI:
        options.width, options.height = _NANO_BANANA_RESOLUTION_SIZES.get(normalized_resolution, (0, 0))
    else:
        options.width, options.height = pixel_width, pixel_height

    return options


def reference_image_usage(spec: ModelSpec) -> str:
    """Adobe 对具体模型要求的参考图用途；Nano Banana 只接受 general。"""
    return "general" if spec.body_kind == BODY_KIND_GEMINI else "subject"
