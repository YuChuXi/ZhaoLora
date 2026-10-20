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

import os
from dataclasses import dataclass
from pathlib import Path

from services.core.natural import natural_key

# 可浏览的图像后缀; 工作目录内的扫描与计数均以此为准
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})

# 任务根(ANNOTATOR_DATA_DIR, 如 datasets/)层的系统保留目录名; 任务名即任务根下
# 的一级目录名, 不得与之冲突
PACKED_DIR = "__packed__"   # 归档压缩包与预览(services/state/archiver)
LOG_DIRNAME = "__log__"     # 运行日志(server)
TASK_ROOT_RESERVED = frozenset({PACKED_DIR, LOG_DIRNAME})

# 每图裁剪输出记录目录名(datasets/xxx/.crops/{stem}.json, routes/crop.py 读写)
CROP_META_DIRNAME = ".crops"

# WD14 推理缓存目录名(datasets/xxx/{子目录}/.wd14_meta/{模型指纹}/{stem}.npy);
# 缓存整条输出概率向量, 随图片所在工作子目录存放(与 .{来源}_meta 同级),
# 数据集根不出现缓存目录; 指纹由权重与标签表内容共同决定, 模型变化即换目录
WD14_META_DIRNAME = ".wd14_meta"

# 回收站目录名(数据集根下, services/state/annotator.py 读写)
TRASH_DIRNAME = ".trash"

# 缩略图缓存目录名(数据集根下, services/core/thumbnailer.py 读写)
THUMBS_DIRNAME = ".thumbnails"

# 数据集根(datasets/xxx)内部的保留目录名: 不会被当成工作子目录或部位输出目录
RESERVED_DIR_NAMES = frozenset({
    "main",
    "mask",
    "crops",              # 未命名选区的存放目录
    "aesthetic_report",   # 美学报告
    "__pycache__",
    CROP_META_DIRNAME,
    WD14_META_DIRNAME,
    TRASH_DIRNAME,
    THUMBS_DIRNAME,
})

# 目录级自然语言描述文件名(datasets/xxx/{子目录}/captions.json); 键为图片文件名,
# 值为描述列表, 由 diffusion-pipe 直接作为目录级 caption 读取
CAPTIONS_JSON_FILE = "captions.json"

# 数据集级前端设置文件名(datasets/xxx/.annotator_settings.json)与全局缺省层
# 文件名(datasets/.annotator_settings.json); 各业务页的输入参数按页面/组件名
# 分键持久化, 由 routes/settings.py 读写
SETTINGS_FILENAME = ".annotator_settings.json"

# 视频目录名(数据集根下, 可为指向外部存储的符号链接)
VIDEO_DIRNAME = "video"

# 保留目录中不含可浏览图片的(蒙版/报告/缓存/回收站); 派生自 RESERVED_DIR_NAMES
NON_BROWSABLE_DIRS = RESERVED_DIR_NAMES - {"main", "crops"}


@dataclass(frozen=True)
class SidecarSource:
    """工作子目录内按图片 stem 存放产物文件的本地源.

    Attributes:
        name (str): 源名, 与在线图源同处一个命名空间(如 ``wd14``).
        meta_subdir (str): 该源在工作子目录内的产物目录名(如 ``.wd14_meta``).

    Callers:
        - ``services/dataset/layout.py:SIDECAR_SOURCES``
    """

    name: str
    meta_subdir: str


# 本地旁路源注册表: 产物目录位于图片所在工作子目录内, 图片移入回收站或恢复
# 时随行. 在线图源的 .{来源}_meta 由 services/online_sources 的 SOURCES 管理
SIDECAR_SOURCES: tuple[SidecarSource, ...] = (
    SidecarSource("wd14", WD14_META_DIRNAME),
)


