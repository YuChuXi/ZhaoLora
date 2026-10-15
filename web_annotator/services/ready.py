"""生成训练用配置文本, 并按子目录统计蒙版状态.

各导出格式的配置文本由 ``assets/configs/<格式名>/`` 下的 Jinja2 模板渲染: 模板目录名
即格式名, 目录内 ``*.j2`` 去掉后缀即该格式的配置文件名; 每个模板需要哪些变量由其自身
声明决定(``jinja2.meta.find_undeclared_variables`` 解析其语法树), 再从 ``_VARIABLES``
按变量名取数据。因此新增格式或新增模板文件只需在 ``assets/configs/`` 新建模板, 并在
``.env`` 中按 ``<格式大写下划线>_CONFIG_ROOT`` 约定命名位置变量, 本模块无需登记任何
格式、模板或环境变量的对应关系。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import jinja2
from jinja2.meta import find_undeclared_variables
from jinja2.sandbox import ImmutableSandboxedEnvironment

from services.core.natural import natural_key
from services.state.mask import has_mask
from services.models import (
    LoraMode,
    MaskStats,
    ReadySubset,
    ReadyTomlResult,
    WorkDirInfo,
)
from services.core.scanner import IMAGE_EXTS, scan_images
from services.core.work_dir import list_work_dirs
from services.tagging.clean import tag_key
from services.tagging.stats import count_tags


# ---- 模板渲染(assets/ 下的 Jinja2 文件) ----

# 配置模板的渲染环境:
# - 沙箱: 模板只能访问传入的变量与只读能力, 无法经属性链逃逸到 os/filesystem(默认
#   ``Environment`` 可经 ``cycler.__init__.__globals__`` 之类读到任意对象, 而声明式变量
#   检查对这类载荷无效), 也不能就地修改传入的列表/字典;
# - 关闭自动转义(TOML 为纯文本); trim/lstrip_blocks 使独占一行的控制标签不产生空行
_JINJA_ENV = ImmutableSandboxedEnvironment(
    loader=jinja2.FileSystemLoader(Path(__file__).resolve().parent.parent / "assets"),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)

# web_annotator 应用根目录(server.py 所在目录), 用于把相对的路径类环境变量锚定到项目内
_APP_DIR = Path(__file__).resolve().parents[1]

# 各格式模板所在目录(``assets/configs/``); 其下每个子目录即一种配置导出格式
_TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "assets" / "configs"

# 各格式的模板清单: 格式名(模板目录名) → {配置文件名: 模板相对路径}. 预览键、导出请求键
# 校验与前端标签页均以配置文件名集合为准, 故新增模板文件即自动生效, 代码不登记文件名单
TRAIN_FORMAT_TEMPLATES: dict[str, dict[str, str]] = {
    d.name: {p.stem: f"configs/{d.name}/{p.name}" for p in sorted(d.glob("*.j2"))}
    for d in _TEMPLATE_ROOT.iterdir() if d.is_dir()
}


def _env_root(key: str) -> Path:
    """读取指向根目录的环境变量并解析为绝对路径.

    相对路径按应用根目录 ``web_annotator/`` 锚定.

    Args:
        key (str): 环境变量名.

    Returns:
        Path: 解析后的绝对根目录.

    Raises:
        ValueError: 变量未配置或值为空.

    Callers:
        - ``services/ready.py:export_dir``
        - ``services/ready.py:output_dir``
    """
    value = os.environ.get(key, "").strip()
    if not value:
        raise ValueError(f"未配置 {key}(见 .env.example)")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (_APP_DIR / path).resolve()


def _format_env_key(train_format: str, kind: str) -> str:
    """按格式名推导该格式的位置类环境变量名 ``<格式大写下划线>_<kind>``.

    格式名即 ``assets/configs/`` 下的模板目录名, 故新增格式只需新建模板目录并在 ``.env``
    中按同一约定命名变量(见 .env.example), 本模块不登记格式与环境变量的对应关系.

    Args:
        train_format (str): 配置导出格式, 即模板目录名.
        kind (str): 变量用途后缀, 如 ``CONFIG_ROOT`` / ``OUTPUT_ROOT``.

    Returns:
        str: 环境变量名.

    Callers:
        - ``services/ready.py:export_dir``
        - ``services/ready.py:output_dir``
    """
    return f"{train_format.upper().replace('-', '_')}_{kind}"


def export_dir(root_name: str, train_format: str) -> Path:
    """返回数据集的配置导出目录 ``{格式对应配置根}/{数据集名}``.

    配置根取 ``{格式名}_CONFIG_ROOT`` 环境变量; 渲染配置与导出写入共用本函数, 保证文本
    内的路径与真实写入位置一致.

    Args:
        root_name (str): 数据集名, 作为导出目录名.
        train_format (str): 配置导出格式, 即模板目录名.

    Returns:
        Path: 配置导出目录的绝对路径.

    Raises:
        ValueError: 对应配置根环境变量未配置.

    Callers:
        - ``services/ready.py:_config_dir``
        - ``routes/ready.py:export``
    """
    return _env_root(_format_env_key(train_format, "CONFIG_ROOT")) / root_name


def output_dir(root_name: str, train_format: str) -> Path:
    """返回该格式的训练产物目录 ``{格式对应产物根}/{数据集名}``.

    产物根取 ``{格式名}_OUTPUT_ROOT`` 环境变量。目录按数据集名分列, 避免多个数据集在同一
    产物根下用相同的 epoch/step 子目录名互相覆盖。仅声明 ``output_dir`` 变量的模板会取用
    本函数, 以相对工作目录写产物的模板不受影响。

    Args:
        root_name (str): 数据集名, 作为产物目录名.
        train_format (str): 配置导出格式, 即模板目录名.

    Returns:
        Path: 训练产物目录的绝对路径.

    Raises:
        ValueError: 对应产物根环境变量未配置.

    Callers:
        - ``services/ready.py:_artifact_dir``
    """
    return _env_root(_format_env_key(train_format, "OUTPUT_ROOT")) / root_name


def reg_datasets() -> list[tuple[int, str]]:
    """解析环境变量 ``REG_DATASETS`` 得到正则化数据集列表.

    ``REG_DATASETS`` 格式为逗号分隔的 ``分辨率:目录绝对路径`` 项(目录内图片须携带
    类提示词 caption, 不含触发词), 如
    ``1536:/data/datasets/regL/256_1,1024:/data/datasets/regL/256_2``;
    由 ``.env`` 配置(见 .env.example), 代码中不持有任何路径常量.

    Args:
        None: 无入参.

    Returns:
        list[tuple[int, str]]: (分辨率, 目录路径) 列表, 保持配置顺序.

    Raises:
        ValueError: 环境变量未配置/为空, 或某项缺少分辨率前缀、分辨率非正整数.

    Callers:
        - ``services/ready.py:_group_datasets``
        - ``services/ready.py:_directory_entries``
    """
    raw = os.environ.get("REG_DATASETS", "").strip()
    if not raw:
        raise ValueError("已开启「正则化」但未配置环境变量 REG_DATASETS(见 .env.example); 或关闭该开关")
    entries: list[tuple[int, str]] = []
    for part in raw.split(","):
        part = part.strip()
        res_text, sep, path = part.partition(":")
        if not sep or not res_text.strip().isdigit() or int(res_text) <= 0 or not path.strip():
            raise ValueError(f"REG_DATASETS 条目无效: {part!r}, 须为 分辨率:目录绝对路径")
        entries.append((int(res_text), path.strip()))
    return entries


# 各训练模式的网络规模: network_dim = network_alpha = rank
_LORA_RANKS: dict[LoraMode, int] = {"style": 16, "character": 32}


def _has_images(d: Path) -> bool:
    """目录内是否至少有一张图.

    Args:
        d (Path): 待检查的目录.

    Returns:
        bool: 目录内存在图片文件则为 True.
    Callers:
        - ``services/ready.py:_selected_names``
    """
    if not d.is_dir():
        return False
    return any(p.is_file() and p.suffix.lower() in IMAGE_EXTS for p in d.iterdir())


def subdir_mask_stats(root: Path, subdir: str = "main") -> MaskStats:
    """统计指定子目录的就绪状态: 图片 / 标注 / 蒙版计数与就绪判定.

    标注与蒙版均按图片逐张判定, 仅统计有对应图片的条目;
    整体 mask_status: 全部有蒙版 →「完整」；否则 →「无」;
    ready: 标注全部存在, 且蒙版全部存在或全无.

    Args:
        root (Path): 数据集根目录.
        subdir (str): 子目录名, 默认 ``main``.

    Returns:
        MaskStats: 含 main_count / text_count / mask_complete / mask_none / ready 的统计.

    Callers:
        - ``routes/ready.py:dirs``
        - ``services/ready.py:_build_plan``
    """
    root = root.resolve()
    subdir = (subdir or "main").strip().strip("/")
    image_dir = root / subdir
    mask_dir = root / "mask"
    images = scan_images(image_dir, path_prefix=subdir) if image_dir.is_dir() else []
    complete = 0
    none = 0
    text_count = 0
    for img in images:
        if any(s.endswith(".txt") for s in img.siblings):
            text_count += 1
        if has_mask(mask_dir, img.path):
            complete += 1
        else:
            none += 1
    total = complete + none
    status = "完整" if total > 0 and none == 0 else "无"
    return MaskStats(
        name=subdir,
        mask_status=status,
        main_count=total,
        text_count=text_count,
        mask_complete=complete,
        mask_none=none,
        ready=total > 0 and text_count == total and (complete == total or complete == 0),
    )


def _alloc_repeats(res_list: list[int | None], total_repeats: int) -> list[int]:
    """把子集级总重复次数均分到该子集的各分辨率块.

    均分规则: base = floor(total_repeats / 块数), remainder 个 +1,
    优先分配给最靠近 1024 的分辨率(同距则高分辨率优先).

    Args:
        res_list (list[int | None]): 该子集的分辨率列表, None 表示不带 resolution 行的块.
        total_repeats (int): 子集级总重复次数, 须 >= len(res_list).

    Returns:
        list[int]: 各分辨率块的 num_repeats(按 res_list 顺序), 每项 >= 1.

    Callers:
        - ``services/ready.py:_build_plan``
    """
    n = len(res_list)
    base, remainder = divmod(total_repeats, n)

    def _priority(i: int) -> tuple[int, int]:
        """分辨率块 i 的 +1 优先级: (距 1024 的绝对差, 负分辨率), 越小越优先.

        Args:
            i (int): 分辨率块索引.

        Returns:
            tuple[int, int]: ``(距 1024 的绝对差, 负分辨率)``; 无分辨率时为 ``(0, 0)``.
        """
        r = res_list[i]
        return (abs(r - 1024), -r) if r is not None else (0, 0)

    reps = [base] * n
    for i in sorted(range(n), key=_priority)[:remainder]:
        reps[i] += 1
    return reps


def _top_caption_tags(root: Path, names: list[str], trigger_word: str) -> list[str]:
    """统计参与导出子集标注文件中出现次数最多的标签(按样本去重计数).

    标签解析与归一化口径与标签统计页一致(``count_tags``), 触发词自身从结果中
    排除以免与单独写入的触发词重复.

    Args:
        root (Path): 数据集根目录, 标注文件位于各子集目录旁路 ``.txt``.
        names (list[str]): 参与导出的工作目录名.
        trigger_word (str): 该数据集触发词, 用于排除自身.

    Returns:
        list[str]: 出现次数最多的标签, 最多 10 个, 按频率降序.

    Callers:
        - ``services/ready.py:_prompt_head``
    """
    result = count_tags([root / n for n in names], per_sample=True)
    trigger_key = tag_key(trigger_word) if trigger_word else None
    return [
        r.tag for r in result.tags
        if trigger_key is None or tag_key(r.tag) != trigger_key
    ][:10]


# ---- 目录计划: 参与导出的目录及其分辨率与重复次数(与配置格式无关) ----

@dataclass(frozen=True)
class _DirPlan:
    """单个工作目录的导出计划.

    Attributes:
        stats (MaskStats): 该目录的图片/标注/蒙版计数与就绪状态.
        image_dir (str): 图片目录绝对路径.
        mask_dir (str | None): 蒙版目录绝对路径; 蒙版状态非「完整」时为 None.
        image_count (int): 图片数.
        resolutions (list[int | None]): 该目录的分辨率列表; None 表示不带分辨率.
        repeats (list[int]): 与 ``resolutions`` 一一对应的 num_repeats.
    """

    stats: MaskStats
    image_dir: str
    mask_dir: str | None
    image_count: int
    resolutions: list[int | None]
    repeats: list[int]

    @property
    def name(self) -> str:
        """目录名.

        Returns:
            str: 目录名(即 ``stats.name``).
        """
        return self.stats.name


def _selected_names(root: Path, available: dict[str, WorkDirInfo], dirs: list[str] | None) -> list[str]:
    """按 ``main`` 优先 + 自然序挑出参与导出的目录名, 仅保留实际存在且有图片的目录.

    Args:
        root (Path): 数据集根目录(已解析).
        available (dict[str, WorkDirInfo]): 数据集内含图片的工作目录, 键为目录名.
        dirs (list[str] | None): 参与导出的目录名; None 表示全部.

    Returns:
        list[str]: 目录名列表.

    Callers:
        - ``services/ready.py:exportable_dirs``
        - ``services/ready.py:_build_plan``
    """
    names = [n for n in dirs if n in available] if dirs is not None else list(available)
    ordered = ["main"] if "main" in names else []
    ordered.extend(sorted((n for n in names if n != "main"), key=natural_key))
    return [n for n in ordered if _has_images((root / n).resolve())]


def exportable_dirs(root: Path, dirs: list[str] | None = None) -> list[str]:
    """返回参与导出且有图片的工作目录名(``main`` 优先, 其余按自然序).

    Args:
        root (Path): 数据集根目录.
        dirs (list[str] | None): 参与导出的工作目录名; None 表示全部.

    Returns:
        list[str]: 目录名列表; 无可导出目录时为空.

    Callers:
        - ``routes/ready.py:export``
    """
    root = root.resolve()
    return _selected_names(root, {d.name: d for d in list_work_dirs(root)}, dirs)


def _build_plan(ctx: "_ExportContext") -> list[_DirPlan]:
    """构造函数无关的目录计划: 每个参与导出的目录一项.

    子集级总重复次数取 ``max(该目录分辨率块数, ceil(target_images / 参与导出目录图片总数))``
    (参与导出图片总数为 0 时取块数)以保证每个分辨率块 num_repeats >= 1, 再经
    ``_alloc_repeats`` 均分到各分辨率块。分辨率取侧栏输入(去重、去非法值); 留空时该目录
    只有一个不分块的条目, 渲染方式由模板决定(省略 resolution 键, 或按模板内的默认分辨率
    训练)。

    Args:
        ctx (_ExportContext): 本次生成的输入.

    Returns:
        list[_DirPlan]: 各目录的导出计划(不含正则化数据集).

    Callers:
        - ``services/ready.py:_ExportContext.plan``
    """
    available = {d.name: d for d in ctx.work_dirs()}
    selected = [available[n] for n in _selected_names(ctx.root, available, ctx.dirs)]
    total = sum(d.image_count for d in selected)
    per_target = -(-ctx.target_images // total) if total else 1

    plan: list[_DirPlan] = []
    for work_dir in selected:
        # 目录内同一分辨率只保留首个, 重复值无独立语义
        resolutions: list[int | None] = list(dict.fromkeys(
            r for r in ctx.subset_resolutions.get(work_dir.name, [])
            if isinstance(r, int) and r > 0
        )) or [None]
        stats = subdir_mask_stats(ctx.root, work_dir.name)
        plan.append(_DirPlan(
            stats=stats,
            image_dir=str((ctx.root / work_dir.name).resolve()),
            mask_dir=str((ctx.root / "mask" / work_dir.name).resolve())
            if stats.mask_status == "完整" else None,
            image_count=work_dir.image_count,
            resolutions=resolutions,
            repeats=_alloc_repeats(resolutions, max(len(resolutions), per_target)),
        ))
    return plan


@dataclass
class _ExportContext:
    """单次配置生成的输入与派生数据(模板变量取值的唯一来源).

    Attributes:
        root (Path): 数据集根目录(已解析).
        mode (LoraMode): 训练模式.
        train_format (str): 配置导出格式(即模板目录名).
        dirs (list[str] | None): 参与导出的工作目录名; None 表示全部.
        target_images (int): 目标图片数.
        subset_resolutions (dict[str, list[int]]): 目录名→分辨率列表.
        use_reg (bool): 是否附带正则化数据段.
        trigger_word (str): 数据集触发词.
    """

    root: Path
    mode: LoraMode
    train_format: str
    dirs: list[str] | None
    target_images: int
    subset_resolutions: dict[str, list[int]]
    use_reg: bool
    trigger_word: str
    _work_dirs: list[WorkDirInfo] | None = field(default=None, repr=False)
    _plan: list[_DirPlan] | None = field(default=None, repr=False)

    def work_dirs(self) -> list[WorkDirInfo]:
        """数据集内含图片的工作目录(``main`` 置顶), 记忆化以避免重复扫描.

        Returns:
            list[WorkDirInfo]: 工作目录列表.

        Callers:
            - ``services/ready.py:_build_plan``
            - ``services/ready.py:_prompt_head``
            - ``services/ready.py:build_ready``
        """
        if self._work_dirs is None:
            self._work_dirs = list_work_dirs(self.root)
        return self._work_dirs

    def plan(self) -> list[_DirPlan]:
        """本格式的目录计划(参与导出的目录及其分辨率与 num_repeats), 记忆化.

        Returns:
            list[_DirPlan]: 目录计划.

        Callers:
            - ``services/ready.py:_group_datasets``
            - ``services/ready.py:_directory_entries``
            - ``services/ready.py:build_ready``
        """
        if self._plan is None:
            self._plan = _build_plan(self)
        return self._plan


# 模板变量: 变量名 → 取值函数(模板声明需要哪些变量, 渲染时就取哪些)


def _group_datasets(ctx: _ExportContext) -> list[dict]:
    """装配 ``groups`` 变量: 数据块及其中的子集.

    以 (分辨率, 是否携带蒙版目录) 为键分组, 每组一个数据块: 分辨率按首次出现顺序, 同一
    分辨率下携带蒙版目录的子集自成一类 —— 部分训练框架仅当块内全部子集都携带
    conditioning 目录时才按蒙版数据集校验, 两类不得混入同一块; 不带分辨率的子集归入
    不带分辨率行的块. ``use_reg`` 为真时追加正则化数据(caption 为类提示词, num_repeats
    固定 1).

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        list[dict]: 每项 ``{"resolution": int | None, "subsets": [...]}``.

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    blocks: dict[tuple[int | None, bool], list[dict]] = {}
    for entry in ctx.plan():
        for resolution, repeats in zip(entry.resolutions, entry.repeats):
            blocks.setdefault((resolution, entry.mask_dir is not None), []).append({
                "image_dir": entry.image_dir,
                "conditioning_data_dir": entry.mask_dir,
                "repeats": repeats,
            })

    order: dict[int | None, int] = {}
    for key in blocks:
        order.setdefault(key[0], len(order))
    groups = [
        {"resolution": key[0], "subsets": blocks[key]}
        for key in sorted(blocks, key=lambda k: (order[k[0]], not k[1]))
    ]
    if ctx.use_reg:
        for resolution, path in reg_datasets():
            groups.append({
                "resolution": resolution,
                "subsets": [{"image_dir": path, "conditioning_data_dir": None, "repeats": 1}],
            })
    return groups


