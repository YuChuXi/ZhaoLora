"""生成训练用 datasets TOML 片段, 并按子目录统计蒙版状态.

各导出格式的配置文本由 ``assets/configs/{sd-scripts,diffusion-pipe}/`` 下的 Jinja2
模板渲染, 本模块负责装配数据(路径、重复次数、分组与正则化数据段等)并调用
``render_template``.
"""

from __future__ import annotations

import os
from pathlib import Path

import jinja2
import tomlkit

from services.core.natural import natural_key
from services.state.mask import has_mask
from services.models import LoraMode, MaskStats, ReadySubset, ReadyTomlResult, TrainFormat
from services.core.scanner import IMAGE_EXTS, scan_images
from services.core.work_dir import list_work_dirs
from services.tagging.clean import tag_key
from services.tagging.stats import count_tags


# ---- 配置模板渲染(assets/ 下 Jinja2 文件) ----

# TOML 为纯文本, 关闭自动转义; trim/lstrip_blocks 使独占一行的控制标签不产生空行
_JINJA_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).resolve().parent.parent / "assets"),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render_template(name: str, **context: object) -> str:
    """渲染 ``assets/`` 目录下的 Jinja2 配置模板.

    Args:
        name (str): 模板相对路径, 如 ``configs/sd-scripts/dataset.toml.j2``.
        **context (object): 模板变量(路径、rank、分组数据等).

    Returns:
        str: 渲染后的配置文本.

    Callers:
        - ``services/ready.py:build_ready_toml``
        - ``services/ready.py:build_train_config``
        - ``services/ready.py:_build_dp_train_config``
        - ``services/ready.py:_build_dp_dataset_config``
    """
    return _JINJA_ENV.get_template(name).render(**context)

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
        - ``services/ready.py:build_ready_toml``
        - ``services/ready.py:_build_dp_dataset_config``
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

# sd-scripts 导出文件名集合(三件套); diffusion-pipe 见 DP_FILE_NAMES
TRAIN_FILE_NAMES = ("lora.toml", "dataset.toml", "prompt.txt")

# diffusion-pipe 导出文件名集合(两件套: 无采样提示词机制, 不导出 prompt.txt)
DP_FILE_NAMES = ("train.toml", "dataset.toml")

# diffusion-pipe 各目录 resolutions 的默认填充值(configs/chawenjian/dataset.toml 蓝本);
# 导出时每个 [[directory]] 都显式声明 resolutions, 侧栏留空的目录用它填充
DP_DEFAULT_RESOLUTIONS = [512, 1024, 1536]

# 各导出格式的文件名集合(预览键与导出请求 files 键校验的唯一依据)
TRAIN_FORMAT_FILES: dict[TrainFormat, tuple[str, ...]] = {
    "sd-scripts": TRAIN_FILE_NAMES,
    "diffusion-pipe": DP_FILE_NAMES,
}


