"""OpenAI 兼容视觉模型批量描述标注(描述页), 结果写入各工作目录的 ``captions.json``.

输出格式与 diffusion-pipe 的目录级 caption 约定一致: 键为图片文件名(basename),
值为描述列表(本模块按「粗略 → 详细」顺序写入). diffusion-pipe 侧只要目录内存在
``captions.json`` 即按其内容取 caption 并忽略同名 ``.txt``, 无需开启
``online_captions``(该开关会改用图片绝对路径取 caption, 与 basename 键不匹配).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import os
import random
from collections import deque
from collections.abc import Callable
from functools import partial
from pathlib import Path

import httpx
from jinja2 import Environment, StrictUndefined, Template
from PIL import Image

from services.analysis.wd14 import (
    DEFAULT_THRESHOLD,
    Wd14Cache,
    Wd14Labels,
    Wd14Tag,
    Wd14TagGroups,
)
from services.core.atomic import atomic_write_text
from services.core.jobs import JobProgress
from services.dataset.workdir import CAPTIONS_JSON_FILE, WorkDir
from services.models import CaptionImageItem, CaptionResult, CaptionRule, CaptionRuleGroup
from services.tagging.clean import make_tag_matcher, tag_key

logger = logging.getLogger(__name__)

# OpenAI 兼容描述服务: API 根地址(含 /v1)与视觉模型名
BASE_URL = os.environ.get("CAPTION_OPENAI_BASE_URL", "").strip()
MODEL = os.environ.get("CAPTION_OPENAI_MODEL", "").strip()
# 服务密钥; 留空时不发送 Authorization 头
API_KEY = os.environ.get("CAPTION_OPENAI_API_KEY", "").strip()
# 同时处理的图片数(CAPTION_CONCURRENCY), 需与服务端承载能力匹配
CONCURRENCY = int(os.environ.get("CAPTION_CONCURRENCY", "").strip() or 1)
# 附加请求体(CAPTION_OPENAI_EXTRA_BODY): JSON 对象, 原样合并进请求体, 用于传递
# 思考开关与等级等服务端扩展字段; 留空不合并
EXTRA_BODY: dict = json.loads(os.environ.get("CAPTION_OPENAI_EXTRA_BODY", "").strip() or "{}")
if not isinstance(EXTRA_BODY, dict):
    raise TypeError("CAPTION_OPENAI_EXTRA_BODY 须为 JSON 对象")
# 附加请求头(CAPTION_OPENAI_HEADERS): JSON 对象, 与默认 UA、Authorization 合并,
# 用于覆盖 UA 或传服务端要求的会话标识等头
EXTRA_HEADERS: dict = json.loads(
    os.environ.get("CAPTION_OPENAI_HEADERS", "").strip() or "{}"
)
if not isinstance(EXTRA_HEADERS, dict):
    raise TypeError("CAPTION_OPENAI_HEADERS 须为 JSON 对象")

# 单次生成的最大 token 数(CAPTION_MAX_TOKENS): 0 表示不向服务传该项, 由服务端默认值决定
MAX_TOKENS = int(os.environ.get("CAPTION_MAX_TOKENS", "").strip() or 8192)
# 单张生成超时(秒, CAPTION_TIMEOUT)
TIMEOUT_SEC = float(os.environ.get("CAPTION_TIMEOUT", "").strip() or 60)
# 单张图片长边像素上限(CAPTION_MAX_IMAGE_EDGE): 超过该值时等比缩小, 0 表示不限制;
# 8192 为多数 OpenAI 兼容视觉服务的单边上限, 超限图片会被以「不支持的图像」拒绝
MAX_IMAGE_EDGE = int(os.environ.get("CAPTION_MAX_IMAGE_EDGE", "").strip() or 8192)
# 压缩重编码时 JPEG 与 WebP 的质量档位; 其余格式为无损编码, 字节缩减只靠缩小尺寸
_ENCODE_QUALITY = 95
# 单张图片生成失败后的最大重试次数
MAX_RETRIES = 3
# 探测服务可用性的请求超时(秒)
PROBE_TIMEOUT_SEC = 10.0

# 图片扩展名到 data URL 媒体类型与 Pillow 编码格式的映射, 覆盖 ``workdir.IMAGE_EXTS``
# 的全部取值
_IMAGE_FORMATS = {
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
    ".webp": ("image/webp", "WEBP"),
    ".bmp": ("image/bmp", "BMP"),
    ".gif": ("image/gif", "GIF"),
}

# 提示词模板环境: 模块内内嵌模板的解析与渲染入口; 未定义变量直接报错, 禁止静默空串
_JINJA_ENV = Environment(undefined=StrictUndefined)

# 各描述模式的系统提示词模板(内容由用户给定); 模板变量 patch=NSFW 增强补丁, 为空时不输出
SYSTEM_PROMPT_TEMPLATES: dict[str, Template] = {
    "coarse": _JINJA_ENV.from_string(
        "{% if patch %}{{ patch }}\n\n{% endif %}"
        "<Role>\n"
        "You are an expert image annotator processing an image dataset.\n"
        "</Role>\n\n"
        "<Objective>\n"
        "Write a straightforward, concise caption confidently describing the primary focal point of the image.\n"
        "</Objective>\n\n"
        "<Rules>\n"
        "1. Structure & Style: Output strictly in unbroken, compactly formatted English (no line breaks). Vary your sentence structure. Begin with the main subject and medium. NEVER start with \"This image is...\" or similar phrasing.\n"
        "2. Content Scope: Mention pivotal elements using confident, definite language. Focus on concrete details like color, shape, texture, and spatial relationships. Note any watermarks, signatures, or compression artifacts.\n"
        "3. Exclusions: Strictly omit mood, speculative wording, absent elements, image resolution, or any unobservable details.\n"
        "4. Transcription: Transcribe any embedded text exactly in its original language, enclosed in \"quotation marks\".\n"
        "5. User Notes: Strictly apply user-provided notes to append required details or filter out restricted content.\n"
        "</Rules>"
    ),
    "detailed": _JINJA_ENV.from_string(
        "{% if patch %}{{ patch }}\n\n{% endif %}"
        "<Role>\n"
        "You are an expert image annotator processing an image dataset.\n"
        "</Role>\n\n"
        "<Objective>\n"
        "Write a comprehensive, straightforward caption confidently describing all visible content in the image.\n"
        "</Objective>\n\n"
        "<Rules>\n"
        "1. Structure & Style: Output strictly in unbroken, compactly formatted English (no line breaks). Vary your sentence structure. Begin with the main subject and medium. NEVER start with \"This image is...\" or similar phrasing.\n"
        "2. Content Scope: Explicitly specify all entities (people, objects, scenery). Focus on concrete details like color, shape, texture, and spatial relationships. Show exactly how elements interact using confident, definite language. Note any watermarks, signatures, or compression artifacts.\n"
        "3. Exclusions: Strictly omit mood, speculative wording, absent elements, image resolution, or any unobservable details.\n"
        "4. Transcription: Transcribe any embedded text exactly in its original language, enclosed in \"quotation marks\".\n"
        "5. User Notes: Strictly apply user-provided notes to append required details or filter out restricted content.\n"
        "</Rules>"
    ),
}

# NSFW 增强触发时注入系统提示词头部的补丁(素材文件)
_SYSTEM_PROMPT_PATCH = (
    Path(__file__).resolve().parents[2] / "assets" / "purge.txt"
).read_text(encoding="utf-8").strip()

# 用户消息模板: 每行一条, 预设提示词在前、NSFW 备注次之、WD14 参考标签在后;
# 标签行首与 NSFW 备注等提示词正文全部内联于此; 不添加分节标题与列表符号.
# 每行固定以换行收尾, 末尾多余换行由 ``build_user_prompt`` 去掉.
# 模板变量: instructions=命中的预设提示词文本, reference=WD14 参考标签分组, nsfw=是否输出 NSFW 备注
_USER_MESSAGE_TEMPLATE = _JINJA_ENV.from_string(
    "{% for text in instructions %}{{ text }}\n{% endfor %}"
    "{% if nsfw %}Using explicit slang.\n{% endif %}"
    "{% for category, items in reference %}"
    "{% if category == 'general' %}来自 WD14 的 Danbooru 通用标签可供参考{% else %}来自 WD14 的 Danbooru 角色标签可供参考{% endif %}: "
    "{% for tag in items %}\"{{ tag.name }}\":{{ '%.2f'|format(tag.score) }}{{ ', ' if not loop.last }}{% endfor %}\n"
    "{% endfor %}"
)

# NSFW 增强判定的 rating 分级名: 二者任一达标即触发用户消息模板的 nsfw 分支
_NSFW_RATING_TAGS = ("explicit", "questionable")


# 预设提示词模板: 键为界面展示名, 值为内嵌 Jinja 模板源码(待填字段即其未声明变量);
# 只收带变量的句式, 取值与触发标签由用户在条目上设置
RULE_TEMPLATES: dict[str, str] = {
    "角色数据集": (
        '这是一个角色数据集, 主角是"{{ 角色名 }}", 故不需要详细描述主角本身, 使用角色名即可. '
        "主角的特征是{{ 主角特征 }}, 供你辨别."
    ),
    "画风数据集": (
        '这是一个画风数据集, 艺术家是"{{ 艺术家名 }}", 故不需要详细描述画风本身, 使用艺术家名即可.'
    ),
    "概念数据集": (
        '这是一个概念数据集, 主题是"{{ 概念名 }}", 故不需要详细描述概念本身, 使用概念名即可.'
    ),
    "负面偏见标注": "数据集存在{{ 偏见项 }}的负面偏见, 因此务必在符合的图片中明确标注.",
    "角色特征触发词": '触发词 "{{ 触发词 }}" 代表{{ 特征说明 }}, 当画面中可见时添加.',
}


# 描述筛选预置: 展示名 → (匹配模式, 模式串); regex 项用于定位含拒绝话术的描述,
# 内联 (?i) 大小写标志由前端正则构造时转为 i 标志
FILTER_PRESETS: dict[str, tuple[str, str]] = {
    "拒绝话术": (
        "regex",
        (
            r"(?i)\b(?:(?:i\s+)?(?:can(?:'|’)?t|cannot|won(?:'|’)?t|will\s+not)|"
            r"(?:(?:i(?:'|’)?m|i\s+am)\s+)?(?:not\s+able|unable)\s+to|"
            r"(?:(?:i(?:'|’)?m|i\s+am)\s+)?sorry|i\s+apologize|"
            r"(?:i\s+must\s+)?refuse(?:\s+to)?|i\s+have\s+to\s+decline|"
            r"as\s+an\s+ai\s+(?:language\s+model|assistant)|"
            r"against\s+my\s+(?:guidelines|policy)|not\s+something\s+i\s+can|"
            r"out\s+of\s+my\s+scope|i\s+can\s+only\s+provide\s+general|"
            r"please\s+understand\s+that|i(?:'|’)?d\s+recommend|ethical|"
            r"safety\s+guidelines|policy\s+violation|illegal|unauthorized)\b"
        ),
    ),
}


def _rule_texts(rules: list[CaptionRule], tag_keys: set[str]) -> list[str]:
    """挑出对当前图片生效的预设提示词文本.

    条目按其 ``pattern``(逗号分隔的标签模式)判定, 命中判定复用标签清洗页的
    ``make_tag_matcher``(``danbooru`` 通配, 作用于 ``tag_key`` 归一化标签);
    ``pattern`` 为空串时不做判定, 该条目对全部图片生效.

    Args:
        rules (list[CaptionRule]): 预设提示词条目.
        tag_keys (set[str]): 该图片 WD14 缓存标签的归一化键集合.

    Returns:
        list[str]: 生效的提示词文本(保持条目顺序, 丢弃空文本).

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    out: list[str] = []
    for rule in rules:
        text = rule.text.strip()
        if not text:
            continue
        patterns = [p.strip() for p in rule.pattern.split(",") if p.strip()]
        if not patterns:
            out.append(text)
            continue
        matcher = make_tag_matcher(patterns, "danbooru")
        if any(matcher(key) for key in tag_keys):
            out.append(text)
    return out


