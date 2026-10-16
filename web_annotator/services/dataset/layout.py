"""
数据集目录约定.

--data-dir datasets/xxx 对应:

  datasets/xxx/
    main/     # 源图(筛选 / 绘制 / 裁剪输入)
    mask/     # alpha mask
    video/    # 视频存放(切帧来源, 保存帧写入 main)
    head/     # 部位裁剪输出(文件名与 main 同 stem)
    foot/
    ...       # 其它部位目录按选区名称自动创建

保留目录名不会被当成部位输出目录.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.core.natural import natural_key

# 任务根(ANNOTATOR_DATA_DIR, 如 datasets/)层的系统保留目录名; 任务名即任务根下
# 的一级目录名, 不得与之冲突
PACKED_DIR = "__packed__"   # 归档压缩包与预览(services/state/archiver)
LOG_DIRNAME = "__log__"     # 运行日志(server)
TASK_ROOT_RESERVED = frozenset({PACKED_DIR, LOG_DIRNAME})

# 数据集根(datasets/xxx)内部的保留目录名, 不会被当成部位输出目录
RESERVED_DIR_NAMES = frozenset({
    "main",
    "mask",
    "crops",              # 未命名选区的存放目录
    ".crops",             # 每图裁剪输出记录(.crops/{stem}.json)
    "aesthetic_report",   # 美学报告
    ".trash",
    ".thumbs",
    ".thumbnails",
    "__pycache__",
})

# 每图裁剪输出记录目录名(datasets/xxx/.crops/{stem}.json, routes/crop.py 读写)
CROP_META_DIRNAME = ".crops"

# WD14 打标缓存目录名(datasets/xxx/.wd14_meta/{子目录}/{stem}.json); 按图片相对
# 路径镜像存放, 使不同子目录的同名图片各占一份缓存
WD14_META_DIRNAME = ".wd14_meta"

# 目录级自然语言描述文件名(datasets/xxx/{子目录}/captions.json); 键为图片文件名,
# 值为描述列表, 由 diffusion-pipe 直接作为目录级 caption 读取
CAPTIONS_JSON_FILE = "captions.json"

# 视频目录名(数据集根下, 可为指向外部存储的符号链接)
VIDEO_DIRNAME = "video"

# 保留目录中不含可浏览图片的(蒙版/报告/缓存/回收站); 派生自 RESERVED_DIR_NAMES
NON_BROWSABLE_DIRS = RESERVED_DIR_NAMES - {"main", "crops"}


@dataclass(frozen=True)
class DatasetLayout:
    root: Path          # datasets/xxx
    main_dir: Path      # datasets/xxx/main
    mask_dir: Path      # datasets/xxx/mask
    crops_dir: Path     # datasets/xxx/crops(未命名选区)
    crop_meta_dir: Path  # datasets/xxx/.crops(每图裁剪输出记录)

    def part_dir(self, name: str) -> Path:
        """部位输出目录: root/{name}/.

        Args:
            name (str): 部位目录名.

        Returns:
            Path: 部位目录绝对路径.

        Callers:
            - ``routes/crop.py:_resolve_out_path``
            - ``routes/crop.py:save_crops``
        """
        return self.root / name

    def list_part_dirs(self) -> list[Path]:
        """列出部位输出目录.

        video 目录是切帧来源的工作目录而非部位输出目录, 不在列.

        Returns:
            list[Path]: 部位目录绝对路径列表(按名称排序).

        Callers:
            - ``routes/crop.py:get_config``
            - ``routes/crop.py:get_crop_state``
        """
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir(), key=lambda p: natural_key(p.name)):
            if p.is_dir() and p.name not in RESERVED_DIR_NAMES and p.name != VIDEO_DIRNAME and not p.name.startswith("."):
                out.append(p)
        return out


def resolve_dataset_layout(data_dir: Path) -> DatasetLayout:
    """解析数据集根目录, 自动确保 ``main/`` 与 ``mask/`` 存在.

    Args:
        data_dir (Path): 数据集根目录(``datasets/xxx``).

    Returns:
        DatasetLayout: 解析后的布局, 含 root / main_dir / mask_dir / crops_dir / crop_meta_dir.

    Raises:
        FileNotFoundError: 目录不存在.

    Callers:
        - ``services/dataset/runtime.py:DatasetRegistry.ensure``
    """
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"目录不存在: {data_dir}")

    main_dir = data_dir / "main"
    main_dir.mkdir(parents=True, exist_ok=True)
    mask_dir = data_dir / "mask"
    mask_dir.mkdir(parents=True, exist_ok=True)

    return DatasetLayout(
        root=data_dir,
        main_dir=main_dir,
        mask_dir=mask_dir,
        crops_dir=data_dir / "crops",
        crop_meta_dir=data_dir / CROP_META_DIRNAME,
    )
