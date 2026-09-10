"""生成训练用 datasets TOML 片段, 并按子目录统计蒙版状态. """

from __future__ import annotations

from pathlib import Path

import tomlkit

from services.core.natural import natural_key
from services.state.mask import has_mask
from services.models import MaskStats, ReadySubset, ReadyTomlResult
from services.core.scanner import IMAGE_EXTS, scan_images
from services.core.work_dir import list_work_dirs
from services.tagging.clean import tag_key
from services.tagging.stats import count_tags


# ---- Anima-Flux LoRA 固定训练配置(全部由代码构造, 无模板文件) ----
# 正则化数据集根目录(独立于当前标注数据集, 共享的正则化图像库)
_REGL_ROOT = "/home/yuchuxi/Projects/Experiments/Dataset/ZhaoLora/datasets/regL"

# dataset.toml 的正则化数据段: (resolution, 子目录名)
_REGL_DATASETS = [(1536, "256_1"), (1024, "256_2")]

# dataset.toml 的 [general] 段
_GENERAL_CONFIG = {
    "enable_bucket": True,
    "min_bucket_reso": 256,
    "max_bucket_reso": 1536,
    "bucket_reso_steps": 64,
    "bucket_no_upscale": True,
}

# lora.toml 固定字段(模型/编码器路径与训练超参; output_name/sample_prompts/dataset_config 导出时覆写)
_LORA_CONFIG = {
    "pretrained_model_name_or_path": "/media/yuchuxi/Data/Servers/ComfyUI/models/diffusion_models/anima-base-v1.0.safetensors",
    "vae": "/media/yuchuxi/Data/Servers/ComfyUI/models/vae/qwen_image_vae.safetensors",
    "qwen3": "/media/yuchuxi/Data/Servers/ComfyUI/models/text_encoders/qwen_3_06b_base.safetensors",
    "qwen3_max_token_length": 1024,
    "t5_max_token_length": 1024,
    "timestep_sampling": "flux_shift",
    "sigmoid_scale": 1.0,
    "attn_mode": "flash",
    "prior_loss_weight": 0.5,
    "resolution": "1536",
    "output_dir": "./output",
    "save_model_as": "safetensors",
    "save_precision": "bf16",
    "save_every_n_steps": 50,
    "max_train_steps": 500,
    "train_batch_size": 2,
    "gradient_checkpointing": True,
    "gradient_accumulation_steps": 4,
    "network_train_unet_only": True,
    "learning_rate": 5e-05,
    "unet_lr": 5e-05,
    "text_encoder_lr": 0,
    "lr_scheduler": "cosine_with_min_lr",
    "lr_scheduler_min_lr_ratio": 1e-2,
    "lr_warmup_steps": 50,
    "optimizer_type": "AdamW",
    "network_dim": 16,
    "network_alpha": 16,
    "network_module": "lycoris.kohya",
    "network_dropout": 0.1,
    "network_args": ["dora_wd=1"],
    "sample_at_first": True,
    "sample_every_n_steps": 50,
    "log_with": "wandb",
    "logging_dir": "./logs",
    "caption_extension": ".txt",
    "shuffle_caption": True,
    "keep_tokens_separator": "|||",
    "caption_tag_dropout_rate": 0.5,
    "seed": 1337,
    "mixed_precision": "bf16",
    "cache_latents": True,
    "cache_latents_to_disk": True,
    "max_data_loader_n_workers": 0,
    "optimizer_args": ["weight_decay=0.001"],
    "masked_loss": True,
}