def _group_texts(
    groups: list[CaptionRuleGroup], tag_keys: set[str], rng: random.Random
) -> list[str]:
    """按命中条件与抽取方式挑出对当前图片生效的提示词文本.

    组按其 ``pattern``(逗号分隔的标签模式)判定, 命中判定复用标签清洗页的
    ``make_tag_matcher``(``danbooru`` 通配, 作用于 ``tag_key`` 归一化标签);
    ``pattern`` 为空串时不做判定, 该组对全部图片生效. 权重为 0 的候选不参与抽取.

    互斥组先对候选权重做 softmax 归一化, 再随机抽取一条注入; 非互斥组把每条权重
    钳制到 [0, 1] 作为该条命中的概率, 独立判定并注入全部命中的候选.

    Args:
        groups (list[CaptionRuleGroup]): 预设提示词组.
        tag_keys (set[str]): 该图片 WD14 缓存标签的归一化键集合.
        rng (random.Random): 本次任务的随机源.

    Returns:
        list[str]: 生效的提示词文本(保持组的顺序与组内候选顺序, 丢弃空文本).

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    out: list[str] = []
    for group in groups:
        patterns = [p.strip() for p in group.pattern.split(",") if p.strip()]
        if patterns:
            matcher = make_tag_matcher(patterns, "danbooru")
            if not any(matcher(key) for key in tag_keys):
                continue
        candidates = [
            item for item in group.items if item.text.strip() and item.weight > 0
        ]
        if not candidates:
            continue
        if group.exclusive:
            peak = max(item.weight for item in candidates)
            weights = [math.exp(item.weight - peak) for item in candidates]
            out.append(rng.choices(candidates, weights=weights, k=1)[0].text.strip())
        else:
            for item in candidates:
                if rng.random() < min(item.weight, 1.0):
                    out.append(item.text.strip())
    return out


def _needs_wd14(
    rules: list[CaptionRule],
    groups: list[CaptionRuleGroup],
    general_threshold: float | None,
    character_threshold: float | None,
    nsfw_threshold: float | None,
) -> bool:
    """判断本次描述是否需要 WD14 标签.

    两类用途都取自 ``.wd14_meta`` 缓存: 参考标签注入(通用或角色任一启用),
    NSFW 增强(rating 分级判定), 以及带「匹配标签」的预设提示词条目或抽取组
    (其命中判定基于该图片的缓存标签).

    Args:
        rules (list[CaptionRule]): 预设提示词条目.
        groups (list[CaptionRuleGroup]): 预设提示词组.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.
        nsfw_threshold (float | None): NSFW 增强的置信度下限; None 为不启用.

    Returns:
        bool: 需要 WD14 标签时为 True.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    if (
        general_threshold is not None
        or character_threshold is not None
        or nsfw_threshold is not None
    ):
        return True
    return any(rule.pattern.strip() for rule in rules) or any(
        group.pattern.strip() for group in groups
    )


