"""WD14 批量打标 (SmilingWolf 系列标签模型, ONNX 推理). """

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, cast

import numpy as np
from PIL import Image

from services.models import JobSnapshot, WorkDirInfo
from services.core.atomic import atomic_write_text
from services.core.natural import natural_key
from services.core.scanner import IMAGE_EXTS, collect_images
from services.dataset.layout import WD14_META_DIRNAME
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

# WD14 模型句柄: (onnx 会话, [(标签名, 是否角色), ...]), 由 device 管理生命周期
_WD14_HANDLE = tuple["InferenceSession", list[tuple[str, bool]]]


class Tags:
    """WD14 标签工具集, 提供拆分、拼接、等价键比较与置信度合并. 

    Callers:
        - ``services/analysis/wd14.py:_process_one`` : 调用 ``split_caption``、``merge_for_disk_by_confidence``
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
            - ``services/analysis/wd14.py:_process_one``
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
        wd_scored: list[tuple[str, float, bool]],
        *,
        artist_tag: str = "",
    ) -> str:
        """按置信度合并原标签与 WD14 结果, 并按 Anima 格式输出.

        头部按 性别 → 角色 → 画师 收纳, 其余通用标签置于 ``|||`` 分隔的正文,
        与图源下载的 Anima 风格保持一致. 角色标签由 ``is_character`` 标记区分,
        画师标签由 ``artist_tag`` 显式提供(需带 ``@`` 前缀), 均按等价键去重.

        Args:
            base: 原旁路标签文本拆分列表.
            wd_scored: ``(标签名, 置信度, 是否角色)`` 元组列表.
            artist_tag: 画师标签, 需带 ``@`` 前缀; 为空则不写入.

        Returns:
            Anima 格式的标签文本.

        Callers:
            - ``services/analysis/wd14.py:_process_one``
        """
        score_by_key: dict[str, float] = {}
        canonical_by_key: dict[str, str] = {}
        character_key: set[str] = set()
        for tag, score, is_character in wd_scored:
            key = Tags.equivalence_key(tag)
            if key not in score_by_key or score > score_by_key[key]:
                score_by_key[key] = score
                canonical_by_key[key] = Tags._canonical_spacing(tag)
            if is_character:
                character_key.add(key)
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


# 打标页不列出这些目录(mask 是 alpha, 非待打标图)
_SKIP_TAG_DIRS = frozenset({
    "mask",
    "aesthetic_report",
    ".trash",
    ".thumbs",
    ".thumbnails",
    "__pycache__",
})


def list_image_subdirs(root: Path) -> list[WorkDirInfo]:
    """列出根下含图片的一级子目录.

    Args:
        root: 数据集根目录.

    Returns:
        list[WorkDirInfo]: 含图片的一级子目录信息列表; 根目录自身有图时以
        ``name="."`` 列首, ``main`` 次之, 其余按名排序.

    Callers:
        - ``routes/tagging.py:dirs``
        - ``routes/scoring.py:dirs``
        - ``services/core/work_dir.py:list_work_dirs``
        - ``services/analysis/wd14.py:resolve_image_dirs``
    """
    root = root.resolve()
    out: list[WorkDirInfo] = []
    if not root.is_dir():
        return out

    def count_images(d: Path) -> int:
        """统计目录内图像数.

        Args:
            d: 待统计目录.

        Returns:
            int: 目录内图片文件数.

        Callers:
            - ``services/analysis/wd14.py:list_image_subdirs``
        """
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    n_root = count_images(root)
    if n_root:
        out.append(WorkDirInfo(name=".", label="(根目录)", image_count=n_root))

    for p in sorted(root.iterdir(), key=lambda p: natural_key(p.name)):
        if not p.is_dir() or p.name.startswith(".") or p.name in _SKIP_TAG_DIRS:
            continue
        n = count_images(p)
        if n > 0 or p.name == "main":
            out.append(WorkDirInfo(name=p.name, label=p.name, image_count=n))

    root_item = [d for d in out if d.name == "."]
    rest = [d for d in out if d.name != "."]
    main = [d for d in rest if d.name == "main"]
    others = [d for d in rest if d.name != "main"]
    if not main and (root / "main").is_dir():
        main = [WorkDirInfo(name="main", label="main", image_count=count_images(root / "main"))]
    return root_item + main + others


def resolve_image_dirs(root: Path, names: list[str] | None) -> list[Path]:
    """将子目录名解析为绝对路径；未知名跳过. 

    Callers:
        - ``routes/scoring.py:start``
        - ``routes/tagging.py:start``
    """
    root = root.resolve()
    available = {
        d.name: root if d.name == "." else root / d.name
        for d in list_image_subdirs(root)
    }
    if not names:
        return []
    dirs: list[Path] = []
    for name in names:
        name = (name or "").strip()
        if name in available:
            dirs.append(available[name])
    return dirs


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
        - ``services/analysis/wd14.py:_build_session``
        - ``services/analysis/wd14.py:_load_model``
    """
    if not WD14_ONNX.is_file():
        raise RuntimeError(f"WD14 模型权重不存在: {WD14_ONNX}")
    if not WD14_CSV.is_file():
        raise RuntimeError(f"WD14 标签表不存在: {WD14_CSV}")
    return WD14_ONNX, WD14_CSV


