"""工作子目录解析（筛选 / 去重等）。"""

from __future__ import annotations

from pathlib import Path

from services.wd14_batch import list_image_subdirs

# 不允许当作筛选目录的名字
_BLOCKED = frozenset({
    "mask",
    "aesthetic_report",
    ".trash",
    ".thumbs",
    ".thumbnails",
    "__pycache__",
})


def resolve_subdir(root: Path, name: str | None) -> tuple[str, Path]:
    """
    解析一级子目录名，默认 main。
    返回 (name, abs_path)。
    """
    root = root.resolve()
    raw = (name or "main").strip().replace("\\", "/").strip("/")
    if not raw or raw == ".":
        raw = "main"
    if "/" in raw or ".." in raw or raw.startswith(".") or raw in _BLOCKED:
        raise ValueError(f"非法目录: {name}")
    path = (root / raw).resolve()
    if not str(path).startswith(str(root)) or not path.is_dir():
        raise ValueError(f"目录不存在: {raw}")
    return raw, path


def list_work_dirs(root: Path) -> list[dict]:
    """可选工作目录列表（默认优先 main）。"""
    dirs = list_image_subdirs(root)
    # 去掉根目录项「.」，筛选只做一级子目录
    out = [d for d in dirs if d["name"] != "."]
    if not any(d["name"] == "main" for d in out):
        main = root / "main"
        if main.is_dir():
            out.insert(0, {"name": "main", "label": "main", "image_count": 0})
    return out