def _headers() -> dict[str, str]:
    """组装请求头: 默认 UA、配置的附加头与认证头, 后者覆盖前者.

    Returns:
        dict[str, str]: 请求头; 密钥为空时不含 ``Authorization``.

    Callers:
        - ``services/analysis/caption.py:openai_status``
        - ``services/analysis/caption.py:run_caption``
    """
    headers = {"User-Agent": "web-annotator-caption/1.0", **EXTRA_HEADERS}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def openai_status() -> tuple[bool, str]:
    """探测描述服务的配置与就绪状态; 未就绪时描述页不可提交任务.

    连通性与模型可用性经 ``GET {BASE_URL}/models`` 校验.

    Returns:
        tuple[bool, str]: ``(是否就绪, 说明文本)``.

    Callers:
        - ``routes/caption.py:dirs``
        - ``routes/caption.py:start``
    """
    if not BASE_URL or not MODEL:
        return False, "未配置 CAPTION_OPENAI_BASE_URL / CAPTION_OPENAI_MODEL"
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_SEC) as client:
            resp = client.get(f"{BASE_URL.rstrip('/')}/models", headers=_headers())
            resp.raise_for_status()
            names = [str(m.get("id", "")) for m in resp.json().get("data", [])]
    except httpx.HTTPError as e:
        logger.warning("描述服务不可用: %s", e)
        return False, f"无法连接 {BASE_URL}: {e}"
    if MODEL not in names:
        logger.warning("描述服务未提供模型 %s", MODEL)
        return False, f"{BASE_URL} 未提供模型 {MODEL}"
    return True, f"服务就绪: {BASE_URL} · {MODEL}"


