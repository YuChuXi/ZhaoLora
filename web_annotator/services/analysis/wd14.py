"""WD14 批量打标. """

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, cast

import numpy as np
from PIL import Image

from services.models import JobSnapshot, WorkDirInfo
from services.core.natural import natural_key
from services.core.scanner import IMAGE_EXTS, collect_images
from services.online_sources.framework.utils import _GENDER_TAGS
from services.core.device import register_model

if TYPE_CHECKING:
    from onnxruntime import InferenceSession

DEFAULT_THRESHOLD = 0.1

_REPO_ROOT = Path(__file__).resolve().parents[3]
WD14_MODEL_DIR = _REPO_ROOT / "wd14_tagger_model" / "SmilingWolf_wd-vit-large-tagger-v3"
WD14_ONNX_PATH = WD14_MODEL_DIR / "model.onnx"
WD14_CSV_PATH = WD14_MODEL_DIR / "selected_tags.csv"

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


def model_ready() -> tuple[bool, str]:
    """检查本地 WD14 权重是否齐全; 返回相对仓库根的模型目录路径. 

    Callers:
        - ``routes/tagging.py:dirs``
        - ``routes/tagging.py:start``
    """
    rel = WD14_MODEL_DIR.relative_to(_REPO_ROOT).as_posix()
    if WD14_ONNX_PATH.is_file() and WD14_CSV_PATH.is_file():
        return True, rel
    return False, f"找不到本地 WD14 模型: {rel}"


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

    return ort.InferenceSession(str(WD14_ONNX_PATH), providers=providers)


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

    df = pd.read_csv(WD14_CSV_PATH)
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


def _process_one(
    src: Path,
    *,
    handle: _WD14_HANDLE,
    threshold: float,
    disable_orig_tags: bool,
    force: bool,
    artist_tag: str = "",
) -> str:
    """处理单张图片: WD14 推理 → 合并原标签 → 写入同名 .txt. 

    Args:
        src: 图片绝对路径.
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

    img = Image.open(src)
    base: list[str] = []
    if not disable_orig_tags:
        base = Tags.split_caption(_load_text(out_txt_path))
    wd_scored = _infer_wd14(img, threshold, handle)
    caption = Tags.merge_for_disk_by_confidence(base, wd_scored, artist_tag=artist_tag)
    out_txt_path.write_text(caption, encoding="utf-8")
    return "ok"


@_MODEL.auto_release
def run_batch(
    dirs: list[Path],
    *,
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
