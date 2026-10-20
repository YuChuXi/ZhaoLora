"""WD14 批量打标 (SmilingWolf 系列标签模型, ONNX 推理). """

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, cast

import numpy as np
from PIL import Image

from services.models import JobSnapshot
from services.core.atomic import atomic_write_bytes, atomic_write_text
from services.dataset.workdir import WD14_META_DIRNAME, WorkDir
from services.online_sources.framework.utils import _GENDER_TAGS
from services.core.device import register_model

if TYPE_CHECKING:
    from onnxruntime import InferenceSession

DEFAULT_THRESHOLD = 0.1

# WD14 打标模型权重: 经 ``WD14_MODEL`` 环境变量手动配置, 指向 ``.onnx`` 权重文件,
# 标签表取与其同名的 ``.csv`` 文件; 留空时使用仓库内默认模型.
_REPO_ROOT = Path(__file__).resolve().parents[3]
WD14_ONNX = Path(
    os.environ.get("WD14_MODEL", "")
    or _REPO_ROOT / "wd14_tagger_model" / "SmilingWolf_wd-vit-large-tagger-v3" / "model.onnx"
)
WD14_CSV = WD14_ONNX.with_suffix(".csv")

# 模型指纹缓存文件(任务根下, 与翻译缓存同级): 权重与标签表的 size/mtime 命中时
# 直接复用已算出的内容哈希, 避免每次进程启动重读整个权重文件
_DATA_ROOT = Path(
    os.environ.get("ANNOTATOR_DATA_DIR", "").strip() or _REPO_ROOT / "datasets"
)
_FINGERPRINT_CACHE = _DATA_ROOT / ".wd14_model.json"

# 标签表 category 取值到标签类型的映射; 未登记的取值在加载时直接报错
_CATEGORY_NAMES = {"0": "general", "4": "character", "9": "rating"}
# rating 分级类型名: 只表示图片分级, 不参与 txt 标签与描述参考注入
_RATING = "rating"


@dataclass(frozen=True)
class Wd14Tag:
    """WD14 推理得到的一个标签及其置信度.

    Attributes:
        name (str): 标签名(下划线已转空格).
        score (float): 置信度.

    Callers:
        - ``services/analysis/wd14.py:Wd14Labels.groups`` : 构造来源.
        - ``services/analysis/wd14.py:Wd14Cache.groups`` : 读取缓存的产物.
        - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence`` : 合并输入.
        - ``services/analysis/caption.py:_reference_tags`` : 注入提示词的参考候选.
    """

    name: str
    score: float


@dataclass(frozen=True)
class Wd14TagGroups:
    """WD14 标签按标签表 category 分组的结果.

    分组键取自标签表本身(``general`` / ``character`` / ``rating`` 等), 不预设
    固定字段; 下游按名称取用, 未出现的类别返回空元组.

    Attributes:
        by_category (dict[str, tuple[Wd14Tag, ...]]): 类别名到标签列表, 组内按置信度降序.

    Callers:
        - ``services/analysis/wd14.py:Wd14Labels.groups`` : 构造来源.
        - ``services/analysis/wd14.py:Wd14Cache.groups`` : 缓存取用产物.
        - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence`` : 合并非 rating 标签.
        - ``services/analysis/caption.py:_reference_tags`` : 按类别注入.
    """

    by_category: dict[str, tuple[Wd14Tag, ...]]

    def of(self, category: str) -> tuple[Wd14Tag, ...]:
        """返回指定类别的标签列表.

        Args:
            category (str): 标签类型名, 如 ``general`` / ``character`` / ``rating``.

        Returns:
            tuple[Wd14Tag, ...]: 该类别的标签, 按置信度降序; 无该类时为空的元组.

        Callers:
            - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence``
            - ``services/analysis/caption.py:_reference_tags``
        """
        return self.by_category.get(category, ())

    @property
    def tags(self) -> tuple[Wd14Tag, ...]:
        """返回全部非 rating 标签, 按置信度降序.

        Returns:
            tuple[Wd14Tag, ...]: 通用与角色等可写入 txt 的标签.

        Callers:
            - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence``
            - ``services/analysis/caption.py:_generate_captions``
        """
        return tuple(
            sorted(
                (
                    tag
                    for name, items in self.by_category.items()
                    if name != _RATING
                    for tag in items
                ),
                key=lambda tag: tag.score,
                reverse=True,
            )
        )


