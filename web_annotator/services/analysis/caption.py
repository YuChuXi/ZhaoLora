"""Ollama 视觉模型批量描述标注(描述页), 结果写入各工作目录的 ``captions.json``.

输出格式与 diffusion-pipe 的目录级 caption 约定一致: 键为图片文件名(basename),
值为描述列表(本模块按「粗略 → 详细」顺序写入). diffusion-pipe 侧只要目录内存在
``captions.json`` 即按其内容取 caption 并忽略同名 ``.txt``, 无需开启
``online_captions``(该开关会改用图片绝对路径取 caption, 与 basename 键不匹配).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable

import httpx
from jinja2 import Environment, Template

from services.analysis.wd14 import DEFAULT_THRESHOLD, Wd14Tag, ensure_meta, load_meta
from services.core.atomic import atomic_write_text
from services.core.scanner import collect_images
from services.dataset.layout import CAPTIONS_JSON_FILE
from services.models import CaptionImageItem, CaptionResult, CaptionRule, JobSnapshot
from services.tagging.clean import make_tag_matcher, tag_key

logger = logging.getLogger(__name__)

# 描述标注服务列表经 ``.env`` 的 CAPTION_OLLAMA_ENDPOINTS 配置: 逗号分隔的
# ``地址|模型`` 对, 地址须含协议与端口(如 http://10.0.0.1:11434); 多个服务时
# 图片在服务间并行分发, 每个服务同一时刻只处理一张
ENDPOINTS_RAW = os.environ.get("CAPTION_OLLAMA_ENDPOINTS", "").strip()

# 上下文长度与单次生成的最大 token 数(CAPTION_NUM_CTX / CAPTION_NUM_PREDICT):
# 0 表示不向服务传该项, 由服务端默认值决定
NUM_CTX = int(os.environ.get("CAPTION_NUM_CTX", "").strip() or 0)
NUM_PREDICT = int(os.environ.get("CAPTION_NUM_PREDICT", "").strip() or 8192)
# 单张生成超时(秒, CAPTION_TIMEOUT)
TIMEOUT_SEC = float(os.environ.get("CAPTION_TIMEOUT", "").strip() or 60)
# 单张图片生成失败后的最大重试次数
MAX_RETRIES = 3
# 探测服务可用性的请求超时(秒)
PROBE_TIMEOUT_SEC = 10.0

# 提示词模板环境: 模块内内嵌模板的解析与渲染入口
_JINJA_ENV = Environment()

# 各描述模式的系统提示词模板(内容由用户给定)
SYSTEM_PROMPT_TEMPLATES: dict[str, Template] = {
    "coarse": _JINJA_ENV.from_string(
        "You are currently performing text annotation for an image dataset. "
        "You must confidently and concisely describe the focal point of the image using dense English. "
        "Additionally, you should add and filter content based on the user's provided remarks."
    ),
    "detailed": _JINJA_ENV.from_string(
        "You are currently performing text annotation for an image dataset. "
        "You must confidently and thoroughly describe the image content using dense English formatting, "
        "detailing the attributes, relationships, and spatial arrangement of all entities. "
        "Additionally, you should add and filter content based on the user's provided remarks."
    ),
}

# 用户消息模板: 逐行拼接命中的预设提示词与 WD14 参考标签(按标签类型分行并带置信度);
# 不添加分节标题与列表符号, 行间以单个换行分隔
_USER_MESSAGE_TEMPLATE = _JINJA_ENV.from_string(
    """{%- set ns = namespace(lines=instructions) -%}
{%- for category, items in reference -%}
{%- set label = '来自 WD14 的 Danbooru 通用标签可供参考' if category == 'general' else '来自 WD14 的 Danbooru 角色标签可供参考' -%}
{%- set tags = namespace(text=[]) -%}
{%- for tag in items -%}
{%- set tags.text = tags.text + ['"' ~ tag.name ~ '":' ~ ('%.2f'|format(tag.score))] -%}
{%- endfor -%}
{%- set ns.lines = ns.lines + [label ~ ': ' ~ (tags.text | join(', '))] -%}
{%- endfor -%}
{{ ns.lines | join('\\n') }}"""
)


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


def _needs_wd14(
    rules: list[CaptionRule],
    general_threshold: float | None,
    character_threshold: float | None,
) -> bool:
    """判断本次描述是否需要 WD14 标签.

    两类用途都取自 ``.wd14_meta`` 缓存: 参考标签注入(通用或角色任一启用),
    以及带「匹配标签」的预设提示词(其命中判定基于该图片的缓存标签).

    Args:
        rules (list[CaptionRule]): 预设提示词条目.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.

    Returns:
        bool: 需要 WD14 标签时为 True.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    if general_threshold is not None or character_threshold is not None:
        return True
    return any(rule.pattern.strip() for rule in rules)