def model_ready() -> tuple[bool, str]:
    """检查 WD14 模型目录与权重/标签文件是否齐全.

    Returns:
        ``(ok, message)``. ok 为 True 时表示模型就绪、可加载.

    Callers:
        - ``routes/tagging.py:dirs``
        - ``routes/tagging.py:start``
    """
    try:
        onnx_path, _csv_path = _model_files()
    except RuntimeError as e:
        return False, str(e)
    return True, f"模型就绪: {onnx_path}"


def _build_session(providers: list[str]) -> "InferenceSession":
    """按执行提供程序创建 WD14 ONNX 推理会话.

    Args:
        providers: ONNX 执行提供程序列表, 首位为首选设备.

    Returns:
        InferenceSession: 已初始化的 ONNX 推理会话.

    Callers:
        - ``services/analysis/wd14.py:_load_model``
        - ``services/analysis/wd14.py:_move_model``
    """
    import onnxruntime as ort

    onnx_path, _csv_path = _model_files()
    return ort.InferenceSession(str(onnx_path), providers=providers)


def _load_model() -> _WD14_HANDLE:
    """加载 WD14 ONNX 推理会话与标签表 (会话恒以 CPU 提供程序创建).

    转移目标设备由 ``device.Model`` 生命周期统一决定 (``_move_model``),
    加载侧不做设备判断.

    Returns:
        ``(session, labels)`` — ``labels`` 为 ``(标签名, 是否角色)`` 元组列表,
        与模型输出索引对齐.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 load 回调
    """
    ok, msg = model_ready()
    if not ok:
        raise RuntimeError(msg)
    import pandas as pd

    _onnx_path, csv_path = _model_files()
    df = pd.read_csv(csv_path)
    is_character = (df["category"].astype(str) == "4").tolist()
    labels: list[tuple[str, bool]] = list(zip(df["name"].tolist(), is_character))
    return _build_session(["CPUExecutionProvider"]), labels


def _move_model(handle: _WD14_HANDLE, device) -> _WD14_HANDLE:
    """把 WD14 会话转移到目标设备.

    ONNX 会话的执行提供程序创建后不可迁移, 故按目标设备重建会话;
    会话生效提供程序与目标一致时原样返回 (幂等).

    Args:
        handle: ``_load_model`` 返回的 ``(session, labels)`` 元组.
        device: 转移目标设备.

    Returns:
        完成转移后的 ``(session, labels)`` 元组.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 move 回调
    """
    session, labels = handle
    providers = (
        ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if device.type == "cuda"
        else ["CPUExecutionProvider"]
    )
    if session.get_providers() == providers:
        return handle
    del session
    return _build_session(providers), labels