def _reference_tags(
    groups: Wd14TagGroups | None,
    general_threshold: float | None,
    character_threshold: float | None,
) -> list[tuple[str, list[Wd14Tag]]]:
    """按各类别的置信度下限挑选参考标签, 保持缓存句柄给出的分组.

    通用标签与角色标签各自独立: 下限为 None 表示该类别不注入, 否则只保留置信度不低于
    该下限的标签.

    Args:
        groups (Wd14TagGroups | None): WD14 分组标签; 未打标时为 None.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.

    Returns:
        list[tuple[str, list[Wd14Tag]]]: ``(标签类型, 标签列表)``, 通用标签在前、
        角色标签在后(各自保持置信度降序); 未注入或该类型无达标标签时不含对应项.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    if groups is None:
        return []
    picked: list[tuple[str, list[Wd14Tag]]] = []
    for category, threshold in (
        ("general", general_threshold),
        ("character", character_threshold),
    ):
        if threshold is None:
            continue
        selected = [tag for tag in groups.of(category) if tag.score >= threshold]
        if selected:
            picked.append((category, selected))
    return picked


def build_user_prompt(
    *,
    reference: list[tuple[str, list[Wd14Tag]]],
    instructions: list[str],
    nsfw: bool,
) -> str:
    """组装单张图片的用户提示词.

    按「预设提示词 → NSFW 备注 → WD14 参考标签」顺序逐行拼接; 全部行文正文内联于
    ``_USER_MESSAGE_TEMPLATE``; 不添加任何分节标题或列表符号.

    Args:
        reference (list[tuple[str, list[Wd14Tag]]]): WD14 参考标签, 按标签类型分组.
        instructions (list[str]): 命中的预设提示词文本.
        nsfw (bool): 是否输出 NSFW 备注行.

    Returns:
        str: 逐行拼接的提示词全文.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    return _USER_MESSAGE_TEMPLATE.render(
        reference=reference,
        instructions=instructions,
        nsfw=nsfw,
    ).rstrip("\n")


