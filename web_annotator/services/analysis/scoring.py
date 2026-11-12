"""统一评分引擎: 美学(CLIP)与 WD14 标签概率的数据驱动评分. """

from __future__ import annotations

import ast
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from PIL import Image, UnidentifiedImageError
from torch import nn

from services.analysis import expression
from services.analysis.wd14 import Tags, Wd14Cache, Wd14Labels
from services.core.atomic import atomic_write_text
from services.core.device import register_model
from services.core.jobs import JobProgress
from services.dataset.workdir import WorkDir
from services.models.pages import ScoreItem

if TYPE_CHECKING:
    from services.dataset.runtime import Dataset

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

AESTHETIC_SCORES = ".aesthetic_scores.json"
SCORE_ITEMS_FILE = ".annotator_scoring.json"
SCORE_RESULTS = ".score_results.json"

# 评分项按单像素掩码求值, 复用掩码表达式引擎的全部运算符与函数
_SCALAR_DEFAULT = np.zeros((1, 1), dtype=np.float64)

# 内置评分项: 概率取自 WD14 标签表中同名标签, 不可删除
BUILTIN_SCORE_ITEMS: tuple[ScoreItem, ...] = (
    ScoreItem(key="csam", label="CSAM", expr='max("loli", "shota")', builtin=True),
    ScoreItem(
        key="nsfw",
        label="NSFW",
        expr='max("sensitive" * 0.33, "questionable" * 0.66, "explicit")',
        builtin=True,
    ),
)

# 美学模型 CLIP 骨干: 经 ``AESTHETIC_CLIP_MODEL`` 环境变量手动配置, 取 OpenAI CLIP
# 模型名或本地权重路径.
AESTHETIC_CLIP_MODEL = os.environ.get("AESTHETIC_CLIP_MODEL", "") or "ViT-L/14"
# 美学头权重 ckpt 文件: 经 ``AESTHETIC_HEAD_CKPT`` 环境变量手动配置; 留空时从
# HuggingFace 下载默认权重.
AESTHETIC_HEAD_CKPT = (
    Path(os.environ["AESTHETIC_HEAD_CKPT"]) if os.environ.get("AESTHETIC_HEAD_CKPT") else None
)

# ---------------------------------------------------------------------------
# 分数 IO
# ---------------------------------------------------------------------------


def _rel_key(value: object) -> str:
    """把分数文件的键规范为数据集根相对路径.

    Args:
        value (object): 文件中的原始键.

    Returns:
        str: 正斜杠分隔、去除前导斜杠的相对路径.

    Callers:
        - ``services/analysis/scoring.py:load_score_json``
        - ``services/analysis/scoring.py:save_score_json``
        - ``services/analysis/scoring.py:load_item_scores``
        - ``services/analysis/scoring.py:save_item_scores``
    """
    return str(value).replace("\\", "/").lstrip("/")


def _read_json_object(path: Path) -> dict | None:
    """读取 JSON 文件根对象.

    Args:
        path (Path): 文件路径.

    Returns:
        dict | None: 根对象; 文件不存在时为 None.

    Raises:
        TypeError: 根节点不是对象.

    Callers:
        - ``services/analysis/scoring.py:load_score_json``
        - ``services/analysis/scoring.py:load_item_scores``
        - ``services/analysis/scoring.py:load_score_items``
    """
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"JSON 根节点必须是对象: {path}")
    return data


def _write_json_object(path: Path, data: dict) -> None:
    """写入 JSON 对象, 两位缩进、按键排序并原子替换.

    Args:
        path (Path): 目标文件路径.
        data (dict): 待写入对象.

    Callers:
        - ``services/analysis/scoring.py:save_score_json``
        - ``services/analysis/scoring.py:save_item_scores``
        - ``services/analysis/scoring.py:save_score_items``
    """
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def load_score_json(root: Path, filename: str) -> dict[str, float]:
    """读取数据集根目录下的分数 JSON 文件. 

    返回 ``{相对路径: 分数}`` 的映射字典；文件不存在时返回空字典. 
    自动规范化路径分隔符. 

    Args:
        root: 数据集根目录. 
        filename: JSON 文件名(如 ``.aesthetic_scores.json``). 

    Returns:
        dict[str, float]: 相对路径到分数的映射. 

    Raises:
        TypeError: JSON 根节点不是对象时抛出. 

    Callers:
        - ``routes/scoring.py:`` : 读取美学分数. 
        - ``routes/images.py:`` : 加载美学分数填充图片列表. 
        - ``services/analysis/scoring.py:_run_aesthetic_scoring`` : 读取已有分数跳过重复计算. 
    """
    data = _read_json_object(root.resolve() / filename)
    if data is None:
        return {}
    return {_rel_key(k): float(v) for k, v in data.items()}