def _unload_model(handle: object) -> None:
    """释放 WD14 ONNX 会话.

    Args:
        handle: ``_load_model`` 返回的 ``(session, labels)`` 元组.

    Callers:
        - ``services/core/device.py:register_model`` 注册的 unload 回调
    """
    session, _labels = cast("_WD14_HANDLE", handle)
    del session, _labels


_MODEL = register_model(_load_model, _unload_model, _move_model)


def _infer_wd14(
    img: Image.Image,
    threshold: float,
    handle: _WD14_HANDLE,
) -> list[tuple[str, float, bool]]:
    """对单张图跑 WD14, 返回 (tag, score, is_character).

    Args:
        img: 待打标图片.
        threshold: 置信度阈值.
        handle: WD14 模型句柄 ``(session, labels)``, 由 ``run_batch`` 经 ``auto_release`` 持有.

    Returns:
        ``[(tag, score, is_character), ...]``, 已按置信度过滤阈值以上.

    Callers:
        - ``services/analysis/wd14.py:_process_one``
    """
    session, labels = handle
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
    scored: list[tuple[str, float, bool]] = []
    for i, p in enumerate(probs):
        if i < len(labels) and float(p) >= threshold:
            name, is_character = labels[i]
            if name in ["general", "sensitive", "questionable", "explicit"]:
                continue
            scored.append((name.replace("_", " ").strip(), float(p), is_character))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _load_text(path: Path) -> str:
    """读取旁路 txt；不存在则空串. 

    Callers:
        - ``services/analysis/wd14.py:_process_one``
    """
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Wd14Tag:
    """WD14 推理得到的一个标签及其置信度与标签类型.

    Attributes:
        name (str): 标签名(下划线已转空格).
        score (float): 置信度, 由阈值过滤后保留.
        category (str): 标签类型, 取值 ``general`` 或 ``character``.

    Callers:
        - ``services/analysis/wd14.py:write_meta`` : 序列化来源.
        - ``services/analysis/wd14.py:load_meta`` : 反序列化产物.
        - ``services/analysis/caption.py:run_caption`` : 注入提示词的参考候选.
    """

    name: str
    score: float
    category: str


def meta_path(root: Path, image_rel: str) -> Path:
    """返回图片的 WD14 缓存文件路径 ``<root>/.wd14_meta/<子目录>/<stem>.json``.

    缓存按图片相对路径镜像存放, 使不同子目录的同名图片各占一份缓存.

    Args:
        root (Path): 数据集根目录.
        image_rel (str): 图片相对数据集根的路径, 如 ``main/foo.png``.

    Returns:
        Path: 缓存 json 的绝对路径.

    Callers:
        - ``services/analysis/wd14.py:write_meta``
        - ``services/analysis/wd14.py:load_meta``
        - ``services/analysis/caption.py:run_caption``
    """
    rel = image_rel.replace("\\", "/").lstrip("/")
    return root / WD14_META_DIRNAME / Path(rel).with_suffix(".json")


def write_meta(root: Path, image_rel: str, threshold: float, tags: list[Wd14Tag]) -> None:
    """把单张图片的 WD14 推理结果写入缓存 json.

    缓存只记录阈值以上的标签, 供描述页注入参考候选与就绪页统计完整度;
    父目录按需创建, 写入经原子替换, 避免半截文件.

    Args:
        root (Path): 数据集根目录.
        image_rel (str): 图片相对数据集根的路径.
        threshold (float): 本次推理所用置信度阈值.
        tags (list[Wd14Tag]): 阈值以上的标签列表, 按置信度降序.

    Returns:
        None

    Callers:
        - ``services/analysis/wd14.py:_process_one``
    """
    path = meta_path(root, image_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "image": image_rel.replace("\\", "/").lstrip("/"),
        "threshold": threshold,
        "tags": [
            {"tag": tag.name, "score": round(tag.score, 6), "category": tag.category}
            for tag in tags
        ],
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_meta(root: Path, image_rel: str) -> list[Wd14Tag] | None:
    """读取图片的 WD14 缓存标签.

    Args:
        root (Path): 数据集根目录.
        image_rel (str): 图片相对数据集根的路径.

    Returns:
        list[Wd14Tag] | None: 缓存标签列表; 未打标或缓存缺失时为 None.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    path = meta_path(root, image_rel)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Wd14Tag(name=str(item["tag"]), score=float(item["score"]), category=str(item["category"]))
        for item in data.get("tags", [])
    ]


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
            - ``services/analysis/wd14.py:run_batch``
        """
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-300:]


