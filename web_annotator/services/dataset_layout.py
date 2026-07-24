"""
数据集目录约定。

--data-dir datasets/xxx 对应:

  datasets/xxx/
    main/     # 源图（筛选 / 绘制 / 裁剪输入）
    mask/     # alpha mask
    head/     # 部位裁剪输出（文件名与 main 同 stem）
    foot/
    ...       # 其它部位目录按选区名称自动创建

保留目录名不会被当成部位输出目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

RESERVED_DIR_NAMES = frozenset({
    "main",
    "mask",
    "crops",              # 未命名选区兜底
    "aesthetic_report",   # 美学报告
    ".trash",
    ".thumbs",
    ".thumbnails",
    "__pycache__",
})


@dataclass(frozen=True)
class DatasetLayout:
    root: Path          # datasets/xxx
    main_dir: Path      # datasets/xxx/main
    mask_dir: Path      # datasets/xxx/mask
    crops_dir: Path     # datasets/xxx/crops（未命名选区）

    def part_dir(self, name: str) -> Path:
        """部位输出目录：root/{name}/"""
        return self.root / name

    def list_part_dirs(self) -> list[Path]:
        """``list_part_dirs``。引用：``DatasetLayout``。"""
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir()):
            if p.is_dir() and p.name not in RESERVED_DIR_NAMES and not p.name.startswith("."):
                out.append(p)
        return out

    def stem_has_part_output(self, stem: str) -> bool:
        """任一部位目录或 crops 中已有同 stem 输出。"""
        candidates = [*self.list_part_dirs(), self.crops_dir]
        for d in candidates:
            if not d.is_dir():
                continue
            if (d / f"{stem}.png").is_file():
                return True
            for f in d.iterdir():
                if not f.is_file() or f.suffix.lower() != ".png":
                    continue
                if f.stem == stem or f.stem.startswith(stem + "_"):
                    return True
        return False


def resolve_dataset_layout(data_dir: Path) -> DatasetLayout:
    """
    解析 --data-dir。
    - datasets/xxx（含 main/）→ root=xxx, main=xxx/main
    - datasets/xxx/main → root=xxx, main=xxx/main
    """
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"目录不存在: {data_dir}")

    if (data_dir / "main").is_dir():
        root = data_dir
        main_dir = data_dir / "main"
    elif data_dir.name == "main" and data_dir.parent.is_dir():
        root = data_dir.parent
        main_dir = data_dir
    else:
        raise ValueError(
            f"--data-dir 应为 datasets/xxx（其下需有 main/），收到: {data_dir}"
        )

    return DatasetLayout(
        root=root,
        main_dir=main_dir,
        mask_dir=root / "mask",
        crops_dir=root / "crops",
    )