def save_score_json(root: Path, filename: str, scores: dict[str, float]) -> None:
    """将分数写入数据集根目录下的 JSON 文件. 

    自动规范化路径分隔符, 分数保留 6 位小数, 按 key 排序输出. 

    Args:
        root: 数据集根目录. 
        filename: JSON 文件名. 
        scores: 相对路径到分数的映射. 

    Callers:
        - ``services/analysis/scoring.py:_run_aesthetic_scoring`` : 评分过程中定期持久化. 
    """
    clean = {_rel_key(k): round(float(v), 6) for k, v in scores.items()}
    _write_json_object(root.resolve() / filename, clean)


def load_item_scores(root: Path) -> dict[str, dict[str, float]]:
    """读取各评分项的结果缓存.

    Args:
        root: 数据集根目录.

    Returns:
        dict[str, dict[str, float]]: 评分项 key → ``{相对路径: 分数}``; 文件不存在时为
        空映射.

    Raises:
        TypeError: 文件结构不是两层对象时抛出.

    Callers:
        - ``routes/scoring.py:dirs`` : 统计各评分项已算出的数量.
        - ``services/analysis/scoring.py:build_score_results`` : 读取指定评分项结果.
        - ``services/analysis/scoring.py:delete_score_item`` : 清除被删评分项的结果.
        - ``routes/images.py:list_images`` : 填充图片列表的评分项分值.
    """
    path = root.resolve() / SCORE_RESULTS
    data = _read_json_object(path)
    if data is None:
        return {}
    result: dict[str, dict[str, float]] = {}
    for key, values in data.items():
        if not isinstance(values, dict):
            raise TypeError(f"评分结果 {key} 必须是对象: {path}")
        result[str(key)] = {_rel_key(p): float(v) for p, v in values.items()}
    return result


def save_item_scores(root: Path, scores: dict[str, dict[str, float]]) -> None:
    """写入各评分项的结果缓存.

    Args:
        root: 数据集根目录.
        scores: 评分项 key → ``{相对路径: 分数}``.

    Callers:
        - ``services/analysis/scoring.py:_run_wd14_scoring`` : 评分过程中定期持久化.
        - ``services/analysis/scoring.py:delete_score_item`` : 清除被删评分项的结果.
    """
    clean = {
        str(key): {_rel_key(p): round(float(v), 6) for p, v in values.items()}
        for key, values in scores.items()
    }
    _write_json_object(root.resolve() / SCORE_RESULTS, clean)


def image_inventory(dataset: Dataset) -> set[str]:
    """数据集当前工作目录内图片的相对路径集合.

    评分结果文件以磁盘图片清单为键空间: 工作目录内的图片才计入, 回收站与
    保留目录不计入. 读取结果与重建结果文件都以该集合为准.

    Args:
        dataset (Dataset): 数据集.

    Returns:
        set[str]: 相对数据集根的 POSIX 路径集合.

    Callers:
        - ``services/analysis/scoring.py:_run_aesthetic_scoring`` : 重建美学分表.
        - ``services/analysis/scoring.py:_run_wd14_scoring`` : 重建评分项结果表.
        - ``services/analysis/scoring.py:build_score_results`` : 按图片清单取值.
        - ``routes/scoring.py:dirs`` : 统计当前图片中已算出分值的数量.
    """
    return {
        image.rel
        for work in dataset.work_dirs()
        for image in work.images()
    }


# ---------------------------------------------------------------------------
# 评分项定义
# ---------------------------------------------------------------------------