class Tags:
    """WD14 标签工具集, 提供拆分、拼接、等价键比较与置信度合并. 

    Callers:
        - ``services/analysis/wd14.py:Wd14Cache.refresh`` : 调用 ``split_caption``、``merge_for_disk_by_confidence``
    """

    @staticmethod
    def split_caption(caption: str) -> list[str]:
        """按逗号拆分 caption.

        Args:
            caption: 原始 caption 文本, 换行符视为逗号.

        Returns:
            list[str]: 去除空白后的非空标签列表.

        Callers:
            - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence``
            - ``services/analysis/wd14.py:Wd14Cache.refresh``
        """
        return [x.strip() for x in caption.replace("\n", ",").split(",") if x.strip()]

    @staticmethod
    def join_caption(tags: Iterable[str]) -> str:
        """用逗号空格拼接标签.

        Args:
            tags: 标签可迭代对象.

        Returns:
            str: 以 ``", "`` 拼接的非空标签字符串.

        Callers:
            - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence``
        """
        return ", ".join([t.strip() for t in tags if t.strip()])

    @staticmethod
    def equivalence_key(tag: str) -> str:
        """标签等价键(小写、空格化).

        Args:
            tag: 原始标签文本.

        Returns:
            str: 小写、下划线转空格、折叠空白后的等价键.

        Callers:
            - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence``
        """
        return " ".join(tag.strip().lower().replace("_", " ").split())

    @staticmethod
    def _canonical_spacing(tag: str) -> str:
        """规范化空格, 保留大小写.

        Args:
            tag: 原始标签文本.

        Returns:
            str: 下划线转空格、折叠空白后的规范标签, 保留原大小写.

        Callers:
            - ``services/analysis/wd14.py:Tags.merge_for_disk_by_confidence``
        """
        return " ".join(tag.strip().replace("_", " ").split())

    @staticmethod
    def merge_for_disk_by_confidence(
        base: list[str],
        groups: Wd14TagGroups,
        *,
        artist_tag: str = "",
    ) -> str:
        """按置信度合并原标签与 WD14 结果, 并按 Anima 格式输出.

        头部按 性别 → 角色 → 画师 收纳, 其余通用标签置于 ``|||`` 分隔的正文,
        与图源下载的 Anima 风格保持一致. 角色标签取自 ``character`` 组,
        画师标签由 ``artist_tag`` 显式提供(需带 ``@`` 前缀), 均按等价键去重.

        Args:
            base: 原旁路标签文本拆分列表.
            groups: WD14 分组标签; rating 组不写入.
            artist_tag: 画师标签, 需带 ``@`` 前缀; 为空则不写入.

        Returns:
            Anima 格式的标签文本.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.refresh``
        """
        score_by_key: dict[str, float] = {}
        canonical_by_key: dict[str, str] = {}
        for tag in groups.tags:
            key = Tags.equivalence_key(tag.name)
            if key not in score_by_key or tag.score > score_by_key[key]:
                score_by_key[key] = tag.score
                canonical_by_key[key] = Tags._canonical_spacing(tag.name)
        character_key = {
            Tags.equivalence_key(tag.name) for tag in groups.of("character")
        }
        for raw in base:
            key = Tags.equivalence_key(raw)
            if key not in canonical_by_key:
                canonical_by_key[key] = Tags._canonical_spacing(raw)
                score_by_key[key] = -1.0
        ordered = sorted(score_by_key.keys(), key=lambda k: score_by_key[k], reverse=True)

        gender_keys = {Tags.equivalence_key(g) for g in _GENDER_TAGS}
        header, body = [], []
        for key in ordered:
            tag = canonical_by_key[key]
            if key in character_key or Tags.equivalence_key(tag) in gender_keys:
                header.append(tag)
            else:
                body.append(tag)
        artist = artist_tag.strip()
        if artist:
            artist = Tags._canonical_spacing(artist)
            header_keys = {Tags.equivalence_key(t) for t in header}
            if Tags.equivalence_key(artist) not in header_keys:
                header.append(artist)
        caption = Tags.join_caption(header)
        if body:
            caption += " ||| " + Tags.join_caption(body)
        return caption