def _has_images(d: Path) -> bool:
    """目录内是否至少有一张图.

    Args:
        d (Path): 待检查的目录.

    Returns:
        bool: 目录内存在图片文件则为 True.
    Callers:
        - ``services/ready.py:build_ready_toml``
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
        - ``services/ready.py:build_ready_toml``
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
        - ``services/ready.py:build_ready_toml``
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


def build_ready_toml(
    root: Path,
    *,
    dirs: list[str] | None = None,
    target_images: int = 512,
    subset_resolutions: dict[str, list[int]] | None = None,
    use_reg: bool = True,
) -> ReadyTomlResult:
    """生成完整 dataset.toml 文本与 subset 元数据.

    ``dirs`` 指定参与导出的工作目录名列表; 为 None 时表示全部目录,
    为空列表时表示一个目录都不导出(返回空 subsets).
    某子目录蒙版状态为「完整」时, 对应 subset 附带 conditioning_data_dir(指向 mask/子目录).

    每个子集的分辨率列表取自 ``subset_resolutions[目录名]``, 过滤非法值后为空时
    生成单块且不带 resolution 行. 子集级总重复次数取
    ``max(该子集分辨率块数, ceil(target_images / 启用子集总图片数))`` (启用子集总图片数
    为 0 时取块数), 保证每个分辨率块 num_repeats >= 1, 再按该子集的分辨率块均分,
    使每个子集重复后图片数 >= 目标图片数折算的份额.

    以 (分辨率, 是否携带蒙版目录) 为键对子集分组, 每组渲染一个 [[datasets]] 块:
    分辨率按首次出现顺序, 同一分辨率下蒙版块在前; 无分辨率的子集(列表为空)归入
    不带 resolution 行的块. sd-scripts 仅当块内全部 subset 均携带
    conditioning_data_dir 时才按蒙版数据集校验, 因此两类子集不得混入同一块.

    dataset.toml = ``[general]`` 固定段 + 数据集块 + 正则化数据段(``use_reg`` 为
    True 时附带), 预览与导出共用本渲染.

    Args:
        root (Path): 数据集根目录.
        dirs (list[str] | None): 参与导出的工作目录名列表; None 表示全部, 空列表表示不导出.
        target_images (int): 目标图片数, 据此计算各子集的 num_repeats.
        subset_resolutions (dict[str, list[int]] | None): 目录名→分辨率列表; 未指定的子集生成单块无 resolution 行.
        use_reg (bool): 是否附带固定正则化数据段(datasets/regL).

    Returns:
        ReadyTomlResult: 含 files(dataset.toml 文本)、subsets、mask_stats、subset_repeats 等.

    Callers:
        - ``routes/ready.py:toml``
        - ``routes/ready.py:export``
        - ``services/ready.py:build_train_config``
    """
    root = root.resolve()
    available = {d.name: d for d in list_work_dirs(root)}
    if dirs is not None:
        names = [n for n in dirs if n in available]
    else:
        names = list(available.keys())

    ordered: list[str] = []
    if "main" in names:
        ordered.append("main")
    ordered.extend(sorted((n for n in names if n != "main"), key=natural_key))

    # 先确定有图片的目录并统计启用子集总图片数(与前端 subsets 张数同源)
    valid_names = [n for n in ordered if _has_images((root / n).resolve())]
    total = sum(available[n].image_count for n in valid_names)

    # 各子集的分辨率块规格: 目录名 → [(分辨率, num_repeats)], None 表示不带
    # resolution 行的块. 子集级总重复次数取 ceil(target/total) 且至少为该子集块数
    # (启用子集总图片数为 0 时取块数), 保证每块 num_repeats >= 1, 再均分到各分辨率块
    per_target = -(-target_images // total) if total else 1
    block_specs: dict[str, list[tuple[int | None, int]]] = {}
    for name in valid_names:
        # 子集内同一分辨率只保留首个, 重复值无独立语义
        res_list: list[int | None] = list(dict.fromkeys(
            r for r in (subset_resolutions or {}).get(name, [])
            if isinstance(r, int) and r > 0
        )) or [None]
        reps = _alloc_repeats(res_list, max(len(res_list), per_target))
        block_specs[name] = list(zip(res_list, reps))

    subsets: list[ReadySubset] = []
    mask_stats: list[MaskStats] = []
    for name in valid_names:
        image_dir = (root / name).resolve()
        entry = ReadySubset(
            name=name,
            image_dir=str(image_dir),
            image_count=available[name].image_count,
        )
        mstats = subdir_mask_stats(root, name)
        mask_stats.append(mstats)
        if mstats.mask_status == "完整":
            entry.conditioning_data_dir = str((root / "mask" / name).resolve())
        subsets.append(entry)

    # 分组: (分辨率, 是否携带蒙版目录) → [(subset, num_repeats)], 组由成员产生,
    # 不存在空块. sd-scripts 仅当块内全部 subset 均携带 conditioning_data_dir 时才按
    # 蒙版数据集校验, 因此携带与不携带的子集分属不同组; 无分辨率子集归入
    # 不带 resolution 行的组
    grouped: dict[tuple[int | None, bool], list[tuple[ReadySubset, int]]] = {}
    for s in subsets:
        for res, repeats in block_specs[s.name]:
            grouped.setdefault((res, bool(s.conditioning_data_dir)), []).append((s, repeats))

    # 块顺序: 分辨率按首次出现顺序, 同一分辨率蒙版块在前
    res_rank: dict[int | None, int] = {}
    for res, _ in grouped:
        res_rank.setdefault(res, len(res_rank))
    keys = sorted(grouped, key=lambda k: (res_rank[k[0]], not k[1]))

    # 渲染分组数据: 每组一个 [[datasets]] 块(分辨率 + subset 列表), 交给模板统一排版
    groups: list[dict] = []
    for key in keys:
        res = key[0]
        groups.append({
            "resolution": res,
            "subsets": [
                {
                    "image_dir": s.image_dir,
                    "conditioning_data_dir": s.conditioning_data_dir,
                    "repeats": repeats,
                }
                for s, repeats in grouped[key]
            ],
        })

    # 正则化数据段: reg 图以类提示词 caption 参与损失, num_repeats 固定 1, 追加在数据块之后
    if use_reg:
        for res, path in reg_datasets():
            groups.append({
                "resolution": res,
                "subsets": [{"image_dir": path, "conditioning_data_dir": None, "repeats": 1}],
            })

    toml = render_template("configs/sd-scripts/dataset.toml.j2", groups=groups)
    main_ms = next((m for m in mask_stats if m.name == "main"),
                   MaskStats(name="main", mask_status="无"))
    return ReadyTomlResult(
        root=str(root),
        files={"dataset.toml": toml},
        subsets=subsets,
        dirs=list(available.values()),
        mask_stats=mask_stats,
        mask=str((root / "mask").resolve()) if main_ms.mask_status == "完整" else None,
        target_images=target_images,
        subset_repeats={name: [r for _, r in spec] for name, spec in block_specs.items()},
        mask_status=main_ms.mask_status,
        mask_complete=main_ms.mask_complete,
        mask_none=main_ms.mask_none,
        main_count=main_ms.main_count,
    )


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

    Raises:
        无.

    Callers:
        - ``services/ready.py:build_train_config``
    """
    result = count_tags([root / n for n in names], per_sample=True)
    trigger_key = tag_key(trigger_word) if trigger_word else None
    return [
        r.tag for r in result.tags
        if trigger_key is None or tag_key(r.tag) != trigger_key
    ][:10]