def load_score_items(root: Path) -> list[ScoreItem]:
    """读取数据集的自定义评分项并与内置项合并.

    Args:
        root: 数据集根目录.

    Returns:
        list[ScoreItem]: 内置项在前、自定义项在后.

    Raises:
        TypeError: 定义文件结构非法.
        ValueError: 自定义 key 与内置或彼此重复.

    Callers:
        - ``routes/scoring.py:dirs``
        - ``routes/scoring.py:create_item``
        - ``routes/scoring.py:delete_item``
        - ``services/analysis/scoring.py:_run_wd14_scoring``
    """
    path = root.resolve() / SCORE_ITEMS_FILE
    data = _read_json_object(path)
    if data is None:
        return list(BUILTIN_SCORE_ITEMS)
    raw = data.get("items", [])
    if not isinstance(raw, list):
        raise TypeError(f"评分项 items 必须是数组: {path}")
    custom = [ScoreItem.model_validate({**item, "builtin": False}) for item in raw]
    keys = [item.key for item in custom]
    if len(set(keys)) != len(keys):
        raise ValueError(f"评分项 key 重复: {keys}")
    reserved = {item.key for item in BUILTIN_SCORE_ITEMS} & set(keys)
    if reserved:
        raise ValueError(f"评分项 key 与内置项冲突: {sorted(reserved)}")
    return [*BUILTIN_SCORE_ITEMS, *custom]


def save_score_items(root: Path, items: list[ScoreItem]) -> None:
    """把自定义评分项写入数据集定义文件.

    Args:
        root: 数据集根目录.
        items: 全部评分项; 内置项不写入.

    Callers:
        - ``services/analysis/scoring.py:create_score_item``
        - ``services/analysis/scoring.py:delete_score_item``
    """
    custom = [
        {"key": item.key, "label": item.label, "expr": item.expr}
        for item in items
        if not item.builtin
    ]
    _write_json_object(root.resolve() / SCORE_ITEMS_FILE, {"items": custom})


def create_score_item(
    root: Path, *, label: str, expr: str, label_index: dict[str, int],
) -> list[ScoreItem]:
    """校验表达式并追加一个自定义评分项.

    Args:
        root: 数据集根目录.
        label: 显示名称(去首尾空白后不能为空).
        expr: 评分表达式.
        label_index: WD14 标签等价键到概率下标的索引, 用于校验引用标签.

    Returns:
        list[ScoreItem]: 更新后的全部评分项.

    Raises:
        ValueError: 名称为空或表达式非法/引用标签不存在.

    Callers:
        - ``routes/scoring.py:create_item``
    """
    name = label.strip()
    if not name:
        raise ValueError("评分项名称不能为空")
    source = expr.strip()
    score_item_tree(source, label_index)
    items = load_score_items(root)
    key = _new_item_key({item.key for item in items})
    items.append(ScoreItem(key=key, label=name, expr=source))
    save_score_items(root, items)
    return items


def delete_score_item(root: Path, key: str) -> list[ScoreItem]:
    """删除一个自定义评分项及其已缓存的分数.

    Args:
        root: 数据集根目录.
        key: 评分项 key.

    Returns:
        list[ScoreItem]: 更新后的全部评分项.

    Raises:
        KeyError: 评分项不存在.
        ValueError: 目标为内置评分项.

    Callers:
        - ``routes/scoring.py:delete_item``
    """
    items = load_score_items(root)
    target = next((item for item in items if item.key == key), None)
    if target is None:
        raise KeyError(key)
    if target.builtin:
        raise ValueError("内置评分项不可删除")
    save_score_items(root, [item for item in items if item.key != key])
    scores = load_item_scores(root)
    if scores.pop(key, None) is not None:
        save_item_scores(root, scores)
    return load_score_items(root)


def _new_item_key(existing: set[str]) -> str:
    """生成不与现有评分项冲突的 key.

    Args:
        existing: 现有评分项 key 集合.

    Returns:
        str: ``item_`` 前缀的 8 位十六进制 key.

    Callers:
        - ``services/analysis/scoring.py:create_score_item``
    """
    while True:
        key = f"item_{uuid.uuid4().hex[:8]}"
        if key not in existing:
            return key


# ---------------------------------------------------------------------------
# 表达式 (复用 services/analysis/expression.py 的语法与求值引擎)
# ---------------------------------------------------------------------------