def _shrink_image(img: Image.Image, fmt: str, edge: int) -> bytes:
    """按长边上限等比缩小图像并以源格式编码.

    调色板图像先按透明信息转为 RGBA 或 RGB; JPEG 与 WebP 使用高质量档位,
    PNG/GIF/BMP 使用无损编码; EXIF 与 ICC 配置保留.

    Args:
        img (Image.Image): 打开的源图(单帧), 长边须大于 ``edge``.
        fmt (str): Pillow 编码格式名, 取自 ``_IMAGE_FORMATS``.
        edge (int): 长边像素上限.

    Returns:
        bytes: 缩小后的编码字节.

    Callers:
        - ``services/analysis/caption.py:_image_data_url``
    """
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    scale = edge / max(img.size)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    frame = img.resize(size, Image.Resampling.LANCZOS)
    params: dict = {"optimize": True}
    if fmt in ("JPEG", "WEBP"):
        params["quality"] = _ENCODE_QUALITY
    if fmt in ("JPEG", "WEBP", "PNG"):
        exif = img.info.get("exif")
        if exif:
            params["exif"] = exif
        icc = img.info.get("icc_profile")
        if icc:
            params["icc_profile"] = icc
    buf = io.BytesIO()
    frame.save(buf, format=fmt, **params)
    return buf.getvalue()