def apply_lora_output_paths(doc: tomlkit.TOMLDocument, *, root_name: str, out_dir: Path) -> None:
    """覆写 lora.toml 文档中由导出位置决定的三项路径字段.

    ``output_name`` 取数据集名派生, ``sample_prompts`` / ``dataset_config`` 指向导出
    目录内的 prompt.txt / dataset.toml; 预览与导出共用本覆写, 保证两处路径语义一致.

    Args:
        doc (tomlkit.TOMLDocument): 构造中或编辑后的 lora.toml 文档, 原地修改.
        root_name (str): 数据集名, 作为 output_name 的来源.
        out_dir (Path): 导出目标目录(预览时为数据集根目录, 导出时为配置目录).

    Returns:
        None: 原地修改 ``doc``.

    Callers:
        - ``services/ready.py:build_train_config``
        - ``routes/ready.py:export``
    """
    doc["output_name"] = f"{root_name}_anima"
    doc["sample_prompts"] = str(out_dir / "prompt.txt")
    doc["dataset_config"] = str(out_dir / "dataset.toml")


def _build_dp_train_config(root: Path, *, out_dir: Path | None, mode: LoraMode) -> str:
    """渲染 diffusion-pipe 风格 train.toml 文本.

    固定段(训练/验证/保存与 ``[model]`` / ``[optimizer]`` / ``[monitoring]``)取自
    ``assets/configs/diffusion-pipe/train.toml.j2``(configs/chawenjian/train.toml 蓝本);
    ``output_dir`` 指向导出目录内 ``output/``, ``dataset`` 指向同目录 ``dataset.toml``,
    ``[adapter].rank`` 取 ``_LORA_RANKS[mode]``, ``[monitoring].wandb_tracker_name``
    由数据集名派生.

    Args:
        root (Path): 数据集根目录, 提供数据集名.
        out_dir (Path | None): 导出目标目录; None 时取 root.
        mode (LoraMode): 训练模式, 决定 LoRA rank(画风 style=16, 角色 character=32).

    Returns:
        str: train.toml 文本.

    Callers:
        - ``services/ready.py:build_train_config``
    """
    out = out_dir or root
    return render_template(
        "configs/diffusion-pipe/train.toml.j2",
        output_dir=str(out / "output"),
        dataset=str(out / "dataset.toml"),
        rank=_LORA_RANKS[mode],
        wandb_tracker_name=f"{root.name}-krea2",
    )