def score_item_tree(source: str, label_index: dict[str, int]) -> ast.AST:
    """解析评分表达式并校验引用标签存在于 WD14 标签表.

    语法与蒙板表达式一致: 双引号字符串取同名 WD14 标签概率; 支持算术、比较与
    ``min`` / ``max`` / ``sum`` / ``avg`` / ``abs`` / ``sqrt`` / ``exp`` /
    ``log`` / ``pow`` / ``clamp`` / ``blur`` 函数.

    Args:
        source: 表达式文本.
        label_index: WD14 标签等价键到概率下标的索引.

    Returns:
        ast.AST: 已解析并静态校验的表达式语法树.

    Raises:
        ValueError: 表达式为空、语法错误或引用标签不存在.

    Callers:
        - ``services/analysis/scoring.py:create_score_item``
        - ``services/analysis/scoring.py:_run_wd14_scoring``
    """
    tree = expression.parse(source)
    missing = [
        tag for tag in expression.extract_concepts(tree)
        if Tags.equivalence_key(tag) not in label_index
    ]
    if missing:
        raise ValueError(f"WD14 标签表中不存在: {', '.join(missing)}")
    return tree


def evaluate_score(
    tree: ast.AST,
    buffers: dict[str, np.ndarray],
    label_index: dict[str, int],
    probabilities: np.ndarray,
) -> float:
    """按一张图的 WD14 概率向量对评分表达式求值.

    评分表达式复用蒙板表达式引擎: 每个标签概率写入单像素数组, 按 0-1 域参与
    运算, 求值结果即该评分项分值.

    Args:
        tree: ``score_item_tree`` 返回的语法树.
        buffers: 概念名 → 单像素数组, 就地写入概率.
        label_index: WD14 标签等价键到概率下标的索引.
        probabilities: 与该标签表对齐的概率向量.

    Returns:
        float: 表达式求值结果.

    Callers:
        - ``services/analysis/scoring.py:_run_wd14_scoring``
    """
    for tag, buffer in buffers.items():
        buffer[0, 0] = probabilities[label_index[Tags.equivalence_key(tag)]]
    return float(
        expression.evaluate(
            tree, buffers, _SCALAR_DEFAULT, concept_scale=1.0,
        )[0, 0]
    )


# ---------------------------------------------------------------------------
# 美学模型(CLIP ViT-L/14 + purplesmartai aesthetic-classifier)
# ---------------------------------------------------------------------------


def _raw_to_bin(raw: float) -> int:
    """原始美学分 [0, 1] → 0~9 整数档位. 

    Args:
        raw: CLIP 美学头输出的原始分数. 

    Returns:
        int: 0~9 的整数档位. 

    Callers:
        - ``services/analysis/scoring.py:build_score_results``
    """
    return int(max(0.0, min(0.99, raw)) * 10)


def _load_aes_models() -> tuple:
    """加载 CLIP ViT-L/14 视觉编码器 + 美学 MLP 头 (CPU).

    Returns:
        tuple: ``(clip_model, preprocess_fn, aes_head)``.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 load 回调
    """
    import clip
    clip_model, preprocess = clip.load(AESTHETIC_CLIP_MODEL, device="cpu")
    clip_model.eval()
    if AESTHETIC_HEAD_CKPT is not None:
        ckpt_path = str(AESTHETIC_HEAD_CKPT)
    else:
        ckpt_path = hf_hub_download(repo_id="purplesmartai/aesthetic-classifier", filename="v2.ckpt")
    state_dict = torch.load(ckpt_path, map_location="cpu")["state_dict"]
    state_dict = {k.replace("model.", ""): v for k, v in state_dict.items()}
    aes_head = nn.Sequential(
        nn.Linear(768, 1024), nn.ReLU(), nn.Dropout(0.5),
        nn.Linear(1024, 512), nn.ReLU(), nn.Dropout(0.3),
        nn.Linear(512, 1),
    )
    aes_head.load_state_dict(state_dict)
    aes_head.eval()
    return clip_model, preprocess, aes_head


def _unload_aes_models(handle: object) -> None:
    """释放美学模型.

    Args:
        handle: ``_load_aes_models`` 返回的 ``(clip, preprocess, aes_head)`` 元组.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 unload 回调
    """
    clip_model, _pre, aes_head = cast("tuple", handle)
    del clip_model, aes_head


_AES_MODEL = register_model(_load_aes_models, _unload_aes_models)