# prompt.txt 固定采样参数(触发词与高频标签由导出时拼接)
_PROMPT_SUFFIX = "--n worst quality, low quality, score_1, score_2, score_3, artist name, jpeg artifacts  --w 1024 --h 1024 --l 4  --s 30  --d 42"


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
        ReadyTomlResult: 含 toml 文本、subsets、mask_stats、subset_repeats 等.

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

    blocks: list[str] = []
    for key in keys:
        res = key[0]
        lines = ["[[datasets]]"]
        if res is not None:
            lines.append(f"resolution = {res}")
        for s, repeats in grouped[key]:
            lines.append("  [[datasets.subsets]]")
            lines.append(f'  image_dir = "{s.image_dir}"')
            if s.conditioning_data_dir:
                lines.append(f'  conditioning_data_dir = "{s.conditioning_data_dir}"')
            lines.append(f"  num_repeats = {repeats}")
        blocks.append("\n".join(lines))

    # 正则化数据段与数据集块共用同一文本渲染(缩进、空行风格一致), 统一解析后合并
    if use_reg:
        for res, sub in _REGL_DATASETS:
            blocks.append(
                "[[datasets]]\n"
                f"resolution = {res}\n"
                "  [[datasets.subsets]]\n"
                f'  image_dir = "{_REGL_ROOT}/{sub}"\n'
                "  num_repeats = 1"
            )

    # dataset.toml 完整文档: [general] 固定段 + 数据集块 + 可选正则化数据段,
    # 预览与导出共用同一渲染
    doc = tomlkit.document()
    doc["general"] = dict(_GENERAL_CONFIG)
    aot = tomlkit.aot()
    aot.extend(tomlkit.parse("\n\n".join(blocks)).get("datasets", []))
    doc["datasets"] = aot
    toml = tomlkit.dumps(doc)
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


def build_train_config(
    root: Path,
    *,
    out_dir: Path | None = None,
    dirs: list[str] | None = None,
    target_images: int = 512,
    subset_resolutions: dict[str, list[int]] | None = None,
    use_reg: bool = True,
    trigger_word: str = "",
) -> dict[str, str]:
    """为数据集渲染整套训练配置文本(全部由代码构造, 无模板文件).

    dataset.toml 由 ``build_ready_toml`` 完整渲染(``[general]`` 固定段 + 数据集块 +
    ``use_reg`` 为 True 时附带的正则化数据段), 与就绪页预览同源;
    lora.toml 由 ``_LORA_CONFIG`` 固定字段构成,
    另覆写 ``output_name`` / ``sample_prompts`` / ``dataset_config``;
    prompt.txt = 触发词(未设置则省略) + 出现次数最多的 10 个标签
    (``_top_caption_tags``) + 固定采样参数.

    Args:
        root (Path): 数据集根目录, 用于取数据集名、生成 main 段与统计高频标签.
        out_dir (Path | None): 导出目标目录, 用于写出 prompt.txt / dataset.toml 的引用路径.
        dirs (list[str] | None): 参与导出的工作目录名; None 表示全部(透传 build_ready_toml).
        target_images (int): 目标图片数(透传 build_ready_toml).
        subset_resolutions (dict[str, list[int]] | None): 目录名→分辨率列表(透传 build_ready_toml).
        use_reg (bool): 是否在 dataset.toml 中附带固定正则化数据段(透传 build_ready_toml).
        trigger_word (str): 该数据集触发词, 写入 prompt.txt 正向段开头; 可为空.

    Returns:
        dict[str, str]: 文件名→配置文本(lora.toml / dataset.toml / prompt.txt).

    Callers:
        - ``routes/ready.py:export``
    """
    dataset_toml = build_ready_toml(
        root, dirs=dirs, target_images=target_images,
        subset_resolutions=subset_resolutions, use_reg=use_reg,
    ).toml

    # lora.toml: 固定字段 + 导出时覆写的三项
    out = out_dir or root
    lora_doc = tomlkit.document()
    for key, value in _LORA_CONFIG.items():
        lora_doc[key] = value
    lora_doc["output_name"] = f"{root.name}_anima"
    lora_doc["sample_prompts"] = str(out / "prompt.txt")
    lora_doc["dataset_config"] = str(out / "dataset.toml")
    lora = tomlkit.dumps(lora_doc)

    names = dirs if dirs is not None else [d.name for d in list_work_dirs(root)]
    top_tags = _top_caption_tags(root, names, trigger_word)
    head = ", ".join(t for t in [trigger_word, *top_tags] if t)
    prompt = f"{head},  {_PROMPT_SUFFIX}" if head else _PROMPT_SUFFIX
    return {"lora.toml": lora, "dataset.toml": dataset_toml, "prompt.txt": prompt}