def _data_url(mime: str, data: bytes) -> str:
    """把图片字节编码为 data URL.

    Args:
        mime (str): 媒体类型.
        data (bytes): 图片字节.

    Returns:
        str: ``data:<媒体类型>;base64,<载荷>``.

    Callers:
        - ``services/analysis/caption.py:_image_data_url``
    """
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _image_data_url(src: Path) -> str:
    """读取图片生成内联 data URL, 长边超限时按原格式缩小.

    长边不超过 ``MAX_IMAGE_EDGE`` 时原样发送原始文件字节; 超过时保持源格式与
    透明通道重新编码, 多帧图片只取首帧.

    Args:
        src (Path): 图片绝对路径.

    Returns:
        str: ``data:<媒体类型>;base64,<载荷>``.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    raw = src.read_bytes()
    mime, fmt = _IMAGE_FORMATS[src.suffix.lower()]
    if MAX_IMAGE_EDGE == 0:
        return _data_url(mime, raw)
    with Image.open(io.BytesIO(raw)) as img:
        if max(img.size) <= MAX_IMAGE_EDGE:
            return _data_url(mime, raw)
        data = _shrink_image(img, fmt, MAX_IMAGE_EDGE)
    return _data_url(mime, data)


async def _stream_caption(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict,
    cancel_check: Callable[[], bool],
) -> str:
    """发送流式 chat 请求并返回回答全文, 思考片段不计入.

    Args:
        client (httpx.AsyncClient): 共用的事件循环内 HTTP 客户端.
        url (str): ``/chat/completions`` 完整地址.
        headers (dict[str, str]): 请求头.
        payload (dict): OpenAI 兼容请求体.
        cancel_check (Callable[[], bool]): 协作取消判定, 返回 True 时中止本次请求.

    Returns:
        str: 模型回答全文(不含思考过程).

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    parts: list[str] = []
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.is_error:
            detail = (await resp.aread()).decode("utf-8", "replace").strip()
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {detail}", request=resp.request, response=resp
            )
        async for line in resp.aiter_lines():
            if cancel_check():
                raise RuntimeError("已取消")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))
            choices = obj.get("choices") or []
            if not choices:
                continue
            chunk = (choices[0].get("delta") or {}).get("content") or ""
            if chunk:
                parts.append(chunk)
    content = "".join(parts).strip()
    if not content:
        raise ValueError("模型回答为空")
    return content


def load_captions(directory: Path) -> dict[str, list[str]]:
    """读取工作目录的 ``captions.json``.

    Args:
        directory (Path): 工作目录绝对路径.

    Returns:
        dict[str, list[str]]: 文件名的描述列表; 文件不存在时为空字典.

    Callers:
        - ``services/analysis/caption.py:run_caption``
        - ``routes/caption.py:list_images``
        - ``routes/caption.py:delete_many``
    """
    path = directory / CAPTIONS_JSON_FILE
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): [str(c) for c in captions] for name, captions in data.items()}


def write_captions(directory: Path, data: dict[str, list[str]]) -> None:
    """把完整的描述映射写入工作目录的 ``captions.json``.

    键按文件名排序后整体原子写入, 使文件内容稳定且不会出现半截结果. 并行分发时
    调用方须持有该目录的写入锁, 由内存映射整体写入, 避免并发读-改-写互相覆盖.

    Args:
        directory (Path): 工作目录绝对路径.
        data (dict[str, list[str]]): 文件名 → 描述列表的完整映射.

    Returns:
        None

    Callers:
        - ``services/analysis/caption.py:run_caption``
        - ``routes/caption.py:delete_many``
    """
    ordered = {key: data[key] for key in sorted(data)}
    atomic_write_text(
        directory / CAPTIONS_JSON_FILE,
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
    )