def parse_endpoints(raw: str) -> tuple[list[tuple[str, str]], str]:
    """解析 ``地址|模型`` 服务列表.

    Args:
        raw (str): 逗号分隔的 ``地址|模型`` 对.

    Returns:
        tuple[list[tuple[str, str]], str]: ``([(地址, 模型), ...], 错误说明)``;
        解析成功时错误说明为空串, 失败时服务列表为空.

    Callers:
        - ``services/analysis/caption.py:ollama_status``
        - ``services/analysis/caption.py:run_caption``
    """
    if not raw:
        return [], "未配置 CAPTION_OLLAMA_ENDPOINTS"
    endpoints: list[tuple[str, str]] = []
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        host, sep, model = entry.rpartition("|")
        host, model = host.strip(), model.strip()
        if not sep or not host or not model:
            return [], f"服务项格式应为 地址|模型: {entry!r}"
        endpoints.append((host, model))
    if not endpoints:
        return [], "未配置 CAPTION_OLLAMA_ENDPOINTS"
    return endpoints, ""


def probe_endpoint(host: str, model: str) -> tuple[bool, str]:
    """探测单个服务的连通性与目标模型是否已安装.

    Args:
        host (str): 服务地址, 含协议与端口.
        model (str): 视觉模型名.

    Returns:
        tuple[bool, str]: ``(ok, message)``; ok 为 True 时 message 为 ``地址 · 模型``,
        否则为失败原因.

    Callers:
        - ``services/analysis/caption.py:ollama_status``
    """
    try:
        with httpx.Client(trust_env=False, timeout=PROBE_TIMEOUT_SEC) as client:
            resp = client.get(f"{host.rstrip('/')}/api/tags")
            resp.raise_for_status()
            names = [str(m.get("name", "")) for m in resp.json().get("models", [])]
    except httpx.HTTPError as e:
        return False, f"无法连接 {host}: {e}"
    if model not in names:
        return False, f"{host} 未安装模型 {model}"
    return True, f"{host} · {model}"


def ollama_status() -> tuple[bool, str]:
    """探测全部描述服务的配置与就绪状态.

    全部服务均连通且已安装目标模型时视为就绪, 说明文本列出每个服务; 任一服务不可用时
    描述页不可提交任务, 避免请求被分发到不可用的服务上.

    Returns:
        tuple[bool, str]: ``(是否就绪, 说明文本)``.

    Callers:
        - ``routes/caption.py:dirs``
        - ``routes/caption.py:start``
    """
    endpoints, error = parse_endpoints(ENDPOINTS_RAW)
    if error:
        return False, error
    parts: list[str] = []
    failed = 0
    for host, model in endpoints:
        ready, info = probe_endpoint(host, model)
        if not ready:
            failed += 1
        parts.append(("✓ " if ready else "✗ ") + info)
    detail = "; ".join(parts)
    if failed:
        logger.warning("描述服务未全部就绪: %s", detail)
        return False, f"{failed}/{len(endpoints)} 个服务不可用: {detail}"
    return True, f"{len(endpoints)} 个服务就绪: {detail}"


