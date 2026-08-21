"""生成训练用 datasets TOML 片段, 并按子目录统计蒙版状态. """

from __future__ import annotations

from pathlib import Path

from services.state.mask import has_mask
from services.models import MaskStats, ReadySubset, ReadyTomlResult
from services.core.scanner import IMAGE_EXTS, scan_images
from services.core.work_dir import list_work_dirs, total_image_count


def _has_images(d: Path) -> bool:
    """目录内是否至少有一张图. 引用: ``build_ready_toml``. """
    if not d.is_dir():
        return False
    return any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in d.iterdir())


def subdir_mask_stats(root: Path, subdir: str = "main") -> MaskStats:
    """
    统计指定子目录的蒙版状态: 每张图仅「无」或「完整」. 

    整体 status: 全部有蒙版 →「完整」；否则 →「无」. 
    引用: ``dirs``、``build_ready_toml``；``routes/ready.py`` 侧栏. 
    """
    root = root.resolve()
    subdir = (subdir or "main").strip().strip("/")
    image_dir = root / subdir
    mask_dir = root / "mask"
    images = scan_images(image_dir, path_prefix=subdir) if image_dir.is_dir() else []
    complete = 0
    none = 0
    for img in images:
        if has_mask(mask_dir, img.path):
            complete += 1
        else:
            none += 1
    total = complete + none
    status = "完整" if total > 0 and none == 0 else "无"
    return MaskStats(
        name=subdir,
        mask_status=status,
        mask_complete=complete,
        mask_none=none,
        main_count=total,
    )


def build_ready_toml(
    root: Path,
    *,
    dirs: list[str] | None = None,
    target_images: int = 512,
    resolutions: list[int] | None = None,
) -> ReadyTomlResult:
    """生成 TOML 文本与 subset 元数据.

    某子目录蒙版状态为「完整」时, 对应 subset 附带 conditioning_data_dir(指向 mask/子目录);
    按是否带 conditioning_data_dir 分组, 每组渲染为一个 [[datasets]] 块.
    传入 ``resolutions`` 时, 按每个分辨率 × 每组各渲染一个 [[datasets]] 块,
    块内首行 ``resolution = N``; 不传则保持单块且不带 resolution 行.

    ``target_images`` 为目标图片数: 据此计算总重复次数 ``total_repeats = ceil(target_images / 总图片数)``,
    再按分辨率块数均分: base = floor(total_repeats / block_count), remainder 个 +1
    优先分配给最靠近 1024 的分辨率(同距则高分辨率优先), 使跨所有块的总重复图片数 >= target_images;
    不传分辨率时块数为 1, num_repeats = total_repeats. 总图片数为 0 时 total_repeats 取 1.

    引用: ``routes/ready.py``.
    """
    root = root.resolve()
    available = {d.name: d for d in list_work_dirs(root)}
    if dirs:
        names = [n for n in dirs if n in available]
    else:
        names = list(available.keys())

    ordered: list[str] = []
    if "main" in names:
        ordered.append("main")
    ordered.extend(sorted(n for n in names if n != "main"))

    # 先确定有图片的目录并统计总图片数(复用统一的任务图数口径)
    valid_names = [n for n in ordered if _has_images((root / n).resolve())]
    total = total_image_count(root)

    # 过滤非法分辨率(须为正整数), 保留输入顺序
    res_list = [r for r in (resolutions or []) if isinstance(r, int) and r > 0]
    if not res_list:
        res_list = [None]

    # total_repeats = ceil(target / total): 跨所有分辨率块的总重复次数
    # 按 block_count 均分: base = floor(total_repeats/block_count), remainder 个 +1
    # 优先分配给最靠近 1024 的分辨率, 同距则高分辨率优先
    block_count = len(res_list)
    total_repeats = max(1, -(-target_images // total)) if total else 1
    base = total_repeats // block_count
    remainder = total_repeats % block_count

    def _res_priority(i: int) -> tuple[int, int]:
        """分辨率块 i 的 +1 优先级: (距 1024 的绝对差, 负分辨率), 越小越优先."""
        r = res_list[i]
        return (abs(r - 1024), -r) if r is not None else (0, 0)

    order = sorted(range(block_count), key=_res_priority)
    res_repeats = [base] * block_count
    for i in order[:remainder]:
        res_repeats[i] += 1

    subsets: list[ReadySubset] = []
    mask_stats: list[MaskStats] = []
    for name in valid_names:
        image_dir = (root / name).resolve()
        info = available[name]
        entry = ReadySubset(
            name=name,
            image_dir=str(image_dir),
            image_count=info.image_count,
        )
        mstats = subdir_mask_stats(root, name)
        mask_stats.append(mstats)
        if mstats.mask_status == "完整":
            entry.conditioning_data_dir = str((root / "mask" / name).resolve())
        subsets.append(entry)

    # 按是否带 conditioning_data_dir 分组(带蒙版在前), 组内保持原顺序;
    # 每个组内按分辨率渲染 [[datasets]] 块, 块内 num_repeats 取该分辨率的 res_repeats
    groups = [
        [s for s in subsets if s.conditioning_data_dir],
        [s for s in subsets if not s.conditioning_data_dir],
    ]
    blocks: list[str] = []
    for group in groups:
        if not group:
            continue
        for res_idx, res in enumerate(res_list):
            lines = ["[[datasets]]"]
            if res is not None:
                lines.append(f"resolution = {res}")
            rep = res_repeats[res_idx]
            for s in group:
                lines.append("  [[datasets.subsets]]")
                lines.append(f'  image_dir = "{s.image_dir}"')
                if s.conditioning_data_dir:
                    lines.append(f'  conditioning_data_dir = "{s.conditioning_data_dir}"')
                lines.append(f"  num_repeats = {rep}")
            blocks.append("\n".join(lines))

    toml = "\n\n".join(blocks)
    if toml:
        toml += "\n"
    main_ms = next((m for m in mask_stats if m.name == "main"),
                   MaskStats(name="main", mask_status="无"))
    return ReadyTomlResult(
        root=str(root),
        toml=toml,
        subsets=subsets,
        dirs=list(available.values()),
        mask_stats=mask_stats,
        mask=str((root / "mask").resolve()) if main_ms.mask_status == "完整" else None,
        target_images=target_images,
        repeats=res_repeats,
        resolutions=[r for r in res_list if r is not None],
        mask_status=main_ms.mask_status,
        mask_complete=main_ms.mask_complete,
        mask_none=main_ms.mask_none,
        main_count=main_ms.main_count,
    )