def _infer_and_cache(
    img: Image.Image,
    *,
    root: Path,
    image_rel: str,
    threshold: float,
    handle: _WD14_HANDLE,
) -> list[tuple[str, float, bool]]:
    """对单张图片跑 WD14 推理, 并把阈值以上的标签写入 ``.wd14_meta`` 缓存.

    Args:
        img: 待打标图片.
        root: 数据集根目录, 用于定位 ``.wd14_meta`` 缓存.
        image_rel: 图片相对数据集根的路径.
        threshold: 置信度阈值, 低于该值的标签不写入缓存.
        handle: WD14 模型句柄, 由调用方持有.

    Returns:
        ``[(tag, score, is_character), ...]``, 与写入缓存的标签一致.

    Callers:
        - ``services/analysis/wd14.py:_process_one``
        - ``services/analysis/wd14.py:ensure_meta``
    """
    wd_scored = _infer_wd14(img, threshold, handle)
    write_meta(
        root,
        image_rel,
        threshold,
        [
            Wd14Tag(
                name=name,
                score=score,
                category="character" if is_character else "general",
            )
            for name, score, is_character in wd_scored
        ],
    )
    return wd_scored


def _process_one(
    src: Path,
    *,
    root: Path,
    handle: _WD14_HANDLE,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
    artist_tag: str = "",
) -> str:
    """处理单张图片: WD14 推理 → 缓存置信度 → 合并原标签 → 写入同名 .txt. 

    Args:
        src: 图片绝对路径.
        root: 数据集根目录, 用于定位 ``.wd14_meta`` 缓存.
        handle: WD14 模型句柄, 由 ``run_batch`` 持有并透传.
        threshold: 置信度阈值.
        disable_orig_tags: 是否丢弃原标签.
        force: 是否覆盖已存在的 ``.txt``.
        artist_tag: 追加的画师标签 (以 ``@`` 开头).

    Returns:
        ``"skip"`` 或 ``"ok"``. 

    Callers:
        - ``services/analysis/wd14.py:run_batch``
    """
    out_txt_path = src.with_suffix(".txt")
    if not force and out_txt_path.exists():
        return "skip"

    base: list[str] = []
    if not disable_orig_tags:
        base = Tags.split_caption(_load_text(out_txt_path))
    with Image.open(src) as img:
        wd_scored = _infer_and_cache(
            img,
            root=root,
            image_rel=src.relative_to(root).as_posix(),
            threshold=threshold,
            handle=handle,
        )
    caption = Tags.merge_for_disk_by_confidence(base, wd_scored, artist_tag=artist_tag)
    atomic_write_text(out_txt_path, caption)
    return "ok"