@torch.no_grad()
def _aes_score_one(image: Image.Image, clip_model, preprocess, aes_head) -> float:
    """对单张图片执行 CLIP + 美学头评分.

    Args:
        image: PIL 图片对象.
        clip_model: CLIP ViT-L/14 模型.
        preprocess: CLIP 预处理函数.
        aes_head: 美学评分 MLP 头.

    Returns:
        float: 原始美学分数(约 0~1).

    Callers:
        - ``services/analysis/scoring.py:run_scoring``
    """
    device = next(clip_model.parameters()).device
    t = preprocess(image.convert("RGB")).unsqueeze(0).to(device)
    feat = clip_model.encode_image(t).cpu().numpy().astype("float32")
    norm = np.linalg.norm(feat, axis=1, keepdims=True)
    feat = feat / np.where(norm == 0, 1, norm)
    return float(aes_head(torch.tensor(feat, device=device)).item())


# ---------------------------------------------------------------------------
# 评分引擎
# ---------------------------------------------------------------------------


def _run_aesthetic_scoring(
    works: list[WorkDir], *,
    models: Any,
    progress: JobProgress,
    on_update: Callable[[], None] | None,
) -> None:
    """美学评分主循环: 文件收集 → 缓存跳过/新评计算 → 定期持久化.

    结果表以当前图片清单重建, 已删除图片的旧分值随本次写回被剔除.

    Args:
        works: 要评分的工作目录(自动去重).
        models: ``(clip, preprocess, aes_head)`` 模型句柄.
        progress: 进度追踪实例.
        on_update: 每次状态变更后的回调.

    Callers:
        - ``services/analysis/scoring.py:run_scoring``
    """
    root = works[0].dataset.root
    items = [p for work in works for p in work.image_files()]
    progress.total = len(items)
    progress.done = progress.ok = progress.skipped = progress.failed = 0
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张, 加载美学模型…"
    progress.log(f"开始: dirs={[w.name for w in works]}, total={len(items)}")
    if on_update: on_update()

    if not items:
        progress.status = progress.phase = "done"
        progress.message = "没有可处理的图片"
        if on_update: on_update()
        return

    progress.phase = "running"
    progress.message = f"共 {len(items)} 张"
    progress.log("美学模型就绪")
    if on_update: on_update()

    inventory = image_inventory(works[0].dataset)
    existing = load_score_json(root, AESTHETIC_SCORES)
    # 结果表以当前图片清单重建: 已删除图片的旧分值不参与写回
    scores: dict[str, float] = {
        rel: value for rel, value in existing.items() if rel in inventory
    }

    for src in items:
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 完成)"
            save_score_json(root, AESTHETIC_SCORES, scores)
            if on_update: on_update()
            return

        rel = src.relative_to(root).as_posix()
        progress.current = rel

        if rel in scores:
            progress.skipped += 1
            progress.ok += 1
            progress.done += 1
        else:
            try:
                scores[rel] = _aes_score_one(
                    Image.open(src), models[0], models[1], models[2],
                )
                progress.ok += 1
                progress.done += 1
            except (OSError, UnidentifiedImageError, RuntimeError, ValueError) as e:
                progress.failed += 1
                progress.done += 1
                progress.log(f"失败: {rel} — {e}")

        if progress.done % 10 == 0 or progress.done == len(items):
            save_score_json(root, AESTHETIC_SCORES, scores)
        if on_update: on_update()

    save_score_json(root, AESTHETIC_SCORES, scores)
    progress.status = progress.phase = "done"
    progress.current = ""
    progress.message = f"完成 {progress.ok} 张, 跳过 {progress.skipped}"
    if on_update: on_update()


