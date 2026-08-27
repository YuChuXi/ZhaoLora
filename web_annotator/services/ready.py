"""生成训练用 datasets TOML 片段, 并按子目录统计蒙版状态. """

from __future__ import annotations

import os
from pathlib import Path

import tomlkit

from services.state.mask import has_mask
from services.models import MaskStats, ReadySubset, ReadyTomlResult
from services.core.scanner import IMAGE_EXTS, scan_images
from services.core.work_dir import list_work_dirs


# ---- Anima-Flux LoRA 固定训练配置(全部由代码构造, 无模板文件) ----
# 回归数据集根目录(独立于当前标注数据集, 为共享回归库)
_REGL_ROOT = "/home/yuchuxi/Projects/Experiments/Dataset/ZhaoLora/datasets/regL"

# dataset.toml 的回归段: (resolution, 子目录名)
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
    "network_dim": 12,
    "network_alpha": 12,
    "network_module": "networks.lora_anima",
    "network_dropout": 0.1,
    "network_args": [],
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

# prompt.txt 样本提示(触发词由导出时注入)
_PROMPT_TEMPLATE = "{trigger},  --n worst quality, low quality, score_1, score_2, score_3, artist name, jpeg artifacts  --w 1024 --h 1024 --l 4  --s 30  --d 42"


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
    """统计指定子目录的蒙版状态: 每张图仅「无」或「完整」.

    整体 status: 全部有蒙版 →「完整」；否则 →「无」.

    Args:
        root (Path): 数据集根目录.
        subdir (str): 子目录名, 默认 ``main``.

    Returns:
        MaskStats: 含 mask_status / mask_complete / mask_none / main_count 的统计.

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

    ``dirs`` 指定参与导出的工作目录名列表; 为 None 时表示全部目录,
    为空列表时表示一个目录都不导出(返回空 subsets).
    某子目录蒙版状态为「完整」时, 对应 subset 附带 conditioning_data_dir(指向 mask/子目录);
    按是否带 conditioning_data_dir 分组, 每组渲染为一个 [[datasets]] 块.
    传入 ``resolutions`` 时, 按每个分辨率 × 每组各渲染一个 [[datasets]] 块,
    块内首行 ``resolution = N``; 不传则保持单块且不带 resolution 行.

    ``target_images`` 为目标图片数: 据此计算总重复次数 ``total_repeats = max(分辨率块数, ceil(target_images / 启用子集总图片数))``,
    再按分辨率块数均分: base = floor(total_repeats / block_count), remainder 个 +1
    优先分配给最靠近 1024 的分辨率(同距则高分辨率优先), 使每个分辨率块 num_repeats >= 1 且重复后总图片数 >= target_images;
    不传分辨率时块数为 1, num_repeats = total_repeats. 启用子集总图片数为 0 时 total_repeats 取块数.

    Args:
        root (Path): 数据集根目录.
        dirs (list[str] | None): 参与导出的工作目录名列表; None 表示全部, 空列表表示不导出.
        target_images (int): 目标图片数, 据此计算 num_repeats.
        resolutions (list[int] | None): 多分辨率列表; 为空或 None 时生成单块且不带 resolution 行.

    Returns:
        ReadyTomlResult: 含 toml 文本、subsets、mask_stats、repeats、resolutions 等.

    Callers:
        - ``routes/ready.py:toml``
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
    ordered.extend(sorted(n for n in names if n != "main"))

    # 先确定有图片的目录并统计启用子集总图片数(与前端 subsets 张数同源)
    valid_names = [n for n in ordered if _has_images((root / n).resolve())]
    total = sum(available[n].image_count for n in valid_names)

    # 过滤非法分辨率(须为正整数), 保留输入顺序
    res_list = [r for r in (resolutions or []) if isinstance(r, int) and r > 0]
    if not res_list:
        res_list = [None]

    # total_repeats: 跨所有分辨率块的总重复次数, 取 ceil(target/total) 且至少为块数,
    # 保证每个分辨率块 num_repeats >= 1, 不产生 0 重复块
    # 按 block_count 均分: base = floor(total_repeats/block_count), remainder 个 +1
    # 优先分配给最靠近 1024 的分辨率, 同距则高分辨率优先
    block_count = len(res_list)
    total_repeats = max(block_count, -(-target_images // total)) if total else block_count
    base = total_repeats // block_count
    remainder = total_repeats % block_count

    def _res_priority(i: int) -> tuple[int, int]:
        """分辨率块 i 的 +1 优先级: (距 1024 的绝对差, 负分辨率), 越小越优先.

        Args:
            i (int): 分辨率块索引.

        Returns:
            tuple[int, int]: ``(距 1024 的绝对差, 负分辨率)``; 无分辨率时为 ``(0, 0)``.
        """
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


def build_train_config(
    root: Path,
    *,
    out_dir: Path | None = None,
    dirs: list[str] | None = None,
    target_images: int = 512,
    resolutions: list[int] | None = None,
    trigger_word: str = "",
) -> dict[str, str]:
    """为数据集渲染整套训练配置文本(全部由代码构造, 无模板文件).

    dataset.toml = ``[general]`` 固定段 + 就绪页生成的 main 段 + 回归段;
    lora.toml 由 ``_LORA_CONFIG`` 固定字段构成, 另覆写 ``output_name`` /
    ``sample_prompts`` / ``dataset_config``; prompt.txt 将 ``{trigger}`` 占位
    替换为数据集触发词.

    Args:
        root (Path): 数据集根目录, 用于取数据集名与生成 main 段.
        out_dir (Path | None): 导出目标目录, 用于写出 prompt.txt / dataset.toml 的引用路径.
        dirs (list[str] | None): 参与导出的工作目录名; None 表示全部(透传 build_ready_toml).
        target_images (int): 目标图片数(透传 build_ready_toml).
        resolutions (list[int] | None): 多分辨率列表(透传 build_ready_toml).
        trigger_word (str): 该数据集触发词, 替换 prompt.txt 中的 ``{trigger}`` 占位.

    Returns:
        dict[str, str]: 文件名→配置文本(lora.toml / dataset.toml / prompt.txt).

    Callers:
        - ``routes/ready.py:export``
    """
    main_toml = build_ready_toml(
        root, dirs=dirs, target_images=target_images, resolutions=resolutions
    ).toml
    main_doc = tomlkit.parse(main_toml)

    # dataset.toml: 固定 [general] 段 + 生成的主块 + 固定回归段(均为数组表)
    doc = tomlkit.document()
    doc["general"] = dict(_GENERAL_CONFIG)
    aot = tomlkit.aot()
    aot.extend(main_doc.get("datasets", []))
    for res, sub in _REGL_DATASETS:
        seg = tomlkit.table()
        seg["resolution"] = res
        subs = tomlkit.aot()
        subs.append({"image_dir": f"{_REGL_ROOT}/{sub}"})
        seg["subsets"] = subs
        aot.append(seg)
    doc["datasets"] = aot
    dataset_toml = tomlkit.dumps(doc)

    # lora.toml: 固定字段 + 导出时覆写的三项
    out = out_dir or root
    lora_doc = tomlkit.document()
    for key, value in _LORA_CONFIG.items():
        lora_doc[key] = value
    lora_doc["output_name"] = f"{root.name}_anima"
    lora_doc["sample_prompts"] = str(out / "prompt.txt")
    lora_doc["dataset_config"] = str(out / "dataset.toml")
    lora = tomlkit.dumps(lora_doc)

    prompt = _PROMPT_TEMPLATE
    if trigger_word:
        prompt = prompt.replace("{trigger}", trigger_word)
    return {"lora.toml": lora, "dataset.toml": dataset_toml, "prompt.txt": prompt}