def _directory_entries(ctx: _ExportContext) -> list[dict]:
    """装配 ``directories`` 变量: 每个目录一项及其逐分辨率重复次数.

    仅装配数据 —— 每个参与导出的目录给出 ``path`` / ``mask_path`` /
    ``resolution_repeats``(逐分辨率的 num_repeats); 归并成目录块的规则(按 num_repeats
    分组, 同值的分辨率合并进该块的 resolutions 列表, 某目录在不同分辨率上取值不同时
    输出多个同 path 块)由模板决定。``use_reg`` 为真时追加正则化目录(num_repeats 固定 1),
    reg 图以类提示词 caption 参与同一损失计算, 效果即先验保持。

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        list[dict]: 每项 ``{"path": str, "mask_path": str | None, "resolution_repeats": [...]}``.

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    directories = [
        {
            "path": entry.image_dir,
            "mask_path": entry.mask_dir,
            "resolution_repeats": [
                {"resolution": resolution, "repeats": repeats}
                for resolution, repeats in zip(entry.resolutions, entry.repeats)
            ],
        }
        for entry in ctx.plan()
    ]
    if ctx.use_reg:
        for resolution, path in reg_datasets():
            directories.append({
                "path": path,
                "mask_path": None,
                "resolution_repeats": [{"resolution": resolution, "repeats": 1}],
            })
    return directories


def _network_rank(ctx: _ExportContext) -> int:
    """装配 ``rank`` 变量: 该模式的 LoRA 网络规模(供模板写 network_dim / network_alpha).

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        int: 网络规模(画风 16 / 角色 32).

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    return _LORA_RANKS[ctx.mode]