def ensure_meta(
    root: Path,
    images: list[Path],
    *,
    threshold: float,
    log: Callable[[str], None],
    cancel_check: Callable[[], bool],
) -> None:
    """为缓存缺失的图片补建 ``.wd14_meta`` 缓存, 只写缓存并保持同名 ``.txt`` 原样.

    供描述页在参考标签注入或标签模式命中需要 WD14 标签时, 先补齐缓存再生成描述.
    逐个检查缓存文件, 只对缺失的图片跑推理, 已有缓存的原样保留; 全部就绪时不加载
    模型. 单张推理失败记入日志后继续, 不中断整批; 反序列化与模型文件缺失属配置
    错误, 由推理调用链直接抛出.

    Args:
        root: 数据集根目录, 用于定位 ``.wd14_meta`` 缓存.
        images: 待检查的图片绝对路径列表.
        threshold: 推理置信度阈值, 低于该值的标签不写入缓存.
        log: 日志回调, 接收单行文本.
        cancel_check: 协作取消判定, 返回 True 时中止补建.

    Returns:
        None

    Raises:
        RuntimeError: WD14 模型权重或标签表缺失.

    Callers:
        - ``services/analysis/caption.py:run_caption``
    """
    missing = [
        src
        for src in images
        if not meta_path(root, src.relative_to(root).as_posix()).is_file()
    ]
    if not missing:
        return

    log(f"WD14 缓存缺失 {len(missing)}/{len(images)} 张, 加载模型补建 (阈值={threshold})…")
    created = 0
    failed = 0
    with _MODEL as raw_handle:
        handle = cast("_WD14_HANDLE", raw_handle)
        for src in missing:
            if cancel_check():
                log(f"WD14 缓存补建已取消: 新建 {created} 张, 失败 {failed} 张")
                return
            rel = src.relative_to(root).as_posix()
            try:
                with Image.open(src) as img:
                    _infer_and_cache(
                        img,
                        root=root,
                        image_rel=rel,
                        threshold=threshold,
                        handle=handle,
                    )
            except (OSError, ValueError, RuntimeError) as e:
                failed += 1
                log(f"WD14 缓存补建失败: {rel} — {e}")
                continue
            created += 1
            log(f"WD14 缓存补建 [{created}/{len(missing)}]: {rel}")
    log(f"WD14 缓存补建完成: 新建 {created} 张, 失败 {failed} 张")


@_MODEL.auto_release
def run_batch(
    dirs: list[Path],
    *,
    root: Path,
    handle: _WD14_HANDLE,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
    progress: TaggingProgress,
    on_update: Callable[[], None] | None = None,
    artist_tag: str = "",
) -> None:
    """批量打标主循环.

    Args:
        dirs: 待打标的图片目录列表.
        root: 数据集根目录, 用于写入 ``.wd14_meta`` 置信度缓存.
        handle: WD14 模型句柄 ``(session, labels)``, 由 ``auto_release`` 注入.
        threshold: 置信度阈值.
        disable_orig_tags: 是否丢弃原标签.
        force: 是否覆盖已存在的 ``.txt``.
        progress: 进度对象.
        on_update: 进度变更回调.
        artist_tag: 追加的画师标签 (以 ``@`` 开头).

    Callers:
        - ``routes/tagging.py:start``
    """
    items = collect_images(dirs)
    progress.total = len(items)
    progress.done = 0
    progress.skipped = 0
    progress.ok = 0
    progress.failed = 0
    progress.status = "running"
    progress.phase = "loading"
    progress.message = f"共 {len(items)} 张, 加载模型…"
    progress.log(
        f"开始: dirs={[d.name for d in dirs]}, threshold={threshold}, "
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

    progress.log("加载 WD14 模型…")
    progress.phase = "running"
    progress.message = f"共 {len(items)} 张"
    if on_update:
        on_update()

    for i, src in enumerate(items):
        if progress.cancel_requested:
            progress.status = "cancelled"
            progress.phase = "done"
            progress.message = f"已取消 ({progress.ok} 成功 / {progress.skipped} 跳过)"
            progress.log(progress.message)
            if on_update:
                on_update()
            return

        rel = f"{src.parent.name}/{src.name}"
        progress.current = rel
        progress.message = f"[{i + 1}/{len(items)}] {rel}"
        if on_update:
            on_update()

        try:
            result = _process_one(
                src,
                root=root,
                handle=handle,
                threshold=threshold,
                disable_orig_tags=disable_orig_tags,
                force=force,
                artist_tag=artist_tag,
            )
        except (OSError, ValueError, RuntimeError) as e:
            progress.failed += 1
            progress.done += 1
            progress.log(f"[{i + 1}/{len(items)}] 失败: {rel} — {e}")
            if on_update:
                on_update()
            continue
        if result == "skip":
            progress.skipped += 1
            progress.log(f"[{i + 1}/{len(items)}] 跳过(已存在): {rel}")
        else:
            progress.ok += 1
            progress.log(f"[{i + 1}/{len(items)}] 完成: {rel}")
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