def _log(progress: JobProgress, line: str) -> None:
    """追加一条进度日志并输出到服务端日志.

    Args:
        progress (JobProgress): 进度对象.
        line (str): 日志行文本.

    Returns:
        None

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    progress.logs.append(line)
    logger.info("描述标注 %s", line)


async def run_caption(
    works: list[WorkDir],
    *,
    paths: set[str] | None,
    modes: list[str],
    general_threshold: float | None,
    character_threshold: float | None,
    nsfw_threshold: float | None,
    rules: list[CaptionRule],
    groups: list[CaptionRuleGroup],
    force: bool,
    progress: JobProgress,
    on_update: Callable[[], None] | None = None,
) -> CaptionResult:
    """批量描述: ``CONCURRENCY`` 个工作协程共用一条队列轮转取图, 结果写入 ``captions.json``.

    每张图片按 ``modes`` 顺序各请求一次(系统提示词随模式切换); 长边超过
    ``MAX_IMAGE_EDGE`` 时先按原格式缩小尺寸再内联发送. ``force`` 为真时先
    清除待处理图片的已有描述再重新标注; 为假时跳过已存在描述的图片. 单张失败重新
    入队, 至多 ``MAX_RETRIES`` 次, 仍失败才记入失败并继续; 客户端错误(4xx, 429 除外)
    不重试. 取消后不再取新图, 在途请求在下一个流式数据块处中断.

    参考标签注入、NSFW 增强与「匹配标签」命中判定都取自 WD14 缓存, 故生成前先对
    缺失缓存的待处理图片补建 ``.wd14_meta``(见 ``_needs_wd14`` 与
    ``Wd14Cache.populate``); 缓存保存整条概率向量, 取标签的阈值取默认值与各注入
    下限中的最低者.

    Args:
        works (list[WorkDir]): 待描述的工作目录列表.
        paths (set[str] | None): 显式图片相对路径集合; None 表示处理 ``works`` 内全部
            图片. 仅作用于本次任务, 不影响 ``captions.json`` 中其它图片的条目.
        modes (list[str]): 描述模式列表, 取值属于 ``SYSTEM_PROMPT_TEMPLATES``.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.
        nsfw_threshold (float | None): NSFW 增强的置信度下限; None 为不启用.
        rules (list[CaptionRule]): 预设提示词条目.
        groups (list[CaptionRuleGroup]): 预设提示词组.
        force (bool): 是否清除待处理图片的已有描述并重新标注.
        progress (JobProgress): 进度对象.
        on_update (Callable[[], None] | None): 进度变更回调.

    Returns:
        CaptionResult: 含本次成功写入的图片与其描述、成功/失败/跳过计数.

    Raises:
        ValueError: 描述服务配置缺失.

    Callers:
        - ``routes/caption.py:start``
    """
    root = works[0].dataset.root
    items = [p for work in works for p in work.image_files()]
    if paths is not None:
        items = [p for p in items if p.relative_to(root).as_posix() in paths]
    if not BASE_URL or not MODEL:
        raise ValueError("未配置 CAPTION_OPENAI_BASE_URL / CAPTION_OPENAI_MODEL")
    dir_names = [w.name for w in works]
    progress.total = len(items)
    progress.done = progress.ok = progress.failed = progress.skipped = 0
    progress.status = progress.phase = "running"
    progress.message = f"共 {len(items)} 张, 并发 {CONCURRENCY}"
    _log(
        progress,
        f"开始: dirs={dir_names}, modes={modes}, general_threshold={general_threshold}, "
        f"character_threshold={character_threshold}, nsfw_threshold={nsfw_threshold}, "
        f"groups={len(groups)}, "
        f"force={force}, total={len(items)}, 并发={CONCURRENCY}, "
        f"长边上限={MAX_IMAGE_EDGE or '不限'}px, "
        f"附加={EXTRA_BODY or '无'}, 服务={BASE_URL} · {MODEL}",
    )
    if on_update:
        on_update()

    if not items:
        progress.status = progress.phase = "done"
        progress.message = "没有可处理的图片"
        _log(progress, progress.message)
        if on_update:
            on_update()
        return CaptionResult(dirs=dir_names)

    stores = {work.path: load_captions(work.path) for work in works}
    if force:
        cleared = 0
        touched: set[Path] = set()
        for src in items:
            if stores[src.parent].pop(src.name, None) is not None:
                cleared += 1
                touched.add(src.parent)
        for parent in touched:
            write_captions(parent, stores[parent])
        _log(progress, f"已清除 {cleared} 张图片的已有描述")
        if on_update:
            on_update()
    pending: list[Path] = []
    for src in items:
        if not force and stores[src.parent].get(src.name):
            progress.done += 1
            progress.skipped += 1
            _log(progress, f"[{progress.done}/{progress.total}] 跳过(已有描述): {src.relative_to(root).as_posix()}")
        else:
            pending.append(src)
    if on_update:
        on_update()

    thresholds = [t for t in (general_threshold, character_threshold, nsfw_threshold) if t is not None]
    wd14_threshold = min([DEFAULT_THRESHOLD, *thresholds])
    cache: Wd14Cache | None = None
    if _needs_wd14(rules, groups, general_threshold, character_threshold, nsfw_threshold):
        cache = Wd14Cache(root, Wd14Labels.load())
        # WD14 推理是 CPU 密集操作, 交给线程执行以免阻塞事件循环; 其内部逐张检查取消
        await asyncio.to_thread(
            cache.populate,
            cache.missing(pending),
            log=partial(_log, progress),
            cancel_check=lambda: progress.cancel_requested,
        )
        if on_update:
            on_update()

    results: list[CaptionImageItem] = []
    queue = deque(pending)
    rng = random.Random()
    url = f"{BASE_URL.rstrip('/')}/chat/completions"
    headers = _headers()
    attempts: dict[Path, int] = {}

    async def serve() -> None:
        """消费共享队列: 取图生成描述并写盘, 失败项重新入队至多 ``MAX_RETRIES`` 次.

        Returns:
            None

        Callers:
            - ``services/analysis/caption.py:run_caption`` (交给 ``asyncio.gather``)
        """
        while queue and not progress.cancel_requested:
            src = queue.popleft()
            rel = src.relative_to(root).as_posix()
            progress.current = rel
            progress.message = f"[{progress.done + 1}/{progress.total}] {rel}"
            try:
                wd14_groups = cache.groups(src, wd14_threshold) if cache else None
                tag_keys = {tag_key(tag.name) for tag in (wd14_groups.tags if wd14_groups else ())}
                instructions = _rule_texts(rules, tag_keys) + _group_texts(
                    groups, tag_keys, rng
                )
                nsfw = wd14_groups is not None and nsfw_threshold is not None and any(
                    tag.name in _NSFW_RATING_TAGS and tag.score >= nsfw_threshold
                    for tag in wd14_groups.of("rating")
                )
                prompt = build_user_prompt(
                    reference=_reference_tags(
                        wd14_groups, general_threshold, character_threshold
                    ),
                    instructions=instructions,
                    nsfw=nsfw,
                )
                image_url = await asyncio.to_thread(_image_data_url, src)
                captions: list[str] = []
                for mode in modes:
                    payload: dict = {
                        "model": MODEL,
                        "messages": [
                            {
                                "role": "system",
                                "content": SYSTEM_PROMPT_TEMPLATES[mode].render(
                                    patch=_SYSTEM_PROMPT_PATCH if nsfw else ""
                                ),
                            },
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": image_url}},
                                ],
                            },
                        ],
                        "stream": True,
                    }
                    if MAX_TOKENS > 0:
                        payload["max_tokens"] = MAX_TOKENS
                    payload.update(EXTRA_BODY)
                    captions.append(
                        await _stream_caption(
                            client, url, headers, payload, lambda: progress.cancel_requested
                        )
                    )
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as e:
                if progress.cancel_requested:
                    return
                attempts[src] = attempts.get(src, 0) + 1
                permanent = (
                    isinstance(e, httpx.HTTPStatusError)
                    and 400 <= e.response.status_code < 500
                    and e.response.status_code != httpx.codes.TOO_MANY_REQUESTS
                )
                if permanent or attempts[src] > MAX_RETRIES:
                    progress.done += 1
                    progress.failed += 1
                    _log(progress, f"[{progress.done}/{progress.total}] 失败: {rel} — {e}")
                else:
                    queue.append(src)
                    _log(progress, f"失败, 重新排队重试 {attempts[src]}/{MAX_RETRIES}: {rel} — {e}")
            else:
                store = stores[src.parent]
                store[src.name] = captions
                write_captions(src.parent, store)
                progress.done += 1
                progress.ok += 1
                _log(progress, f"[{progress.done}/{progress.total}] 完成: {rel}")
                for mode, caption in zip(modes, captions):
                    _log(progress, f"  {mode}: {caption}")
                results.append(
                    CaptionImageItem(
                        path=rel,
                        stem=src.stem,
                        dir=src.parent.relative_to(root).as_posix(),
                        captions=captions,
                    )
                )
            if on_update:
                on_update()

    if pending:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            await asyncio.gather(*(serve() for _ in range(CONCURRENCY)))

    progress.current = ""
    if progress.cancel_requested:
        progress.status = "cancelled"
        progress.phase = "done"
        progress.message = (
            f"已取消 (成功 {progress.ok} / 跳过 {progress.skipped} / 失败 {progress.failed})"
        )
    else:
        progress.status = progress.phase = "done"
        progress.message = (
            f"完成: 新标注 {progress.ok} 张, 跳过 {progress.skipped} 张, "
            f"失败 {progress.failed} 张, 合计 {progress.total} 张"
        )
    _log(progress, progress.message)
    if on_update:
        on_update()
    return CaptionResult(
        dirs=dir_names,
        ok=progress.ok,
        failed=progress.failed,
        skipped=progress.skipped,
        items=results,
    )