def _run_wd14_scoring(
    works: list[WorkDir], *,
    progress: JobProgress,
    on_update: Callable[[], None] | None,
) -> None:
    """先补齐 WD14 推理缓存, 再按概率计算全部评分项.

    缺失缓存的图片在评分前统一推理写入 ``.wd14_meta``, 随后只读缓存向量求值;
    单张缓存生成失败的图片计入失败, 不中断整批. 结果表以当前评分项与当前图片
    清单重建, 本次未涉及的目录沿用旧分值.

    Args:
        works: 要评分的工作目录.
        progress: 进度追踪实例.
        on_update: 每次状态变更后的回调.

    Raises:
        RuntimeError: WD14 模型权重或标签表缺失.

    Callers:
        - ``services/analysis/scoring.py:run_scoring``
    """
    root = works[0].dataset.root
    items = [p for work in works for p in work.image_files()]
    labels = Wd14Labels.load()
    label_index = labels.index()
    score_items = load_score_items(root)
    trees = {item.key: score_item_tree(item.expr, label_index) for item in score_items}
    buffers = {
        key: {
            tag: np.zeros((1, 1), dtype=np.float64)
            for tag in expression.extract_concepts(tree)
        }
        for key, tree in trees.items()
    }
    cache = Wd14Cache(root, labels)
    pending = cache.missing(items)

    progress.total = len(pending) + len(items)
    progress.done = progress.ok = progress.skipped = progress.failed = 0
    progress.status = "running"
    progress.phase = "running"
    progress.message = (
        f"共 {len(items)} 张, 缺缓存 {len(pending)} 张, "
        f"计算 {len(trees)} 个评分项"
    )
    progress.log(
        f"开始: dirs={[w.name for w in works]}, items={list(trees)}, "
        f"total={len(items)}, pending={len(pending)}"
    )
    if on_update: on_update()

    if not items:
        progress.status = progress.phase = "done"
        progress.message = "没有可处理的图片"
        if on_update: on_update()
        return

    if pending:
        cache.populate(
            pending,
            log=progress.log,
            cancel_check=lambda: progress.cancel_requested,
            progress=progress,
            on_update=on_update,
            done_base=0,
        )
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.current = ""
            progress.message = "已取消 (缓存阶段中止)"
            if on_update: on_update()
            return

    inventory = image_inventory(works[0].dataset)
    existing = load_item_scores(root)
    # 结果表以当前评分项与当前图片清单重建: 本次未涉及的目录沿用旧值,
    # 已删除图片与已删除评分项的旧分值不参与写回
    scores: dict[str, dict[str, float]] = {
        key: {
            rel: value
            for rel, value in existing.get(key, {}).items()
            if rel in inventory
        }
        for key in trees
    }

    for i, src in enumerate(items):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 完成)"
            save_item_scores(root, scores)
            if on_update: on_update()
            return

        rel = cache.rel(src)
        progress.current = rel
        progress.message = f"评分 [{i + 1}/{len(items)}] {rel}"
        probabilities = cache.probabilities(src)
        if probabilities is None:
            progress.failed += 1
        else:
            for key, tree in trees.items():
                scores[key][rel] = evaluate_score(
                    tree, buffers[key], label_index, probabilities,
                )
            progress.ok += 1
        progress.done += 1

        if progress.done % 20 == 0 or progress.done == len(items):
            save_item_scores(root, scores)
        if on_update: on_update()

    save_item_scores(root, scores)
    progress.status = progress.phase = "done"
    progress.current = ""
    progress.message = f"完成 {progress.ok} 张"
    if progress.failed:
        progress.message += f", 缓存生成失败 {progress.failed} 张"
        progress.log("失败的图片可重新开始评分以补齐缓存")
    if on_update: on_update()


# ---------------------------------------------------------------------------
# 统一评分入口
# ---------------------------------------------------------------------------


def run_scoring(
    works: list[WorkDir], *,
    kind: str,
    progress: JobProgress | None = None,
    on_update: Callable[[], None] | None = None,
) -> None:
    """执行美学评分或 WD14 数据驱动评分.

    美学模型的句柄经上下文管理器获取并自动归还, 设备与生命周期由 ``device``
    模块统一管理; WD14 评分先补齐缺失的推理缓存, 再读取概率向量求值.

    Args:
        works: 要评分的工作目录.
        kind: 评分类型, ``"aesthetic"`` 或 ``"wd14"``(仅关键字).
        progress: 进度追踪实例(仅关键字, 为 None 时自动创建).
        on_update: 每次状态变更后的回调(仅关键字).

    Raises:
        ValueError: ``kind`` 不是 ``"aesthetic"`` 或 ``"wd14"`` 时抛出.

    Callers:
        - ``routes/scoring.py:start``
    """
    if kind not in ("aesthetic", "wd14"):
        raise ValueError(f"kind 必须是 'aesthetic' 或 'wd14', 收到: {kind!r}")
    if progress is None:
        progress = JobProgress()

    if kind == "aesthetic":
        with _AES_MODEL as models:
            _run_aesthetic_scoring(
                works, models=models, progress=progress, on_update=on_update,
            )
    else:
        _run_wd14_scoring(works, progress=progress, on_update=on_update)


