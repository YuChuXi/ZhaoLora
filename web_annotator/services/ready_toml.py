"""生成训练用 datasets TOML 片段。"""

from __future__ import annotations

from pathlib import Path

from services.scanner import IMAGE_EXTS
from services.work_dir import list_work_dirs


def _has_images(d: Path) -> bool:
    if not d.is_dir():
        return False
    return any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in d.iterdir())


def build_ready_toml(
    root: Path,
    *,
    dirs: list[str] | None = None,
    num_repeats: int = 1,
) -> dict:
    """
    返回 { toml, subsets, root }。
    main 附带 conditioning_data_dir=mask（若存在）；其它目录仅 image_dir。
    """
    root = root.resolve()
    available = {d["name"]: d for d in list_work_dirs(root)}
    if dirs:
        names = [n for n in dirs if n in available]
    else:
        names = list(available.keys())

    # main 优先，其余按名字
    ordered: list[str] = []
    if "main" in names:
        ordered.append("main")
    ordered.extend(sorted(n for n in names if n != "main"))

    mask_dir = root / "mask"
    use_mask = mask_dir.is_dir() and _has_images(mask_dir)
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
        "mask": str(mask_dir.resolve()) if use_mask else None,
        "num_repeats": repeats,
    }