def _prompt_head(ctx: _ExportContext) -> str:
    """装配 ``head`` 变量: 采样提示词的起始段.

    触发词(可为空) + 参与导出目录中出现次数最多的标签(最多 10 个)拼接而成.

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        str: 以 ``", "`` 分隔的标签串; 无内容时为空串.

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    names = ctx.dirs if ctx.dirs is not None else [d.name for d in ctx.work_dirs()]
    top = _top_caption_tags(ctx.root, names, ctx.trigger_word)
    return ", ".join(t for t in [ctx.trigger_word, *top] if t)


def _dataset_name(ctx: _ExportContext) -> str:
    """装配 ``root_name`` 变量: 数据集名, 供模板派生文件名与运行名.

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        str: 数据集名(数据集根目录名).

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    return ctx.root.name


def _config_dir(ctx: _ExportContext) -> str:
    """装配 ``export_dir`` 变量: 本格式配置导出的绝对目录.

    供模板引用同目录下的兄弟配置文件(如 ``{{ export_dir }}/dataset.toml``).

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        str: 配置导出目录的绝对路径.

    Raises:
        ValueError: 本格式的配置根环境变量未配置.

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    return str(export_dir(ctx.root.name, ctx.train_format))


def _artifact_dir(ctx: _ExportContext) -> str:
    """装配 ``output_dir`` 变量: 本格式训练产物的绝对目录.

    仅声明该变量的模板会取用, 因此未配置产物根环境变量的格式不受影响.

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        str: 训练产物目录的绝对路径.

    Raises:
        ValueError: 本格式的产物根环境变量未配置.

    Callers:
        - ``services/ready.py:_render_files`` (经 ``_VARIABLES`` 按变量名调用)
    """
    return str(output_dir(ctx.root.name, ctx.train_format))


_VARIABLES: dict[str, Callable[[_ExportContext], object]] = {
    "groups": _group_datasets,
    "directories": _directory_entries,
    "rank": _network_rank,
    "head": _prompt_head,
    "root_name": _dataset_name,
    "export_dir": _config_dir,
    "output_dir": _artifact_dir,
}


def _declared_variables(template_path: str) -> set[str]:
    """解析模板语法树, 得到其声明使用且未在模板内部定义的变量名集合.

    Args:
        template_path (str): 模板相对 ``assets/`` 的路径, 如 ``configs/<格式>/lora.toml.j2``.

    Returns:
        set[str]: 变量名集合.

    Callers:
        - ``services/ready.py:_render_files``
    """
    source = _JINJA_ENV.loader.get_source(_JINJA_ENV, template_path)[0]
    return find_undeclared_variables(_JINJA_ENV.parse(source))


def _render_files(ctx: _ExportContext) -> dict[str, str]:
    """渲染该格式模板目录下的全部模板.

    每个模板需要哪些变量由其自身声明决定, 再按 ``_VARIABLES`` 取对应数据, 故新增模板
    文件即自动纳入渲染, 本模块不登记模板名单; 渲染结果即导出写入的文本(导出不再改写
    任何字段).

    Args:
        ctx (_ExportContext): 本次生成的输入与派生数据.

    Returns:
        dict[str, str]: 配置文件名 → 配置文本.

    Raises:
        ValueError: 模板声明了 ``_VARIABLES`` 中未登记的变量.

    Callers:
        - ``services/ready.py:build_ready``
    """
    files: dict[str, str] = {}
    for name, path in TRAIN_FORMAT_TEMPLATES[ctx.train_format].items():
        needed = _declared_variables(path)
        unknown = needed - _VARIABLES.keys()
        if unknown:
            raise ValueError(
                f"{path} 声明了未登记的变量 {sorted(unknown)}; "
                "请在 services/ready.py:_VARIABLES 中补充取值函数"
            )
        files[name] = _JINJA_ENV.get_template(path).render(
            **{variable: _VARIABLES[variable](ctx) for variable in sorted(needed)}
        )
    return files


def build_ready(
    root: Path,
    *,
    mode: LoraMode,
    train_format: str,
    dirs: list[str] | None = None,
    target_images: int = 512,
    subset_resolutions: dict[str, list[int]] | None = None,
    use_reg: bool = True,
    trigger_word: str = "",
) -> ReadyTomlResult:
    """生成指定格式的整套训练配置文本, 并汇总子集与蒙版统计.

    ``dirs`` 指定参与导出的工作目录名列表; 为 None 时表示全部目录, 为空列表时表示一个
    目录都不导出(返回空 subsets)。配置文件集合与文本内容完全由 ``assets/configs/<格式>``
    下的模板决定, 渲染时即按 ``export_dir`` 写出引用同目录文件的绝对路径, 因此预览文本
    与导出写入的文本一致, 导出不再改写任何字段。

    某目录蒙版状态为「完整」时, 「是否携带蒙版目录」在配置中体现为: 数据块按
    (分辨率, 是否携带) 分组以免两类子集混入同一块, 目录项则带 ``mask_path``。

    Args:
        root (Path): 数据集根目录.
        mode (LoraMode): 训练模式, 决定网络 rank(画风 style=16, 角色 character=32).
        train_format (str): 配置导出格式(即模板目录名).
        dirs (list[str] | None): 参与导出的工作目录名; None 表示全部, 空列表表示不导出.
        target_images (int): 目标图片数, 据此计算各目录的 num_repeats.
        subset_resolutions (dict[str, list[int]] | None): 目录名→分辨率列表; 留空按该格式
            的兜底规则处理.
        use_reg (bool): 是否附带正则化数据段(各格式模板均按其结构渲染).
        trigger_word (str): 该数据集触发词, 供模板写入采样提示词; 可为空.

    Returns:
        ReadyTomlResult: 含 files(文件名→配置文本)、subsets、mask_stats、subset_repeats 等.

    Raises:
        ValueError: 本格式的位置类环境变量未配置, 或模板变量未登记.

    Callers:
        - ``routes/ready.py:toml``
    """
    ctx = _ExportContext(
        root=root.resolve(),
        mode=mode,
        train_format=train_format,
        dirs=dirs,
        target_images=target_images,
        subset_resolutions=subset_resolutions or {},
        use_reg=use_reg,
        trigger_word=trigger_word,
    )
    files = _render_files(ctx)
    plan = ctx.plan()
    subsets = [
        ReadySubset(
            name=entry.name,
            image_dir=entry.image_dir,
            image_count=entry.image_count,
            conditioning_data_dir=entry.mask_dir,
        )
        for entry in plan
    ]
    mask_stats = [entry.stats for entry in plan]
    main_stats = next((m for m in mask_stats if m.name == "main"),
                      MaskStats(name="main", mask_status="无"))
    return ReadyTomlResult(
        root=str(ctx.root),
        format=train_format,
        files=files,
        subsets=subsets,
        dirs=ctx.work_dirs(),
        mask_stats=mask_stats,
        mask=str((ctx.root / "mask").resolve()) if main_stats.mask_status == "完整" else None,
        target_images=target_images,
        subset_repeats={entry.name: entry.repeats for entry in plan},
        mask_status=main_stats.mask_status,
        mask_complete=main_stats.mask_complete,
        mask_none=main_stats.mask_none,
        main_count=main_stats.main_count,
    )