def _model_files() -> tuple[Path, Path]:
    """定位 ONNX 权重与同名标签 CSV 文件.

    Args:
        (无参数, 使用模块级常量 ``WD14_ONNX`` / ``WD14_CSV``.)

    Returns:
        tuple[Path, Path]: ``(onnx_path, csv_path)``.

    Raises:
        RuntimeError: 权重或标签文件不存在时抛出.

    Callers:
        - ``services/analysis/wd14.py:model_ready``
        - ``services/analysis/wd14.py:Wd14Labels.load``
        - ``services/analysis/wd14.py:_build_session``
    """
    if not WD14_ONNX.is_file():
        raise RuntimeError(f"WD14 模型权重不存在: {WD14_ONNX}")
    if not WD14_CSV.is_file():
        raise RuntimeError(f"WD14 标签表不存在: {WD14_CSV}")
    return WD14_ONNX, WD14_CSV


def _build_session(providers: list[str]) -> "InferenceSession":
    """按执行提供程序创建 WD14 ONNX 推理会话.

    Args:
        providers: ONNX 执行提供程序列表, 首位为首选设备.

    Returns:
        InferenceSession: 已初始化的 ONNX 推理会话.

    Callers:
        - ``services/analysis/wd14.py:Wd14Model.load``
        - ``services/analysis/wd14.py:Wd14Model.retarget``
    """
    import onnxruntime as ort

    onnx_path, _csv_path = _model_files()
    return ort.InferenceSession(str(onnx_path), providers=providers)


def _model_fingerprint(onnx_path: Path, csv_path: Path) -> str:
    """返回权重与标签表内容共同决定的模型指纹.

    指纹按两文件的 SHA-256 计算; 结果连同文件 size 与 mtime_ns 写入任务根的
    ``.wd14_model.json``, 两文件元信息未变时直接复用, 不重读权重内容.

    Args:
        onnx_path (Path): ONNX 权重文件.
        csv_path (Path): 标签表文件.

    Returns:
        str: 指纹前 16 位十六进制串.

    Callers:
        - ``services/analysis/wd14.py:Wd14Labels.load``
    """
    files = {
        "onnx": {
            "size": onnx_path.stat().st_size,
            "mtime_ns": onnx_path.stat().st_mtime_ns,
        },
        "csv": {
            "size": csv_path.stat().st_size,
            "mtime_ns": csv_path.stat().st_mtime_ns,
        },
    }
    if _FINGERPRINT_CACHE.is_file():
        cached = json.loads(_FINGERPRINT_CACHE.read_text(encoding="utf-8"))
        if cached.get("files") == files:
            return str(cached["fingerprint"])
    digest = hashlib.sha256()
    for path in (onnx_path, csv_path):
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8 << 20), b""):
                digest.update(chunk)
    fingerprint = digest.hexdigest()[:16]
    _FINGERPRINT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _FINGERPRINT_CACHE,
        json.dumps({"files": files, "fingerprint": fingerprint}, indent=2) + "\n",
    )
    return fingerprint


