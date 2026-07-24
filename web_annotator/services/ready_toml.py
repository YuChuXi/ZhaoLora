"""生成训练用 datasets TOML 片段，并统计 main 蒙版状态。"""

from __future__ import annotations

from pathlib import Path

from services.mask_manager import has_mask
from services.scanner import IMAGE_EXTS, scan_images
from services.work_dir import list_work_dirs


def _has_images(d: Path) -> bool:
    if not d.is_dir():
        return False
    return any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in d.iterdir())


def main_mask_stats(root: Path) -> dict:
    """
    统计 main 相对蒙版状态：每张图仅「无」或「完整」。

    整体 status：全部有蒙版 →「完整」；否则 →「无」。
    引用：ready_dirs、build_ready_toml；ready.js 侧栏。
    """
    root = root.resolve()
    main_dir = root / "main"
    mask_dir = root / "mask"
    images = scan_images(main_dir) if main_dir.is_dir() else []
    complete = 0
    none = 0
    for img in images:
        if has_mask(mask_dir, img["path"]):
            complete += 1
        else:
            none += 1
    total = complete + none
    status = "完整" if total > 0 and none == 0 else "无"
    return {
        "mask_status": status,
        "mask_complete": complete,
        "mask_none": none,
        "main_count": total,
    }


def build_ready_toml(
    root: Path,
    *,
    dirs: list[str] | None = None,
    num_repeats: int = 1,
) -> dict:
    """
    返回 { toml, subsets, root, mask_* }。

    仅当 main 蒙版状态为「完整」时，main subset 附带 conditioning_data_dir。
    引用：POST /ready/toml。
    """
    root = root.resolve()
    available = {d["name"]: d for d in list_work_dirs(root)}
    if dirs:
        names = [n for n in dirs if n in available]
    else:
        names = list(available.keys())

    ordered: list[str] = []
    if "main" in names:
        ordered.append("main")
    ordered.extend(sorted(n for n in names if n != "main"))

    mask_stats = main_mask_stats(root)
    use_mask = mask_stats["mask_status"] == "完整"
    mask_dir = root / "mask"
    repeats = max(1, int(num_repeats))

    subsets: list[dict] = []
    blocks: list[str] = []
    for name in ordered:
        image_dir = (root / name).resolve()
        if not _has_images(image_dir):
            continue
        entry: dict = {
            "name": name,
            "image_dir": str(image_dir),
            "num_repeats": repeats,
            "image_count": available.get(name, {}).get("image_count", 0),
        }
        lines = [
            "[[datasets]]",
            "  [[datasets.subsets]]",
            f'  image_dir = "{image_dir}"',
        ]
        if name == "main" and use_mask:
            entry["conditioning_data_dir"] = str(mask_dir.resolve())
            lines.append(f'  conditioning_data_dir = "{mask_dir.resolve()}"')
        lines.append(f"  num_repeats = {repeats}")
        blocks.append("\n".join(lines))
        subsets.append(entry)

    toml = "\n\n".join(blocks)
    if toml:
        toml += "\n"
    return {
        "root": str(root),
        "toml": toml,
        "subsets": subsets,
        "dirs": list(available.values()),
        "mask": str(mask_dir.resolve()) if use_mask else None,
        "num_repeats": repeats,
        **mask_stats,
    }
