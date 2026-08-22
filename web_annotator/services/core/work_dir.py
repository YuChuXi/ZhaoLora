"""
工作子目录与相对路径解析(筛选 / 去重 / 图片读写等). 
"""

from __future__ import annotations

import os
from pathlib import Path

from services.dataset.layout import NON_BROWSABLE_DIRS
from services.models import WorkDirInfo
from services.core.scanner import IMAGE_EXTS
from services.analysis.wd14 import list_image_subdirs

# 不允许当作筛选目录的名字(复用 layout 的不可浏览目录约定)
_BLOCKED = NON_BROWSABLE_DIRS


def resolve_under(root: Path, rel: str) -> Path:
    """将相对路径解析到 ``root`` 下的绝对路径. 

    仅做词法规范化 (消除 ``..``/``.``) 后判定是否位于 ``root`` 之下, 不跟随
    符号链接。这样既阻止 ``..`` 越界, 又允许数据集内指向外部存储的合法
    符号链接 (如 ``video -> /mnt/...``)。

    Args:
        root: 允许访问的根目录. 
        rel: 相对路径字符串. 

    Returns:
        规范化绝对路径. 

    Raises:
        ValueError: 空路径, 或解析结果不在 ``root`` 之下.

    Callers:
        - `routes/deps.py:resolve_path`
        - `services/state/mask.py:mask_out_path`
        - `services/state/annotator.py:apply_delete` / `restore` / `_move_sidecar_files`
        - `services/core/video_frame.py:VideoFrameStore._video_path`
    """
    root = root.resolve()
    raw = (rel or "").replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError(f"非法路径: {rel!r}")
    text = raw.strip("/")
    if not text:
        raise ValueError(f"非法路径: {rel!r}")
    # os.path.normpath 是标准库唯一的纯词法规范化工具 (消除 ``..``/``.``),
    # 不触碰文件系统, 因此不会跟随符号链接。pathlib 的 ``PurePath`` 不消除
    # ``..``, ``Path.resolve`` 会跟随符号链接, 均不适用。
    path = Path(os.path.normpath(str(root / text)))
    if not path.is_relative_to(root):
        raise ValueError(f"路径越界: {rel}")
    return path


def resolve_subdir(root: Path, name: str | None) -> tuple[str, Path]:
    """解析一级子目录名, 默认 ``main``. 

    Args:
        root: 数据集根目录. 
        name: 子目录名；空则 ``main``. 

    Returns:
        ``(name, abs_path)``. 

    Raises:
        ValueError: 名称非法或目录不存在.

    Callers:
        - `services/core/work_dir.py:resolve_subdirs`
        - `routes/online_sources.py:search` / `download` / `undownload` / `batch_start` / `regenerate_captions` / `refresh_metadata`
        - `routes/images.py:similar_images`
        - `routes/video.py:_services`
    """
    raw = (name or "main").strip().replace("\\", "/").strip("/")
    if not raw or raw == ".":
        raw = "main"
    if "/" in raw or raw.startswith(".") or raw in _BLOCKED:
        raise ValueError(f"非法目录: {name}")
    path = resolve_under(root, raw)
    if not path.is_dir():
        raise ValueError(f"目录不存在: {raw}")
    return raw, path


def resolve_subdirs(root: Path, names: list[str] | None) -> list[tuple[str, Path]]:
    """解析多个子目录；空则默认 ``main``, 自动去重.

    Args:
        root: 数据集根目录.
        names: 子目录名列表; 空则使用 ``["main"]``.

    Returns:
        list[tuple[str, Path]]: ``[(子目录名, 绝对路径), ...]``.

    Raises:
        ValueError: 任一名称非法或目录不存在.

    Callers:
        - `routes/annotations.py:select_all` / `get_state`
        - `routes/images.py:list_images`
        - `routes/dedupe.py:scan_duplicates` / `apply_delete`
    """
    raw = [n for n in (names or []) if (n or "").strip()]
    if not raw:
        raw = ["main"]
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name in raw:
        sub, path = resolve_subdir(root, name)
        if sub in seen:
            continue
        seen.add(sub)
        out.append((sub, path))
    return out


def parse_dir_names(dirs: str | None = None, dir: str | None = None) -> list[str]:
    """从 ``dirs=a,b`` 或 ``dir=a`` 解析目录名列表.

    Args:
        dirs: 逗号分隔的目录名(优先).
        dir: 单个目录名(dirs 为空时使用).

    Returns:
        list[str]: 目录名字符串列表; 均未提供时返回 ``["main"]``.

    Callers:
        - `routes/annotations.py:select_all` / `get_state`
        - `routes/images.py:list_images`
    """
    if dirs and dirs.strip():
        return [x.strip() for x in dirs.split(",") if x.strip()]
    if dir and dir.strip():
        return [dir.strip()]
    return ["main"]


def _count_images(d: Path) -> int:
    """统计目录内图像文件数.

    Args:
        d: 待统计目录.

    Returns:
        int: 目录内图像文件数; 目录不存在时为 0.
    """
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def pin_main(root: Path, dirs: list[WorkDirInfo]) -> list[WorkDirInfo]:
    """将 ``main`` 置顶；磁盘有 ``main/`` 但列表缺失时自动补上.

    Args:
        root: 数据集根目录(用于检查 ``main/`` 是否存在).
        dirs: 原始目录列表.

    Returns:
        list[WorkDirInfo]: ``main`` 在首位的目录列表.

    Callers:
        - `services/core/work_dir.py:list_work_dirs`
    """
    by_name = {d.name: d for d in dirs if d.name and d.name != "."}
    out: list[WorkDirInfo] = []
    main_path = root / "main"
    if "main" in by_name:
        out.append(by_name.pop("main"))
    elif main_path.is_dir():
        out.append(WorkDirInfo(name="main", label="main", image_count=_count_images(main_path)))
    for name in sorted(by_name):
        out.append(by_name[name])
    return out


def list_work_dirs(root: Path) -> list[WorkDirInfo]:
    """列出数据集根下含图片的一级子目录(``main`` 置顶, 排除系统目录).

    Args:
        root: 数据集根目录.

    Returns:
        list[WorkDirInfo]: ``WorkDirInfo`` 列表.

    Callers:
        - `routes/images.py:list_dirs`
        - `routes/ready.py:dirs`
        - `routes/dedupe.py:dirs`
        - `routes/tag_stats.py:dirs`
        - `services/ready.py:build_ready_toml`
        - `services/tagging/stats.py:resolve_selected_dirs`
    """
    dirs = [d for d in list_image_subdirs(root) if d.name != "."]
    return pin_main(root, dirs)


def total_image_count(root: Path) -> int:
    """统计任务根下全部含图工作目录的图片总数.

    Args:
        root: 任务根目录.

    Returns:
        int: 所有工作目录图片数之和.

    Callers:
        - `routes/tasks.py:list_tasks`
        - `services/state/archiver.py:archive_task`
        - `services/ready.py:build_ready_toml`
    """
    return sum(d.image_count for d in list_work_dirs(root))