def sidecar_paths(parent: Path, stem: str) -> list[Path]:
    """列出各本地源为指定图片保存的现存产物文件.

    Args:
        parent (Path): 图片所在工作子目录.
        stem (str): 图片文件名 stem.

    Returns:
        list[Path]: 产物文件路径列表(如 ``.wd14_meta/<模型指纹>/<stem>.npy``);
            对应源目录不存在时为空.

    Callers:
        - ``services/state/annotator.py:StateManager._move_meta_files``
        - ``services/online_sources/framework/media.py:remove_download``
    """
    out: list[Path] = []
    for src in SIDECAR_SOURCES:
        base = parent / src.meta_subdir
        if not base.is_dir():
            continue
        out.extend(p for p in base.rglob("*") if p.is_file() and p.stem == stem)
    return out


def resolve_under(root: Path, rel: str) -> Path:
    """将相对路径解析到 ``root`` 下的绝对路径.

    仅做词法规范化(消除 ``..``/``.``)后判定是否位于 ``root`` 之下, 不跟随
    符号链接. 这样既阻止 ``..`` 越界, 又允许数据集内指向外部存储的合法
    符号链接(如 ``video -> /mnt/...``).

    Args:
        root (Path): 允许访问的根目录.
        rel (str): 相对路径字符串.

    Returns:
        Path: 规范化绝对路径.

    Raises:
        ValueError: 空路径, 或解析结果不在 ``root`` 之下.

    Callers:
        - ``routes/deps.py:resolve_path``
        - ``services/state/mask.py:mask_out_path``
        - ``services/state/annotator.py:apply_delete`` / `restore` / `_move_meta_files`
        - ``services/core/video_frame.py:VideoFrameStore._video_path``
    """
    root = root.resolve()
    raw = (rel or "").replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError(f"非法路径: {rel!r}")
    text = raw.strip("/")
    if not text:
        raise ValueError(f"非法路径: {rel!r}")
    # os.path.normpath 是标准库唯一的纯词法规范化工具(消除 ``..``/``.``),
    # 不触碰文件系统, 因此不会跟随符号链接。pathlib 的 ``PurePath`` 不消除
    # ``..``, ``Path.resolve`` 会跟随符号链接, 均不适用。
    path = Path(os.path.normpath(str(root / text)))
    if not path.is_relative_to(root):
        raise ValueError(f"路径越界: {rel}")
    return path


def count_images(directory: Path) -> int:
    """统计目录内图像文件数(不递归).

    Args:
        directory (Path): 待统计目录.

    Returns:
        int: 图像文件数; 目录不存在时为 0.

    Callers:
        - ``services/dataset/layout.py:WorkDir.to_info``
        - ``services/dataset/runtime.py:Dataset.work_dirs``
        - ``services/dataset/runtime.py:Dataset.image_count``
    """
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