def _reference_tags(
    tags: list[Wd14Tag] | None,
    general_threshold: float | None,
    character_threshold: float | None,
) -> list[tuple[str, list[Wd14Tag]]]:
    """按各类别的置信度下限挑选参考标签, 并按标签类型分组.

    通用标签与角色标签各自独立: 下限为 None 表示该类别不注入, 否则只保留置信度不低于
    该下限的标签.

    Args:
        tags (list[Wd14Tag] | None): WD14 缓存标签; 未打标时为 None.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.

    Returns:
        list[tuple[str, list[Wd14Tag]]]: ``(标签类型, 标签列表)``, 通用标签在前、
        角色标签在后(各自保持置信度降序); 未注入或该类型无达标标签时不含对应项.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    if not tags:
        return []
    groups: list[tuple[str, list[Wd14Tag]]] = []
    for category, threshold in (
        ("general", general_threshold),
        ("character", character_threshold),
    ):
        if threshold is None:
            continue
        items = [tag for tag in tags if tag.category == category and tag.score >= threshold]
        if items:
            groups.append((category, items))
    return groups


def build_user_prompt(
    *,
    reference: list[tuple[str, list[Wd14Tag]]],
    instructions: list[str],
) -> str:
    """组装单张图片的用户提示词.

    按「预设提示词 → WD14 参考标签」顺序逐行拼接: 预设提示词文本原样成行,
    参考标签每类一行并带置信度; 不添加任何分节标题或列表符号.

    Args:
        reference (list[tuple[str, list[Wd14Tag]]]): WD14 参考标签, 按标签类型分组.
        instructions (list[str]): 命中的预设提示词文本.

    Returns:
        str: 逐行拼接的提示词全文.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    return _USER_MESSAGE_TEMPLATE.render(
        reference=reference,
        instructions=instructions,
    )


