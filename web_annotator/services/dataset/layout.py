"""
数据集目录约定. 

--data-dir datasets/xxx 对应:

  datasets/xxx/
    main/     # 源图(筛选 / 绘制 / 裁剪输入)
    mask/     # alpha mask
    head/     # 部位裁剪输出(文件名与 main 同 stem)
    foot/
    ...       # 其它部位目录按选区名称自动创建

保留目录名不会被当成部位输出目录. 
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

# 保留目录中不含可浏览图片的(蒙版/报告/缓存/回收站); 派生自 RESERVED_DIR_NAMES
NON_BROWSABLE_DIRS = RESERVED_DIR_NAMES - {"main", "crops"}


@dataclass(frozen=True)
class DatasetLayout:
    root: Path          # datasets/xxx
    main_dir: Path      # datasets/xxx/main
    mask_dir: Path      # datasets/xxx/mask
    crops_dir: Path     # datasets/xxx/crops(未命名选区)

    def part_dir(self, name: str) -> Path:
        """部位输出目录: root/{name}/"""
        return self.root / name

    def list_part_dirs(self) -> list[Path]:
        """列出部位输出目录. 引用: routes.crop. """
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir()):
            if p.is_dir() and p.name not in RESERVED_DIR_NAMES and not p.name.startswith("."):
                out.append(p)
        return out

    def stem_has_part_output(self, stem: str) -> bool:
        """任一部位目录或 crops 中已有同 stem 输出. """
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
    解析数据集根目录, 自动确保 ``main/`` 与 ``mask/`` 存在. 

    Args:
        data_dir: 数据集根目录(``datasets/xxx``). 

    引用: ``services/dataset/runtime.py:DatasetRegistry.ensure``. 
    """
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"目录不存在: {data_dir}")

    main_dir = data_dir / "main"
    main_dir.mkdir(parents=True, exist_ok=True)
    mask_dir = data_dir / "mask"
    mask_dir.mkdir(parents=True, exist_ok=True)
    _migrate_flat_masks(mask_dir)

    return DatasetLayout(root=data_dir, main_dir=main_dir, mask_dir=mask_dir, crops_dir=data_dir / "crops")


def _migrate_flat_masks(mask_dir: Path) -> None:
    """把 mask/ 下平铺的 PNG 迁入 mask/main/(统一带子目录前缀).

    蒙版路径统一为 ``mask/<子目录>/<文件>.png``, 历史平铺的 main 蒙版
    (``mask/<文件>.png``) 在此迁移.

    Callers:
        - ``resolve_dataset_layout``
    """
    main_mask = mask_dir / "main"
    if not mask_dir.is_dir():
        return
    main_mask.mkdir(parents=True, exist_ok=True)
    for p in mask_dir.iterdir():
        if p.is_file() and p.suffix.lower() == ".png":
            dest = main_mask / p.name
            if not dest.exists():
                p.rename(dest)