# ---------------------------------------------------------------------------
# 标签查询: 以 WD14 标签概率对图片评分并排序
# ---------------------------------------------------------------------------


def tag_query_tree(query: str, label_index: dict[str, int]) -> ast.AST:
    """把逗号分隔的标签查询解析为评分表达式语法树.

    单标签取该标签的概率; 多标签取各概率的最小值, 即所有标签同时存在的置信度.

    Args:
        query (str): 逗号分隔的标签查询.
        label_index (dict[str, int]): WD14 标签等价键到概率下标的索引.

    Returns:
        ast.AST: 已解析并静态校验的表达式语法树.

    Raises:
        ValueError: 标签为空, 或引用的标签不在 WD14 标签表中.

    Callers:
        - ``services/analysis/scoring.py:run_tag_query``
        - ``routes/scoring.py:tag_query``
    """
    tags = [tag.strip() for tag in query.split(",") if tag.strip()]
    if not tags:
        raise ValueError("标签不能为空")
    literals = ", ".join(json.dumps(tag, ensure_ascii=False) for tag in tags)
    source = literals if len(tags) == 1 else f"min({literals})"
    return score_item_tree(source, label_index)


def score_tag_query(
    works: list[WorkDir], query: str,
) -> tuple[list[tuple[str, float]], int]:
    """用标签评分器对已有推理缓存的图片评分并降序返回.

    只读取 ``.wd14_meta`` 中已有的概率向量, 不触发生成推理缓存; 缺缓存的图片
    不参与排序, 由调用方提示先到评分页建立缓存. 查询标签的求值口径见
    ``tag_query_tree``.

    Args:
        works (list[WorkDir]): 要检索的工作目录.
        query (str): 逗号分隔的标签查询.

    Returns:
        tuple[list[tuple[str, float]], int]: ``([(相对路径, 分值), ...], 缺缓存张数)``,
        分值按降序排列.

    Raises:
        RuntimeError: WD14 模型权重或标签表缺失.
        ValueError: 查询标签为空或不在标签表中.

    Callers:
        - ``routes/scoring.py:tag_query``
    """
    root = works[0].dataset.root
    items = [p for work in works for p in work.image_files()]
    labels = Wd14Labels.load()
    label_index = labels.index()
    tree = tag_query_tree(query, label_index)
    buffers = {
        tag: np.zeros((1, 1), dtype=np.float64)
        for tag in expression.extract_concepts(tree)
    }
    cache = Wd14Cache(root, labels)
    hits: list[tuple[str, float]] = []
    missing = 0
    for src in items:
        probabilities = cache.probabilities(src)
        if probabilities is None:
            missing += 1
            continue
        hits.append((cache.rel(src), evaluate_score(tree, buffers, label_index, probabilities)))
    hits.sort(key=lambda item: item[1], reverse=True)
    return hits, missing


# ---------------------------------------------------------------------------
# 查询结果构建(供 API 返回 JSON)
# ---------------------------------------------------------------------------


def build_score_results(dataset: Dataset, kind: str) -> list[dict[str, Any]]:
    """按当前图片清单构建前端的评分结果列表.

    以磁盘图片清单为准逐张查表, 已不存在的图片不会出现在结果中.

    Args:
        dataset (Dataset): 数据集.
        kind: ``"aesthetic"`` 或评分项 key.

    Returns:
        list[dict]: 每条包含 ``path`` 与 ``score``; 美学结果另含档位字段. 按分数降序.

    Callers:
        - ``routes/scoring.py:results``
    """
    inventory = image_inventory(dataset)
    root = dataset.root
    if kind == "aesthetic":
        scores = load_score_json(root, AESTHETIC_SCORES)
        results: list[dict[str, Any]] = []
        for path in inventory:
            raw = scores.get(path)
            if raw is None:
                continue
            results.append({
                "path": path,
                "score": round(raw, 6),
                "pony": _raw_to_bin(raw),
                "pony_label": f"score_{_raw_to_bin(raw)}",
            })
        results.sort(key=lambda r: (-r["score"], r["path"]))
        return results

    scores = load_item_scores(root).get(kind, {})
    results = [
        {"path": path, "score": round(scores[path], 6)}
        for path in inventory
        if path in scores
    ]
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results
