"""工作子目录解析（筛选 / 去重等）。"""

from __future__ import annotations

from pathlib import Path

from services.wd14_batch import IMAGE_EXTS, list_image_subdirs

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


def resolve_subdirs(root: Path, names: list[str] | None) -> list[tuple[str, Path]]:
    """解析多个子目录；空则默认 main。"""
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
    """从 dirs=a,b 或 dir=a 解析目录名列表。"""
    if dirs and dirs.strip():
        return [x.strip() for x in dirs.split(",") if x.strip()]
    if dir and dir.strip():
        return [dir.strip()]
    return ["main"]


def _count_images(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def pin_main(root: Path, dirs: list[dict]) -> list[dict]:
    """main 置顶；若不存在于列表但磁盘有 main/ 则补上。"""
    by_name = {d["name"]: dict(d) for d in dirs if d.get("name") and d["name"] != "."}
    out: list[dict] = []
    main_path = root / "main"
    if "main" in by_name:
        out.append(by_name.pop("main"))
    elif main_path.is_dir():
        out.append({
            "name": "main",
            "label": "main",
            "image_count": _count_images(main_path),
        })
    for name in sorted(by_name):
        out.append(by_name[name])
    return out


def list_work_dirs(root: Path) -> list[dict]:
    """可选工作目录列表（main 置顶）。"""
    dirs = list_image_subdirs(root)
    out = [d for d in dirs if d["name"] != "."]
    return pin_main(root, out)