@dataclass(frozen=True)
class Wd14Labels:
    """WD14 标签表与模型身份.

    指纹由权重与标签表内容共同决定, 缓存目录按指纹区分, 使权重或标签表任一
    变化后的旧缓存不再命中. 标签表与输出向量逐行对齐, 与 ONNX 会话无关,
    读取缓存只需本对象, 无需加载权重.

    Attributes:
        fingerprint (str): 权重与标签表 SHA-256 的前 16 位十六进制串.
        names (tuple[str, ...]): 标签名, 下划线已转空格.
        categories (tuple[str, ...]): 标签类型名, 与 ``names`` 逐项对齐.

    Callers:
        - ``services/analysis/wd14.py:Wd14Model`` : 持有标签表用于推理后解码.
        - ``services/analysis/wd14.py:Wd14Cache`` : 定位缓存并解码向量.
    """

    fingerprint: str
    names: tuple[str, ...]
    categories: tuple[str, ...]

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> "Wd14Labels":
        """读取标签表并计算模型指纹.

        内容哈希与标签表解析在进程内只执行一次; 返回的实例不可变, 可安全共享.

        Returns:
            Wd14Labels: 解析后的标签表与指纹.

        Raises:
            RuntimeError: 权重或标签文件缺失.

        Callers:
            - ``services/analysis/wd14.py:Wd14Model.load``
            - ``services/analysis/caption.py:run_caption``
        """
        onnx_path, csv_path = _model_files()
        names: list[str] = []
        categories: list[str] = []
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                raw = row["category"]
                if raw not in _CATEGORY_NAMES:
                    raise RuntimeError(
                        f"WD14 标签表存在未登记的 category: {raw} ({row['name']})"
                    )
                names.append(row["name"].replace("_", " ").strip())
                categories.append(_CATEGORY_NAMES[raw])
        return cls(
            fingerprint=_model_fingerprint(onnx_path, csv_path),
            names=tuple(names),
            categories=tuple(categories),
        )

    def groups(self, scores: np.ndarray, threshold: float) -> Wd14TagGroups:
        """按置信度阈值把整条概率向量解码为分组标签.

        Args:
            scores (np.ndarray): 与本标签表对齐的概率向量.
            threshold (float): 置信度下限, 低于该值的标签丢弃.

        Returns:
            Wd14TagGroups: 按标签表 category 分组, 组内按置信度降序.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.groups``
            - ``services/analysis/wd14.py:Wd14Cache.refresh``
        """
        idx = np.flatnonzero(scores >= threshold)
        order = np.argsort(scores[idx])[::-1]
        buckets: dict[str, list[Wd14Tag]] = {}
        for i in idx[order]:
            buckets.setdefault(self.categories[int(i)], []).append(
                Wd14Tag(self.names[int(i)], float(scores[i]))
            )
        return Wd14TagGroups(
            by_category={name: tuple(items) for name, items in buckets.items()}
        )