def _build_dp_dataset_config(
    root: Path,
    *,
    dirs: list[str] | None = None,
    target_images: int = 512,
    subset_resolutions: dict[str, list[int]] | None = None,
    use_reg: bool = True,
) -> tuple[str, dict[str, list[int]]]:
    """渲染 diffusion-pipe 风格 dataset.toml 文本并返回各目录逐分辨率 num_repeats.

    每个启用且含图的工作目录一个 ``[[directory]]`` 块: ``resolutions`` 为该目录分辨率
    列表, ``num_repeats`` 为目录级单值、对该列表每个分辨率桶独立生效(与 sd-scripts
    块级语义相同: 每图 × 每分辨率 × repeats). 分配与 sd-scripts 同源 —— 总重复次数取
    ``max(块数, ceil(target/total))`` 后经 ``_alloc_repeats`` 均分(余数优先分给最靠近
    1024 的分辨率); 分配值全相等时即单块原生形式, 不等时按 repeats 值归并分辨率、
    以同 ``path`` 多块表达(diffusion-pipe 按块实例化 DirectoryDataset, 无重复路径限制).
    ``mask_path`` 在蒙版状态为「完整」时必带; ``use_reg`` 为 True 时追加 regL
    正则化目录块(num_repeats 固定 1, 目录级 ``resolutions`` 取其蓝本分辨率):
    regL 以类提示词(不含触发词)的 caption 参与同一损失计算, 效果即 kohya 先验
    保持权重 1.0(diffusion-pipe 无独立 reg loss 权重旋钮); 触发词与类先验的
    分离由 regL 自身 caption 保证.

    Args:
        root (Path): 数据集根目录.
        dirs (list[str] | None): 参与导出的工作目录名列表; None 表示全部, 空列表表示不导出.
        target_images (int): 目标图片数, 据此计算各目录逐分辨率 num_repeats.
        subset_resolutions (dict[str, list[int]] | None): 目录名→分辨率列表, 留空以
            ``DP_DEFAULT_RESOLUTIONS`` 填充.
        use_reg (bool): 是否附带 regL 正则化目录块(datasets/regL).

    Returns:
        tuple[str, dict[str, list[int]]]: dataset.toml 文本 与 各数据目录逐分辨率的
        num_repeats 列表(键为目录名, 按其分辨率列表顺序, 不含正则化目录).

    Callers:
        - ``services/ready.py:build_train_config``
    """
    root = root.resolve()
    available = {d.name: d for d in list_work_dirs(root)}
    if dirs is not None:
        names = [n for n in dirs if n in available]
    else:
        names = list(available.keys())
    ordered: list[str] = []
    if "main" in names:
        ordered.append("main")
    ordered.extend(sorted((n for n in names if n != "main"), key=natural_key))
    valid_names = [n for n in ordered if _has_images((root / n).resolve())]
    total = sum(available[n].image_count for n in valid_names)
    per_target = -(-target_images // total) if total else 1

    # 渲染数据: 每目录一个 [[directory]] 块, num_repeats 经 _alloc_repeats 与 sd-scripts
    # 同源分配; 分配值按 repeats 归并分辨率(全相等时即单块), regL 块追加在后
    dir_repeats: dict[str, list[int]] = {}
    directories: list[dict] = []
    for name in valid_names:
        res_list = list(dict.fromkeys(
            r for r in (subset_resolutions or {}).get(name, [])
            if isinstance(r, int) and r > 0
        )) or DP_DEFAULT_RESOLUTIONS
        reps = _alloc_repeats(res_list, max(len(res_list), per_target))
        dir_repeats[name] = reps
        mask_path = str((root / "mask" / name).resolve()) \
            if subdir_mask_stats(root, name).mask_status == "完整" else None
        by_rep: dict[int, list[int]] = {}
        for res, rep in zip(res_list, reps):
            by_rep.setdefault(rep, []).append(res)
        for rep, res_group in by_rep.items():
            directories.append({
                "path": str((root / name).resolve()),
                "mask_path": mask_path,
                "repeats": rep,
                "resolutions": res_group,
            })
    if use_reg:
        for res, path in reg_datasets():
            directories.append({"path": path, "mask_path": None, "repeats": 1, "resolutions": [res]})

    return render_template("configs/diffusion-pipe/dataset.toml.j2",
                           directories=directories), dir_repeats


def build_train_config(
    root: Path,
    *,
    out_dir: Path | None = None,
    mode: LoraMode,
    train_format: TrainFormat = "sd-scripts",
    dirs: list[str] | None = None,
    target_images: int = 512,
    subset_resolutions: dict[str, list[int]] | None = None,
    use_reg: bool = True,
    trigger_word: str = "",
) -> tuple[dict[str, str], dict[str, list[int]]]:
    """为数据集渲染整套训练配置文本(文本由 assets/ 下 Jinja2 模板渲染).

    ``train_format`` 为 ``sd-scripts`` 时: dataset.toml 由 ``build_ready_toml`` 完整渲染
    (模板 ``assets/configs/sd-scripts/dataset.toml.j2``: ``[general]`` 固定段 + 数据集块 +
    ``use_reg`` 为 True 时附带的正则化数据段), 与就绪页预览同源; lora.toml 由模板
    ``assets/configs/sd-scripts/lora.toml.j2`` 渲染, ``network_dim`` / ``network_alpha``
    取 ``_LORA_RANKS[mode]``, 三项路径字段由导出位置决定(与 ``apply_lora_output_paths``
    的覆写语义一致); prompt.txt = 触发词(未设置则省略) + 出现次数最多的 10 个标签
    (``_top_caption_tags``) + 固定采样参数(模板 ``assets/configs/sd-scripts/prompt.txt.j2``).

    ``train_format`` 为 ``diffusion-pipe`` 时: train.toml 由 ``_build_dp_train_config``
    渲染, dataset.toml 由 ``_build_dp_dataset_config`` 渲染.

    Args:
        root (Path): 数据集根目录, 用于取数据集名、生成 main 段与统计高频标签.
        out_dir (Path | None): 导出目标目录, 用于写出配置文件的引用路径; None 时取 root.
        mode (LoraMode): 训练模式, 决定网络 rank(画风 style=16, 角色 character=32).
        train_format (TrainFormat): 配置导出格式.
        dirs (list[str] | None): 参与导出的工作目录名; None 表示全部(透传各构建函数).
        target_images (int): 目标图片数(透传各构建函数).
        subset_resolutions (dict[str, list[int]] | None): 目录名→分辨率列表(透传各构建函数).
        use_reg (bool): 是否在 dataset.toml 中附带 regL 正则化数据段(两种格式均生效,
            diffusion-pipe 下等价于先验保持权重 1.0).
        trigger_word (str): 该数据集触发词, 写入 prompt.txt 正向段开头; 可为空.

    Returns:
        tuple[dict[str, str], dict[str, list[int]]]: (文件名→配置文本, 各子集逐分辨率
        num_repeats). 文件名键与 ``TRAIN_FORMAT_FILES[train_format]`` 一致(sd-scripts:
        lora.toml / dataset.toml / prompt.txt; diffusion-pipe: train.toml / dataset.toml);
        num_repeats 仅 diffusion-pipe 返回(键为目录名, 与其分辨率列表一一对应),
        sd-scripts 返回空 dict(sd-scripts 的 num_repeats 由 ``build_ready_toml`` 计算).

    Callers:
        - ``routes/ready.py:toml``
    """
    if train_format == "diffusion-pipe":
        dataset_toml, subset_repeats = _build_dp_dataset_config(
            root, dirs=dirs, target_images=target_images,
            subset_resolutions=subset_resolutions, use_reg=use_reg,
        )
        return {
            "train.toml": _build_dp_train_config(root, out_dir=out_dir, mode=mode),
            "dataset.toml": dataset_toml,
        }, subset_repeats

    dataset_toml = build_ready_toml(
        root, dirs=dirs, target_images=target_images,
        subset_resolutions=subset_resolutions, use_reg=use_reg,
    ).files["dataset.toml"]

    # lora.toml: 固定字段 + 模式 rank + 导出位置三项路径(与 apply_lora_output_paths 覆写语义一致)
    out = out_dir or root
    rank = _LORA_RANKS[mode]
    lora = render_template(
        "configs/sd-scripts/lora.toml.j2",
        rank=rank,
        output_name=f"{root.name}_anima",
        sample_prompts=str(out / "prompt.txt"),
        dataset_config=str(out / "dataset.toml"),
    )

    names = dirs if dirs is not None else [d.name for d in list_work_dirs(root)]
    top_tags = _top_caption_tags(root, names, trigger_word)
    head = ", ".join(t for t in [trigger_word, *top_tags] if t)
    prompt = render_template("configs/sd-scripts/prompt.txt.j2", head=head)
    return {"lora.toml": lora, "dataset.toml": dataset_toml, "prompt.txt": prompt}, {}