def _clean_answer(raw: str) -> str:
    """把模型回答整理为单行描述文本.

    仅去除代码围栏与首尾空白, 并把换行折叠为空格; 描述内容本身原样保留.

    Args:
        raw (str): 模型回答全文.

    Returns:
        str: 折叠为单行的描述文本.

    Raises:
        ValueError: 回答为空(去除围栏与空白后无内容).

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    caption = " ".join(text.split())
    if not caption:
        raise ValueError("模型回答为空")
    return caption


class RepeatedOutputError(RuntimeError):
    """模型流式输出出现周期性重复时的异常."""


class _LoopDetector:
    """检测流式输出中的周期重复, 用于尽早中断模型的循环生成.

    在固定长度的滚动窗口上求最小周期(前缀函数), 最小周期重复达到 ``min_repeats``
    次即判定为循环, 避免模型在同一段文字上无限重复直至超时.

    Args:
        size (int): 滚动窗口字符数.
        min_repeats (int): 判定为循环所需的最小重复次数.
    """

    def __init__(self, size: int = 200, min_repeats: int = 4) -> None:
        self.size = size
        self.min_repeats = min_repeats
        self.buf = ""
        self.period_len: int | None = None

    def feed(self, text: str) -> bool:
        """追加新片段并判断是否出现循环.

        Args:
            text (str): 本次新增的输出片段.

        Returns:
            bool: 窗口内最小周期重复达标为 True.

        Callers:
            - ``services/analysis/caption.py:_chat_caption``
        """
        self.buf = (self.buf + text)[-self.size :]
        self.period_len = None
        if len(self.buf) < self.size:
            return False
        s = self.buf
        pi = [0] * len(s)
        for i in range(1, len(s)):
            j = pi[i - 1]
            while j and s[i] != s[j]:
                j = pi[j - 1]
            pi[i] = j + (s[i] == s[j])
        period = len(s) - pi[-1]
        if period < len(s) and period * self.min_repeats <= len(s):
            self.period_len = period
            return True
        return False


def build_chat_payload(
    model: str,
    *,
    image_b64: str,
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """组装 Ollama ``/api/chat`` 流式请求体.

    ``num_ctx`` / ``num_predict`` 仅在配置为正数时写入 ``options``, 否则由服务端默认值决定.

    Args:
        model (str): 目标视觉模型名.
        image_b64 (str): 图片的 base64 编码.
        system_prompt (str): 系统提示词.
        user_prompt (str): 用户提示词.

    Returns:
        dict: 可直接作为 ``json`` 请求体的字典.

    Callers:
        - ``services/analysis/caption.py:_chat_caption``
        - ``web_annotator/_bench_ollama_concurrency.py:probe_once``
    """
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt, "images": [image_b64]},
        ],
        "stream": True,
    }
    options: dict[str, int] = {}
    if NUM_CTX > 0:
        options["num_ctx"] = NUM_CTX
    if NUM_PREDICT > 0:
        options["num_predict"] = NUM_PREDICT
    if options:
        payload["options"] = options
    return payload


async def _chat_caption(
    client: httpx.AsyncClient,
    host: str,
    model: str,
    *,
    image_b64: str,
    system_prompt: str,
    user_prompt: str,
    cancel_check: Callable[[], bool],
) -> str:
    """调用指定服务的 Ollama ``/api/chat`` 流式接口, 返回模型回答全文.

    ``num_ctx`` / ``num_predict`` 仅在配置为正数时传入, 否则由服务端默认值决定.
    显式关闭环境变量代理(``trust_env=False``), 避免 .env 中的 HTTP(S)_PROXY 使内网
    地址被代理拦截. 思考片段只参与循环检测, 不计入回答内容.

    Args:
        client (httpx.AsyncClient): 共用的事件循环内 HTTP 客户端.
        host (str): 目标服务地址, 含协议与端口.
        model (str): 目标视觉模型名.
        image_b64 (str): 图片的 base64 编码.
        system_prompt (str): 系统提示词.
        user_prompt (str): 用户提示词.
        cancel_check (Callable[[], bool]): 协作取消判定, 返回 True 时中止本次请求.

    Returns:
        str: 模型回答全文(不含思考过程).

    Raises:
        httpx.HTTPError: 请求失败或响应状态码非 2xx.
        RepeatedOutputError: 输出出现周期性重复.
        RuntimeError: 服务返回 error 字段, 或生成过程中被取消.

    Callers:
        - ``services/analysis/caption.py:_generate_captions``
    """
    payload = build_chat_payload(
        model,
        image_b64=image_b64,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    parts: list[str] = []
    loop_detector = _LoopDetector()
    url = f"{host.rstrip('/')}/api/chat"
    async with client.stream("POST", url, json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if cancel_check():
                raise RuntimeError("已取消")
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))
            msg = obj.get("message") or {}
            thinking = msg.get("thinking") or msg.get("reasoning") or obj.get("thinking") or ""
            if thinking and loop_detector.feed(thinking):
                raise RepeatedOutputError("检测到模型输出循环")
            chunk = msg.get("content") or ""
            if chunk:
                parts.append(chunk)
                if loop_detector.feed(chunk):
                    raise RepeatedOutputError("检测到模型输出循环")
            if obj.get("done"):
                break
    return "".join(parts)


def load_captions(directory: Path) -> dict[str, list[str]]:
    """读取工作目录的 ``captions.json``.

    Args:
        directory (Path): 工作目录绝对路径.

    Returns:
        dict[str, list[str]]: 文件名的描述列表; 文件不存在时为空字典.

    Callers:
        - ``services/analysis/caption.py:run_caption``
        - ``routes/caption.py:_build_item``
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
    """
    ordered = {key: data[key] for key in sorted(data)}
    atomic_write_text(
        directory / CAPTIONS_JSON_FILE,
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
    )


@dataclass
class CaptionProgress:
    """描述标注进度对象.

    Callers:
        - ``routes/caption.py:start`` : 构造后交给任务.
    """

    status: str = "idle"  # idle|running|done|error|cancelled
    phase: str = "idle"  # idle|running|done
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    current: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False

    def snapshot(self) -> JobSnapshot:
        """导出进度快照.

        Returns:
            JobSnapshot: 当前进度的快照对象, 供长任务 SSE 推送.

        Callers:
            - ``routes/caption.py:start``
        """
        total = self.total
        done = self.done
        pct = round(100.0 * done / total, 1) if total > 0 else 0.0
        if self.status == "done" and total > 0:
            pct = 100.0
        return JobSnapshot(
            status=self.status,
            phase=self.phase,
            total=total,
            done=done,
            pct=pct,
            skipped=self.skipped,
            ok=self.ok,
            failed=self.failed,
            current=self.current,
            message=self.message,
            logs=list(self.logs[-80:]),
            cancel_requested=self.cancel_requested,
        )


async def _generate_captions(
    client: httpx.AsyncClient,
    src: Path,
    *,
    root: Path,
    host: str,
    model: str,
    modes: list[str],
    general_threshold: float | None,
    character_threshold: float | None,
    rules: list[CaptionRule],
    cancel_check: Callable[[], bool],
) -> list[str]:
    """生成单张图片的描述列表(按 ``modes`` 顺序各请求一次).

    Args:
        client (httpx.AsyncClient): 共用的事件循环内 HTTP 客户端.
        src (Path): 图片绝对路径.
        root (Path): 数据集根目录(定位 ``.wd14_meta`` 缓存).
        host (str): 服务地址, 含协议与端口.
        model (str): 视觉模型名.
        modes (list[str]): 描述模式列表, 取值属于 ``SYSTEM_PROMPT_TEMPLATES``.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.
        rules (list[CaptionRule]): 预设提示词条目.
        cancel_check (Callable[[], bool]): 协作取消判定.

    Returns:
        list[str]: 各模式的描述文本.

    Raises:
        httpx.HTTPError: 请求失败或状态码非 2xx.
        OSError: 图片读取失败.
        ValueError: 模型回答为空, 或提示词条目缺触发方式.
        RuntimeError: 服务返回错误, 输出循环, 或生成过程中被取消.

    Callers:
        - ``services/analysis/caption.py:run_caption`` (作为协程交给 worker)
    """
    rel = src.relative_to(root).as_posix()
    wd_tags = load_meta(root, rel)
    prompt = build_user_prompt(
        reference=_reference_tags(wd_tags, general_threshold, character_threshold),
        instructions=_rule_texts(rules, {tag_key(tag.name) for tag in wd_tags or []}),
    )
    image_b64 = base64.b64encode(src.read_bytes()).decode("ascii")
    captions: list[str] = []
    for mode in modes:
        raw = await _chat_caption(
            client,
            host,
            model,
            image_b64=image_b64,
            system_prompt=SYSTEM_PROMPT_TEMPLATES[mode].render(),
            user_prompt=prompt,
            cancel_check=cancel_check,
        )
        captions.append(_clean_answer(raw))
    return captions


@dataclass
class _CaptionJob:
    """单次描述任务的共享状态, 由各服务协程共同读写.

    Args:
        root (Path): 数据集根目录.
        modes (list[str]): 描述模式列表.
        general_threshold (float | None): 通用标签注入下限; None 为不注入.
        character_threshold (float | None): 角色标签注入下限; None 为不注入.
        rules (list[CaptionRule]): 预设提示词条目.
        stores (dict[Path, dict[str, list[str]]]): 各工作目录的描述映射.
        progress (CaptionProgress): 进度对象.
        results (list[CaptionImageItem]): 本次成功写入的图片.
        on_update (Callable[[], None] | None): 进度变更回调.
    """

    root: Path
    modes: list[str]
    general_threshold: float | None
    character_threshold: float | None
    rules: list[CaptionRule]
    stores: dict[Path, dict[str, list[str]]]
    progress: CaptionProgress
    results: list[CaptionImageItem]
    on_update: Callable[[], None] | None


def _log(progress: CaptionProgress, line: str) -> None:
    """追加一条进度日志并输出到服务端日志.

    Args:
        progress (CaptionProgress): 进度对象.
        line (str): 日志行文本.

    Returns:
        None

    Callers:
        - ``services/analysis/caption.py:_serve``
        - ``services/analysis/caption.py:run_caption``
    """
    progress.logs.append(line)
    logger.info("描述标注 %s", line)


async def _serve(
    client: httpx.AsyncClient,
    queue: deque[Path],
    host: str,
    model: str,
    job: _CaptionJob,
) -> None:
    """消费共享队列: 取图生成描述并写盘, 失败项重新入队至多 ``MAX_RETRIES`` 次.

    Args:
        client (httpx.AsyncClient): 共用的事件循环内 HTTP 客户端.
        queue (deque[Path]): 各服务共用的待处理图片队列.
        host (str): 服务地址, 含协议与端口.
        model (str): 视觉模型名.
        job (_CaptionJob): 本次任务的共享状态.

    Returns:
        None

    Callers:
        - ``services/analysis/caption.py:run_caption`` (作为协程交给 ``asyncio.gather``)
    """
    progress = job.progress
    attempts: dict[Path, int] = {}
    while queue and not progress.cancel_requested:
        src = queue.popleft()
        rel = src.relative_to(job.root).as_posix()
        progress.current = rel
        progress.message = f"[{progress.done + 1}/{progress.total}] {rel}"
        try:
            captions = await _generate_captions(
                client,
                src,
                root=job.root,
                host=host,
                model=model,
                modes=job.modes,
                general_threshold=job.general_threshold,
                character_threshold=job.character_threshold,
                rules=job.rules,
                cancel_check=lambda: progress.cancel_requested,
            )
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as e:
            if progress.cancel_requested:
                return
            attempts[src] = attempts.get(src, 0) + 1
            if attempts[src] > MAX_RETRIES:
                progress.done += 1
                progress.failed += 1
                _log(progress, f"[{progress.done}/{progress.total}] 失败: {rel} — {e}")
            else:
                queue.append(src)
                _log(progress, f"失败, 重新排队重试 {attempts[src]}/{MAX_RETRIES}: {rel} — {e}")
        else:
            store = job.stores[src.parent]
            store[src.name] = captions
            write_captions(src.parent, store)
            progress.done += 1
            progress.ok += 1
            _log(progress, f"[{progress.done}/{progress.total}] 完成: {rel}")
            for mode, caption in zip(job.modes, captions):
                _log(progress, f"  {mode}: {caption}")
            job.results.append(
                CaptionImageItem(
                    path=rel,
                    stem=src.stem,
                    dir=src.parent.relative_to(job.root).as_posix(),
                    captions=captions,
                )
            )
        if job.on_update:
            job.on_update()


async def run_caption(
    root: Path,
    dirs: list[Path],
    *,
    modes: list[str],
    general_threshold: float | None,
    character_threshold: float | None,
    rules: list[CaptionRule],
    force: bool,
    progress: CaptionProgress,
    on_update: Callable[[], None] | None = None,
) -> CaptionResult:
    """批量描述: 每个服务一个协程, 共用一条队列轮转取图, 结果写入 ``captions.json``.

    每张图片按 ``modes`` 顺序各请求一次(系统提示词随模式切换). 已存在描述的图片在
    ``force`` 为假时跳过; 单张失败重新入队, 至多 ``MAX_RETRIES`` 次, 仍失败才记入
    失败并继续. 取消后不再取新图, 在途请求在下一个流式数据块处中断.

    参考标签注入与「匹配标签」命中判定都取自 WD14 缓存, 故生成前先对缺失缓存的
    待处理图片补建 ``.wd14_meta``(见 ``_needs_wd14`` 与 ``ensure_meta``); 推理阈值
    取 WD14 默认与本次注入下限中更低者.

    Args:
        root (Path): 数据集根目录.
        dirs (list[Path]): 待描述的工作目录列表.
        modes (list[str]): 描述模式列表, 取值属于 ``SYSTEM_PROMPT_TEMPLATES``.
        general_threshold (float | None): 通用标签置信度下限; None 为不注入.
        character_threshold (float | None): 角色标签置信度下限; None 为不注入.
        rules (list[CaptionRule]): 预设提示词条目.
        force (bool): 是否覆盖 captions.json 中已存在的条目.
        progress (CaptionProgress): 进度对象.
        on_update (Callable[[], None] | None): 进度变更回调.

    Returns:
        CaptionResult: 含本次成功写入的图片与其描述、成功/失败/跳过计数.

    Raises:
        ValueError: 描述服务配置缺失或格式错误.

    Callers:
        - ``routes/caption.py:start``
    """
    items = collect_images(dirs)
    endpoints, error = parse_endpoints(ENDPOINTS_RAW)
    if error:
        raise ValueError(error)
    dir_names = [d.name for d in dirs]
    progress.total = len(items)
    progress.done = progress.ok = progress.failed = progress.skipped = 0
    progress.status = progress.phase = "running"
    progress.message = f"共 {len(items)} 张, {len(endpoints)} 个描述服务"
    _log(
        progress,
        f"开始: dirs={dir_names}, modes={modes}, general_threshold={general_threshold}, "
        f"character_threshold={character_threshold}, rules={len(rules)}, "
        f"force={force}, total={len(items)}, "
        f"服务={[f'{host} · {model}' for host, model in endpoints]}",
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

    stores = {work: load_captions(work) for work in dirs}
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

    if _needs_wd14(rules, general_threshold, character_threshold):
        enabled = [t for t in (general_threshold, character_threshold) if t is not None]
        # WD14 推理是 CPU 密集操作, 交给线程执行以免阻塞事件循环; 其内部逐张检查取消
        await asyncio.to_thread(
            ensure_meta,
            root,
            pending,
            threshold=min([DEFAULT_THRESHOLD, *enabled]),
            log=partial(_log, progress),
            cancel_check=lambda: progress.cancel_requested,
        )
        if on_update:
            on_update()

    results: list[CaptionImageItem] = []
    job = _CaptionJob(
        root=root,
        modes=modes,
        general_threshold=general_threshold,
        character_threshold=character_threshold,
        rules=rules,
        stores=stores,
        progress=progress,
        results=results,
        on_update=on_update,
    )
    if pending:
        async with httpx.AsyncClient(trust_env=False, timeout=TIMEOUT_SEC) as client:
            queue = deque(pending)
            await asyncio.gather(*(_serve(client, queue, host, model, job) for host, model in endpoints))

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