@dataclass(frozen=True)
class WorkDir:
    """数据集内的一个工作子目录.

    图片、旁路文件与各本地源产物都以工作子目录为单位存放. 下游持本对象
    而非裸路径: 名称、相对前缀与绝对路径均由对象派生, 路径相对数据集根的
    转换也由对象完成.

    Attributes:
        dataset (Dataset): 所属数据集.
        name (str): 一级子目录名; 数据集根本身为 ``"."``.
        base (Path | None): 承载目录; 为空时取数据集根. 回收站视图借此指向
            ``.trash`` 下的同名子目录, 而相对前缀不变.
    """

    dataset: "Dataset"
    name: str
    base: Path | None = None

    @property
    def path(self) -> Path:
        """该工作目录的绝对路径.

        Returns:
            Path: 承载目录本身(``name="."``)或 ``承载目录/name``.
        """
        base = self.base or self.dataset.root
        return base if self.name == "." else base / self.name

    def trashed(self) -> "WorkDir":
        """返回同名目录在回收站中的视图.

        Returns:
            WorkDir: 路径落在数据集回收站内的同一工作目录.

        Callers:
            - ``routes/images.py:list_images``
            - ``routes/annotations.py:select_all`` / `get_state`
        """
        return WorkDir(self.dataset, self.name, self.dataset.trash_dir)

    @property
    def prefix(self) -> str:
        """相对数据集根的路径前缀.

        Returns:
            str: 目录名; 数据集根为空字符串.
        """
        return "" if self.name == "." else self.name

    def to_info(self) -> "WorkDirInfo":
        """转前端目录选择器使用的摘要.

        Returns:
            WorkDirInfo: 目录名、展示标签与图片数.
        """
        from services.models import WorkDirInfo

        return WorkDirInfo(
            name=self.name,
            label="(根目录)" if self.name == "." else self.name,
            image_count=count_images(self.path),
        )

    def rel(self, path: Path | str) -> str:
        """绝对路径转相对数据集根的 POSIX 路径.

        Args:
            path (Path | str): 工作目录内的路径.

        Returns:
            str: 以 ``/`` 分隔的相对路径.
        """
        return Path(path).resolve().relative_to(self.dataset.root).as_posix()

    def images(self) -> list[WorkImage]:
        """扫描本工作目录内的图片及其同 stem 旁路文件.

        Returns:
            list[WorkImage]: 按 stem 自然序排列的图片.
        """
        return self.scan(self.path)

    def scan(self, directory: Path) -> list[WorkImage]:
        """扫描指定目录, 结果仍归属本工作目录.

        目录内扁平扫描, 不递归. 回收站内同名子目录亦经此扫描, 使图片的
        相对路径与在工作目录中一致.

        Args:
            directory (Path): 待扫描目录.

        Returns:
            list[WorkImage]: 按 stem 自然序排列的图片.
        """
        stem_map: dict[str, list[Path]] = {}
        if not directory.is_dir():
            return []
        for f in directory.iterdir():
            if f.is_file():
                stem_map.setdefault(f.stem, []).append(f)

        out: list[WorkImage] = []
        for stem, files in sorted(stem_map.items(), key=lambda x: natural_key(x[0])):
            image = next((f for f in files if f.suffix.lower() in IMAGE_EXTS), None)
            if image is None:
                continue
            siblings = tuple(
                sorted((f for f in files if f is not image), key=lambda p: natural_key(p.name))
            )
            out.append(WorkImage(self, image, siblings))
        return out

    def image_files(self, recursive: bool = False) -> list[Path]:
        """列出本工作目录内图像文件的绝对路径.

        Args:
            recursive (bool): 是否递归子目录; 默认扁平扫描.

        Returns:
            list[Path]: 按名称自然序排列的图像绝对路径.
        """
        if not self.path.is_dir():
            return []
        if recursive:
            found = (p for p in self.path.rglob("*") if p.is_file())
        else:
            found = (p for p in self.path.iterdir() if p.is_file())
        return sorted(
            (p for p in found if p.suffix.lower() in IMAGE_EXTS),
            key=lambda p: natural_key(p.name),
        )


@dataclass(frozen=True)
class WorkImage:
    """工作目录内的一张图片及其同 stem 旁路文件.

    Attributes:
        work (WorkDir): 图片所属工作目录.
        abs (Path): 图片绝对路径.
        siblings (tuple[Path, ...]): 同 stem 旁路文件的绝对路径.

    Callers:
        - ``services/dataset/layout.py:WorkDir.images``
    """

    work: "WorkDir"
    abs: Path
    siblings: tuple[Path, ...] = ()

    @property
    def rel(self) -> str:
        """相对数据集根的 POSIX 路径.

        Returns:
            str: 如 ``main/foo.png``; 数据集根下为 ``foo.png``.
        """
        prefix = self.work.prefix
        return f"{prefix}/{self.abs.name}" if prefix else self.abs.name

    @property
    def stem(self) -> str:
        """文件名 stem.

        Returns:
            str: 不含后缀的文件名.
        """
        return self.abs.stem

    @property
    def suffix(self) -> str:
        """小写后缀.

        Returns:
            str: 含点的小写后缀, 如 ``.png``.
        """
        return self.abs.suffix.lower()

    @property
    def size(self) -> int:
        """文件字节数.

        Returns:
            int: 文件大小.
        """
        return self.abs.stat().st_size

    @property
    def sibling_rels(self) -> list[str]:
        """旁路文件相对数据集根的 POSIX 路径.

        Returns:
            list[str]: 按名称自然序排列, 如 ``main/foo.txt``.
        """
        return [
            f"{self.work.prefix}/{p.name}" if self.work.prefix else p.name
            for p in self.siblings
        ]