class Wd14Model:
    """WD14 模型句柄: 标签表 + ONNX 推理会话.

    Callers:
        - ``services/core/device.py:register_model`` : 以本类为 WD14 模型的生命周期载体.
    """

    def __init__(self, labels: Wd14Labels, session: "InferenceSession") -> None:
        """初始化模型句柄.

        Args:
            labels (Wd14Labels): 标签表与模型身份.
            session (InferenceSession): ONNX 推理会话.

        Callers:
            - ``services/analysis/wd14.py:Wd14Model.load``
            - ``services/analysis/wd14.py:Wd14Model.retarget``
        """
        self.labels = labels
        self._session = session

    @classmethod
    def load(cls) -> "Wd14Model":
        """加载标签表与会话 (会话恒以 CPU 提供程序创建).

        转移目标设备由 ``device.Model`` 生命周期统一决定 (``retarget``),
        加载侧不做设备判断.

        Returns:
            Wd14Model: 模型句柄.

        Raises:
            RuntimeError: WD14 模型权重或标签表缺失.

        Callers:
            - ``services/core/device.py:register_model`` 注册的 load 回调
        """
        ok, msg = model_ready()
        if not ok:
            raise RuntimeError(msg)
        return cls(Wd14Labels.load(), _build_session(["CPUExecutionProvider"]))

    def retarget(self, device) -> "Wd14Model":
        """把推理会话转移到目标设备.

        ONNX 会话的执行提供程序创建后不可迁移, 故按目标设备重建会话;
        会话生效提供程序与目标一致时原样返回 (幂等). 标签表与指纹复用.

        Args:
            device: 转移目标设备.

        Returns:
            Wd14Model: 完成转移的模型句柄.

        Callers:
            - ``services/core/device.py:register_model`` 注册的 move 回调
        """
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if device.type == "cuda"
            else ["CPUExecutionProvider"]
        )
        if self._session.get_providers() != providers:
            self._session = _build_session(providers)
        return self

    def close(self) -> None:
        """释放 ONNX 推理会话.

        Callers:
            - ``services/core/device.py:register_model`` 注册的 unload 回调
        """
        del self._session

    def scores(self, img: Image.Image) -> np.ndarray:
        """对单张图跑 WD14, 返回整条输出概率向量.

        Args:
            img (Image.Image): 待打标图片.

        Returns:
            np.ndarray: 与标签表逐行对齐的概率向量, 已含 rating 与角色标签.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.ensure``
        """
        session = self._session
        if img.mode == "RGBA":
            rgb = Image.new("RGB", img.size, (255, 255, 255))
            rgb.paste(img, mask=img.split()[3])
            img = rgb
        else:
            img = img.convert("RGB")
        w, h = img.size
        max_side = max(w, h)
        canvas = Image.new("RGB", (max_side, max_side), (255, 255, 255))
        canvas.paste(img, ((max_side - w) // 2, (max_side - h) // 2))
        input_img = canvas.resize((448, 448), Image.Resampling.LANCZOS)
        arr = np.array(input_img).astype(np.float32)
        arr = arr[:, :, ::-1]
        input_data = arr[np.newaxis, ...]
        input_name = session.get_inputs()[0].name
        # 模型输出声明为稀疏张量, 类型推断为 SparseTensor, 但运行时返回 numpy 数组; 显式转 ndarray.
        probs = np.asarray(session.run(None, {input_name: input_data})[0])[0]
        return probs.astype(np.float32, copy=False)


_MODEL = register_model(Wd14Model.load, Wd14Model.close, Wd14Model.retarget)


class Wd14Cache:
    """与图片同目录的 WD14 推理缓存.

    缓存按模型指纹分目录, 每张图保存整条输出概率向量; 取标签的阈值在读取时
    决定, 因此更换阈值只需重新取用, 不必重新推理.

    Attributes:
        root (Path): 数据集根目录.
        labels (Wd14Labels): 标签表与模型身份.

    Callers:
        - ``services/analysis/wd14.py:run_batch`` : 打标主循环.
        - ``services/analysis/wd14.py:Wd14Cache.populate`` : 描述页补建缓存.
        - ``services/analysis/caption.py:run_caption`` : 任务级缓存句柄.
    """

    def __init__(self, root: Path, labels: Wd14Labels) -> None:
        """初始化目录缓存句柄.

        Args:
            root (Path): 数据集根目录.
            labels (Wd14Labels): 标签表与模型身份.

        Callers:
            - ``services/analysis/wd14.py:run_batch``
            - ``services/analysis/caption.py:run_caption``
        """
        self.root = root
        self.labels = labels

    def path(self, src: Path) -> Path:
        """返回图片的缓存文件路径: 落在图片所在工作子目录内.

        缓存置于 ``<图片目录>/.wd14_meta/<模型指纹>/<stem>.npy``, 与来源帖
        元数据目录同级, 数据集根只保留工作子目录与任务级元数据.

        Args:
            src (Path): 图片绝对路径.

        Returns:
            Path: 缓存 ``.npy`` 文件路径.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.missing``
            - ``services/analysis/wd14.py:Wd14Cache.ensure``
            - ``services/analysis/wd14.py:Wd14Cache._scores``
        """
        return src.parent / WD14_META_DIRNAME / self.labels.fingerprint / f"{src.stem}.npy"

    def missing(self, images: Iterable[Path]) -> list[Path]:
        """筛出推理缓存缺失的图片.

        Args:
            images (Iterable[Path]): 待检查的图片绝对路径.

        Returns:
            list[Path]: 缓存文件不存在的图片列表, 保持输入顺序.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.ensure``
            - ``services/analysis/wd14.py:Wd14Cache.populate``
        """
        return [src for src in images if not self.path(src).is_file()]

    def ensure(
        self,
        images: list[Path],
        *,
        model: Wd14Model,
        log: Callable[[str], None],
        cancel_check: Callable[[], bool],
        on_progress: Callable[[int, int, Path], None] | None = None,
    ) -> set[Path]:
        """为缓存缺失的图片跑推理, 把整条概率向量写入缓存.

        已有缓存的图片不重复推理; 单张失败记入日志后继续, 不中断整批.
        模型文件缺失属配置错误, 由推理调用链直接抛出.

        Args:
            images (list[Path]): 待检查的图片绝对路径列表.
            model (Wd14Model): 模型句柄.
            log (Callable[[str], None]): 日志回调, 接收单行文本.
            cancel_check (Callable[[], bool]): 协作取消判定, 返回 True 时中止.
            on_progress (Callable[[int, int, Path], None] | None): 单张完成回调
                ``(已完成数, 总数, 图片路径)``.

        Returns:
            set[Path]: 推理或写入失败的图片集合.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.populate``
            - ``services/analysis/wd14.py:run_batch``
        """
        missing = self.missing(images)
        if not missing:
            return set()

        log(f"WD14 缓存缺失 {len(missing)}/{len(images)} 张, 推理补齐…")
        failed: set[Path] = set()
        for i, src in enumerate(missing):
            if cancel_check():
                log(f"WD14 缓存生成已取消: 完成 {i} 张, 失败 {len(failed)} 张")
                return failed
            try:
                with Image.open(src) as img:
                    scores = model.scores(img)
                path = self.path(src)
                path.parent.mkdir(parents=True, exist_ok=True)
                buf = io.BytesIO()
                np.save(buf, scores.astype(np.float16), allow_pickle=False)
                atomic_write_bytes(path, buf.getvalue())
            except (OSError, ValueError, RuntimeError) as e:
                failed.add(src)
                log(f"WD14 缓存生成失败: {self.rel(src)} — {e}")
            else:
                log(f"WD14 缓存 [{i + 1}/{len(missing)}]: {self.rel(src)}")
            if on_progress:
                on_progress(i + 1, len(missing), src)
        log(f"WD14 缓存生成完成: 新建 {len(missing) - len(failed)} 张, 失败 {len(failed)} 张")
        return failed

    def populate(
        self,
        images: list[Path],
        *,
        log: Callable[[str], None],
        cancel_check: Callable[[], bool],
    ) -> None:
        """按需加载模型并补齐推理缓存, 不改动任何 ``.txt``.

        供描述页在参考标签注入或标签模式命中需要 WD14 标签时, 先补齐缓存再生成
        描述. 全部就绪时不加载模型.

        Args:
            images (list[Path]): 待检查的图片绝对路径列表.
            log (Callable[[str], None]): 日志回调, 接收单行文本.
            cancel_check (Callable[[], bool]): 协作取消判定, 返回 True 时中止.

        Returns:
            None

        Raises:
            RuntimeError: WD14 模型权重或标签表缺失.

        Callers:
            - ``services/analysis/caption.py:run_caption``
        """
        if not self.missing(images):
            return
        with _MODEL as raw_model:
            self.ensure(
                images,
                model=cast(Wd14Model, raw_model),
                log=log,
                cancel_check=cancel_check,
            )

    def _scores(self, src: Path) -> np.ndarray | None:
        """读取图片的整条 WD14 概率向量.

        Args:
            src (Path): 图片绝对路径.

        Returns:
            np.ndarray | None: float32 概率向量; 缓存缺失时为 None.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.groups``
            - ``services/analysis/wd14.py:Wd14Cache.refresh``
        """
        path = self.path(src)
        if not path.is_file():
            return None
        return np.load(path).astype(np.float32)

    def groups(self, src: Path, threshold: float) -> Wd14TagGroups | None:
        """读取缓存向量并按阈值取出分组标签.

        Args:
            src (Path): 图片绝对路径.
            threshold (float): 置信度下限.

        Returns:
            Wd14TagGroups | None: 按类别分组的标签; 缓存缺失时为 None.

        Callers:
            - ``services/analysis/caption.py:_generate_captions``
        """
        scores = self._scores(src)
        if scores is None:
            return None
        return self.labels.groups(scores, threshold)

    def refresh(
        self,
        src: Path,
        *,
        threshold: float,
        disable_orig_tags: bool,
        artist_tag: str = "",
    ) -> None:
        """按阈值从缓存取标签, 合并原标签后刷新图片的同名 ``.txt``.

        Args:
            src (Path): 图片绝对路径.
            threshold (float): 置信度下限.
            disable_orig_tags (bool): 是否丢弃原标签.
            artist_tag (str): 追加的画师标签 (以 ``@`` 开头).

        Returns:
            None

        Raises:
            RuntimeError: 推理缓存缺失.

        Callers:
            - ``services/analysis/wd14.py:run_batch``
        """
        scores = self._scores(src)
        if scores is None:
            raise RuntimeError(f"推理缓存缺失: {self.rel(src)}")
        txt = src.with_suffix(".txt")
        base = (
            []
            if disable_orig_tags
            else Tags.split_caption(
                txt.read_text(encoding="utf-8") if txt.is_file() else ""
            )
        )
        groups = self.labels.groups(scores, threshold)
        caption = Tags.merge_for_disk_by_confidence(
            base,
            groups,
            artist_tag=artist_tag,
        )
        atomic_write_text(txt, caption)

    def rel(self, src: Path) -> str:
        """返回图片相对数据集根的路径, 用于日志与报错.

        Args:
            src (Path): 图片绝对路径.

        Returns:
            str: POSIX 风格相对路径.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.ensure``
            - ``services/analysis/wd14.py:Wd14Cache.refresh``
            - ``services/analysis/wd14.py:run_batch``
        """
        return src.relative_to(self.root).as_posix()


def model_ready() -> tuple[bool, str]:
    """检查 WD14 模型目录与权重/标签文件是否齐全.

    Returns:
        ``(ok, message)``. ok 为 True 时表示模型就绪、可加载.

    Callers:
        - ``routes/tagging.py:dirs``
        - ``routes/tagging.py:start``
        - ``services/analysis/wd14.py:Wd14Model.load``
    """
    try:
        onnx_path, _csv_path = _model_files()
    except RuntimeError as e:
        return False, str(e)
    return True, f"模型就绪: {onnx_path}"


@dataclass
class TaggingProgress:
    status: str = "idle"  # idle|running|done|error|cancelled
    phase: str = "idle"  # idle|loading|running|done
    total: int = 0
    done: int = 0
    skipped: int = 0
    ok: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False

    def snapshot(self) -> JobSnapshot:
        """导出进度快照.

        Returns:
            JobSnapshot: 当前进度的快照对象, 供长任务 SSE 推送.

        Callers:
            - ``routes/tagging.py:start``
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

    def log(self, line: str) -> None:
        """追加进度日志并截断.

        Args:
            line: 日志行文本.

        Callers:
            - ``services/analysis/wd14.py:Wd14Cache.ensure`` (经 ``run_batch`` 传入)
            - ``services/analysis/wd14.py:run_batch``
        """
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


@_MODEL.auto_release
def run_batch(
    works: list[WorkDir],
    *,
    handle: Wd14Model,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
    progress: TaggingProgress,
    on_update: Callable[[], None] | None = None,
    artist_tag: str = "",
) -> None:
    """批量打标主循环: 先生成推理缓存, 再统一取标签刷新图片 ``.txt``.

    阶段一为缓存缺失的待写图片跑推理并缓存整条概率向量; 阶段二从缓存按阈值
    取标签、合并原标签后写入 ``.txt``. 缓存与阈值解耦, 更换阈值无需重新推理.

    Args:
        works (list[WorkDir]): 待打标的工作目录.
        handle (Wd14Model): 模型句柄, 由 ``auto_release`` 注入.
        threshold (float): 置信度阈值.
        disable_orig_tags (bool): 是否丢弃原标签.
        force (bool): 是否覆盖已存在的 ``.txt``.
        progress (TaggingProgress): 进度对象.
        on_update (Callable[[], None] | None): 进度变更回调.
        artist_tag (str): 追加的画师标签 (以 ``@`` 开头).

    Returns:
        None

    Callers:
        - ``routes/tagging.py:start``
    """
    root = works[0].dataset.root
    items = [p for work in works for p in work.image_files()]
    progress.total = len(items)
    progress.done = 0
    progress.skipped = 0
    progress.ok = 0
    progress.failed = 0
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张, 加载模型…"
    progress.log(
        f"开始: dirs={[w.name for w in works]}, threshold={threshold}, "
        f"disable_orig_tags={disable_orig_tags}, force={force}, "
        f"artist={artist_tag or '无'}, total={len(items)}"
    )
    if on_update:
        on_update()

    if not items:
        progress.status = "done"
        progress.phase = "done"
        progress.message = "没有可处理的图片"
        progress.log(progress.message)
        if on_update:
            on_update()
        return

    cache = Wd14Cache(root, handle.labels)
    pending = [
        src for src in items if force or not src.with_suffix(".txt").exists()
    ]
    progress.skipped = len(items) - len(pending)
    progress.done = progress.skipped
    progress.phase = "running"
    progress.message = f"共 {len(pending)} 张待写入, 先生成推理缓存"
    progress.log(progress.message)
    if on_update:
        on_update()

    def report_cache(done: int, total: int, src: Path) -> None:
        """把缓存生成进度写入进度对象.

        Args:
            done (int): 已完成的缓存图片数.
            total (int): 本次缓存生成的图片总数.
            src (Path): 刚完成的图片路径.

        Returns:
            None

        Callers:
            - ``services/analysis/wd14.py:run_batch`` 的 ``Wd14Cache.ensure`` 回调
        """
        progress.current = cache.rel(src)
        progress.message = f"生成缓存 [{done}/{total}] {progress.current}"
        if on_update:
            on_update()

    failed = cache.ensure(
        pending,
        model=handle,
        log=progress.log,
        cancel_check=lambda: progress.cancel_requested,
        on_progress=report_cache,
    )
    progress.failed = len(failed)

    if progress.cancel_requested:
        progress.status = "cancelled"
        progress.phase = "done"
        progress.current = ""
        progress.message = f"已取消 (缓存阶段中止, 跳过 {progress.skipped} 张)"
        progress.log(progress.message)
        if on_update:
            on_update()
        return

    progress.log(f"缓存就绪, 按阈值 {threshold} 生成标签…")
    for i, src in enumerate(pending):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.current = ""
            progress.message = f"已取消 ({progress.ok} 成功 / {progress.skipped} 跳过)"
            progress.log(progress.message)
            if on_update:
                on_update()
            return

        rel = cache.rel(src)
        progress.current = rel
        progress.message = f"[{i + 1}/{len(pending)}] {rel}"
        if on_update:
            on_update()

        if src not in failed:
            try:
                cache.refresh(
                    src,
                    threshold=threshold,
                    disable_orig_tags=disable_orig_tags,
                    artist_tag=artist_tag,
                )
            except (OSError, ValueError, RuntimeError) as e:
                progress.failed += 1
                progress.log(f"[{i + 1}/{len(pending)}] 失败: {rel} — {e}")
            else:
                progress.ok += 1
                progress.log(f"[{i + 1}/{len(pending)}] 完成: {rel}")
        progress.done += 1
        if on_update:
            on_update()

    progress.status = "done"
    progress.phase = "done"
    progress.current = ""
    progress.message = (
        f"完成: 新处理 {progress.ok} 张, 跳过 {progress.skipped} 张, "
        f"失败 {progress.failed} 张, 合计 {progress.total} 张"
    )
    progress.log(progress.message)
    if on_update:
        on_update()
