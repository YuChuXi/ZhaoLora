#!/usr/bin/env python3
"""
批量图片背景移除工具 - 基于 BiRefNet
=============================================
支持输出模式: 透明背景 / 白底 / 纯mask
支持形态学操作处理漏色问题
模型: https://huggingface.co/ZhengPeng7/BiRefNet

用法示例:
    # 透明背景 (默认)
    python batch_birefnet_bg_removal.py -i ./input -o ./output

    # 白底输出
    python batch_birefnet_bg_removal.py -i ./input -o ./output --mode white

    # 仅输出mask
    python batch_birefnet_bg_removal.py -i ./input -o ./output --mode mask

    # 使用本地权重 + 形态学腐蚀处理漏色
    python batch_birefnet_bg_removal.py -i ./input -o ./output \\
        --weight /path/to/birefnet.safetensors \\
        --morph erode --morph-kernel 5 --morph-iterations 2

    # 边缘羽化减少锯齿
    python batch_birefnet_bg_removal.py -i ./input -o ./output \\
        --edge-smooth 3 --morph open --morph-kernel 3
"""

import argparse
import os
import sys
import warnings
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 模型加载
# ---------------------------------------------------------------------------

MODEL_REPO = "ZhengPeng7/BiRefNet"
IMAGE_SIZE = (1024, 1024)
# 注意: 官方 HuggingFace 模型代码使用 ImageNet 归一化
# 但 ComfyUI 版本使用 [0,1] 归一化 (mean=0, std=1)
# 可通过 --norm 参数切换
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]
ZERO_MEAN = [0.0, 0.0, 0.0]
ONE_STD = [1.0, 1.0, 1.0]


def load_model(weight_path: str = None, device: str = "cuda", norm_mode: str = "imagenet"):
    """
    加载 BiRefNet 模型。
    
    优先使用本地权重文件 (不下载 HF 权重)。仅在本地权重不可用时才从 HF 下载。
    模型架构代码始终从 HuggingFace 获取 (仅几 KB, 首次后缓存)。
    
    Args:
        weight_path: 本地 .safetensors 或 .pth 权重路径 (可选)
        device: 'cuda' 或 'cpu'
        norm_mode: 'imagenet' (官方HF) 或 'zero_one' (ComfyUI兼容)
    
    Returns:
        model, transform_image, norm_mode_used
    """
    try:
        from transformers import AutoModelForImageSegmentation, AutoConfig
    except ImportError:
        print("[ERROR] 请安装 transformers: pip install transformers huggingface_hub")
        sys.exit(1)

    print(f"[INFO] 加载模型架构代码从 HuggingFace: {MODEL_REPO} (仅代码/配置, 不下载权重) ...")

    # Step 1: 先获取模型配置 (仅下载 config.json, 几 KB)
    config = AutoConfig.from_pretrained(MODEL_REPO, trust_remote_code=True)
    
    # 设置 dtype
    config.torch_dtype = torch.float16 if device == "cuda" else torch.float32

    # Step 2: 从配置创建模型 (不下载权重)
    model = AutoModelForImageSegmentation.from_config(config, trust_remote_code=True)

    # Step 3: 加载权重
    if weight_path and os.path.isfile(weight_path):
        # --- 使用本地权重文件 (不联网) ---
        print(f"[INFO] 加载本地权重: {weight_path}")
        try:
            import safetensors.torch
            state_dict = safetensors.torch.load_file(weight_path)
        except Exception:
            # 回退到 torch.load (处理 .pth 文件)
            print("[INFO] safetensors 加载失败, 尝试 torch.load ...")
            state_dict = torch.load(weight_path, map_location="cpu", weights_only=True)

        # 检查并处理可能的 key 前缀
        state_dict = _check_state_dict(state_dict)

        # 加载权重 (允许部分不匹配)
        missing, unexpected = model.load_state_dict(state_dict, strict=False)
        if missing:
            print(f"[WARN] 缺失的权重键 ({len(missing)}): {missing[:5]}...")
        if unexpected:
            print(f"[WARN] 多余的权重键 ({len(unexpected)}): {unexpected[:5]}...")
        print("[INFO] 本地权重加载完成.")
    else:
        # --- 回退: 从 HuggingFace 下载权重 ---
        print(f"[INFO] 本地权重不可用, 从 HuggingFace 下载权重 (约 444MB, 仅首次) ...")
        model = AutoModelForImageSegmentation.from_pretrained(
            MODEL_REPO,
            trust_remote_code=True,
            dtype=torch.float16 if device == "cuda" else torch.float32,
        )

    # 移动到设备
    model = model.to(device)
    model.eval()

    # 设置 float32 matmul precision (加速推理)
    if device == "cuda":
        torch.set_float32_matmul_precision("high")

    # 构建预处理变换
    if norm_mode == "zero_one":
        mean, std = ZERO_MEAN, ONE_STD
    else:
        mean, std = IMAGENET_MEAN, IMAGENET_STD

    transform_image = transforms.Compose([
        transforms.Resize(IMAGE_SIZE),
        transforms.ToTensor(),
        transforms.Normalize(mean=mean, std=std),
    ])

    print(f"[INFO] 模型就绪, 设备={device}, 归一化模式={norm_mode}")
    return model, transform_image


def _check_state_dict(state_dict, unwanted_prefixes=("module.", "_orig_mod.")):
    """清理 state_dict 中的前缀 (来自 BiRefNet utils.py)."""
    for k in list(state_dict.keys()):
        new_k = k
        for prefix in unwanted_prefixes:
            if new_k.startswith(prefix):
                new_k = new_k[len(prefix):]
        if new_k != k:
            state_dict[new_k] = state_dict.pop(k)
    return state_dict


# ---------------------------------------------------------------------------
# 推理核心
# ---------------------------------------------------------------------------

def predict_mask(model, image: Image.Image, transform_image, device: str = "cuda") -> np.ndarray:
    """
    对单张图片推理, 返回 [0, 255] 的 uint8 mask (H, W).
    """
    orig_size = image.size  # (W, H)

    input_tensor = transform_image(image).unsqueeze(0).to(device)
    if device == "cuda":
        input_tensor = input_tensor.half()

    with torch.no_grad():
        preds = model(input_tensor)
        # model 返回 scaled_preds (list of tensors), 取最后一个
        if isinstance(preds, (list, tuple)):
            pred = preds[-1].sigmoid().cpu()
        else:
            pred = preds.sigmoid().cpu()

    pred = pred[0].squeeze()  # (H_pred, W_pred) 或 (1, H, W)

    # 转 PIL 并缩放回原始尺寸
    if pred.dim() == 3:
        pred = pred.squeeze(0)
    pred_pil = transforms.ToPILImage()(pred)
    pred_pil = pred_pil.resize(orig_size, Image.BILINEAR)

    mask = np.array(pred_pil, dtype=np.uint8)
    return mask


# ---------------------------------------------------------------------------
# 形态学后处理
# ---------------------------------------------------------------------------

def apply_morphology(mask: np.ndarray, operation: str, kernel_size: int, iterations: int) -> np.ndarray:
    """
    对 mask 应用形态学操作。

    Args:
        mask: uint8 mask, 0-255
        operation: 'erode', 'dilate', 'open', 'close'
        kernel_size: 核大小 (奇数)
        iterations: 迭代次数

    Returns:
        处理后的 mask
    """
    if kernel_size <= 0 or iterations <= 0:
        return mask

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))

    op_map = {
        "erode": cv2.MORPH_ERODE,
        "dilate": cv2.MORPH_DILATE,
        "open": cv2.MORPH_OPEN,
        "close": cv2.MORPH_CLOSE,
    }

    if operation not in op_map:
        print(f"[WARN] 未知的形态学操作: {operation}, 跳过.")
        return mask

    return cv2.morphologyEx(mask, op_map[operation], kernel, iterations=iterations)


def apply_edge_smooth(mask: np.ndarray, blur_radius: int) -> np.ndarray:
    """
    对 mask 边缘进行高斯模糊平滑 (羽化效果), 减少锯齿。

    Args:
        mask: uint8 mask, 0-255
        blur_radius: 高斯模糊半径 (奇数)

    Returns:
        平滑后的 mask
    """
    if blur_radius <= 0:
        return mask

    # 确保半径为奇数
    if blur_radius % 2 == 0:
        blur_radius += 1

    return cv2.GaussianBlur(mask, (blur_radius, blur_radius), 0)


def apply_mask_expand(mask: np.ndarray, expand_px: int) -> np.ndarray:
    """
    将 mask 主体向外扩展指定像素数。

    Args:
        mask: uint8 mask, 0-255
        expand_px: 向外扩展的像素数

    Returns:
        扩展后的 mask
    """
    if expand_px <= 0:
        return mask
    kernel_size = expand_px * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    return cv2.dilate(mask, kernel, iterations=1)


def apply_mask_shrink(mask: np.ndarray, shrink_px: int) -> np.ndarray:
    """
    将 mask 主体向内收缩指定像素数。

    Args:
        mask: uint8 mask, 0-255
        shrink_px: 向内收缩的像素数

    Returns:
        收缩后的 mask
    """
    if shrink_px <= 0:
        return mask
    kernel_size = shrink_px * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    return cv2.erode(mask, kernel, iterations=1)


def remap_mask_range(mask: np.ndarray, mask_min: int, mask_max: int) -> np.ndarray:
    """
    将 mask 值从 [0, 255] 线性映射到 [mask_min, mask_max]。

    Args:
        mask: uint8 mask, 0-255
        mask_min: 目标最小值
        mask_max: 目标最大值

    Returns:
        重映射后的 uint8 mask
    """
    if mask_min == 0 and mask_max == 255:
        return mask
    mask_f = mask.astype(np.float32) / 255.0
    mask_f = mask_f * (mask_max - mask_min) + mask_min
    return np.clip(mask_f, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# 输出生成
# ---------------------------------------------------------------------------

def save_output(image: Image.Image, mask: np.ndarray, output_path: str, mode: str):
    """
    根据模式保存输出图片。

    Args:
        image: 原始 PIL Image (RGB/RGBA)
        mask: uint8 mask, 0-255
        output_path: 输出路径
        mode: 'transparent', 'white', 'mask'
    """
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    image_rgba = image.convert("RGBA")
    image_np = np.array(image_rgba)  # (H, W, 4)

    if mode == "mask":
        # 仅保存 mask 为灰度图
        Image.fromarray(mask).save(output_path)
        return

    # 归一化 mask 到 [0, 1]
    mask_f = mask.astype(np.float32) / 255.0

    if mode == "white":
        # 白底合成
        bg = np.ones_like(image_np, dtype=np.float32) * 255
        fg = image_np.astype(np.float32)
        # alpha blending: fg * alpha + bg * (1 - alpha)
        mask_3ch = np.stack([mask_f] * 3, axis=-1)
        result = fg[:, :, :3] * mask_3ch + bg[:, :, :3] * (1 - mask_3ch)
        result = result.astype(np.uint8)
        Image.fromarray(result, "RGB").save(output_path)

    elif mode == "transparent":
        # 透明背景: 将 mask 设置为 alpha 通道
        image_np[:, :, 3] = mask
        Image.fromarray(image_np, "RGBA").save(output_path)

    else:
        raise ValueError(f"未知的输出模式: {mode}")


# ---------------------------------------------------------------------------
# 批量处理
# ---------------------------------------------------------------------------

def collect_images(input_path: str) -> list:
    """收集输入目录/文件中的所有图片."""
    input_path = Path(input_path)
    supported_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

    if input_path.is_file():
        if input_path.suffix.lower() in supported_exts:
            return [str(input_path)]
        else:
            print(f"[WARN] 不支持的文件格式: {input_path}")
            return []

    if input_path.is_dir():
        images = []
        for ext in supported_exts:
            images.extend(input_path.glob(f"*{ext}"))
            images.extend(input_path.glob(f"*{ext.upper()}"))
        return sorted([str(p) for p in images])

    print(f"[ERROR] 输入路径不存在: {input_path}")
    return []


def process_batch(
    model,
    transform_image,
    input_dir: str,
    output_dir: str,
    mode: str = "transparent",
    morph_op: str = None,
    morph_kernel: int = 0,
    morph_iterations: int = 1,
    edge_smooth: int = 0,
    mask_expand: int = 0,
    mask_shrink: int = 0,
    mask_min: int = 0,
    mask_max: int = 255,
    device: str = "cuda",
    skip_existing: bool = False,
):
    """
    批量处理图片。

    Args:
        model: BiRefNet 模型
        transform_image: torchvision 预处理变换
        input_dir: 输入目录或文件
        output_dir: 输出目录
        mode: 'transparent' / 'white' / 'mask'
        morph_op: 形态学操作类型
        morph_kernel: 形态学核大小
        morph_iterations: 形态学迭代次数
        edge_smooth: 边缘羽化半径
        mask_expand: 主体向外扩展像素数 (膨胀)
        mask_shrink: 主体向内收缩像素数 (腐蚀)
        mask_min: mask 值域下限 (0~255, 默认 0)
        mask_max: mask 值域上限 (0~255, 默认 255)
        device: 设备
        skip_existing: 是否跳过已存在的输出文件
    """
    images = collect_images(input_dir)
    if not images:
        print("[ERROR] 没有找到可处理的图片.")
        return

    os.makedirs(output_dir, exist_ok=True)

    ext_map = {"transparent": ".png", "white": ".png", "mask": ".png"}
    output_ext = ext_map.get(mode, ".png")

    success_count = 0
    skip_count = 0
    fail_count = 0

    pbar = tqdm(images, desc="处理图片", unit="张")
    for img_path in pbar:
        img_name = Path(img_path).stem
        out_path = os.path.join(output_dir, f"{img_name}{output_ext}")

        if skip_existing and os.path.exists(out_path):
            skip_count += 1
            pbar.set_postfix({"成功": success_count, "跳过": skip_count, "失败": fail_count})
            continue

        try:
            # 读取图片 (保留 ICC profile 避免颜色偏差)
            image = Image.open(img_path).convert("RGB")

            # 推理获取 mask
            mask = predict_mask(model, image, transform_image, device)

            # 主体扩展/收缩 (比形态学参数更直观, 以像素为单位)
            if mask_expand > 0:
                mask = apply_mask_expand(mask, mask_expand)
            if mask_shrink > 0:
                mask = apply_mask_shrink(mask, mask_shrink)

            # 形态学后处理
            if morph_op and morph_kernel > 0:
                mask = apply_morphology(mask, morph_op, morph_kernel, morph_iterations)

            # 边缘平滑
            if edge_smooth > 0:
                mask = apply_edge_smooth(mask, edge_smooth)

            # 重映射 mask 值域 (在形态学处理后进行)
            mask = remap_mask_range(mask, mask_min, mask_max)

            # 保存输出
            save_output(image, mask, out_path, mode)

            success_count += 1
            pbar.set_postfix({"成功": success_count, "跳过": skip_count, "失败": fail_count})

        except Exception as e:
            fail_count += 1
            pbar.set_postfix({"成功": success_count, "跳过": skip_count, "失败": fail_count})
            print(f"\n[ERROR] 处理失败 {img_path}: {e}")

    print(f"\n[DONE] 完成! 成功: {success_count}, 跳过: {skip_count}, 失败: {fail_count}")
    print(f"[INFO] 输出目录: {output_dir}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="BiRefNet 批量图片背景移除工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s -i ./input -o ./output
  %(prog)s -i ./input -o ./output --mode white
  %(prog)s -i ./input -o ./output --mode mask
  %(prog)s -i ./input -o ./output --mode mask --mask-expand 20 --mask-min 127
  %(prog)s -i ./input -o ./output --weight /path/to/birefnet.safetensors --morph erode --morph-kernel 5 --morph-iterations 2
  %(prog)s -i ./input -o ./output --edge-smooth 3 --morph open --morph-kernel 3
        """,
    )

    parser.add_argument("-i", "--input", required=True,
                        help="输入目录 (或单张图片路径)")
    parser.add_argument("-o", "--output", required=True,
                        help="输出目录")
    parser.add_argument("--mode", choices=["transparent", "white", "mask"],
                        default="transparent",
                        help="输出模式: transparent=透明背景, white=白底, mask=仅mask (默认: transparent)")
    parser.add_argument("--weight",
                        default="/media/yuchuxi/Data/Servers/ComfyUI/models/background_removal/birefnet.safetensors",
                        help="本地模型权重路径 (.safetensors 或 .pth). "
                             "留空则使用 HuggingFace 在线权重.")
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda",
                        help="推理设备 (默认: cuda)")
    parser.add_argument("--norm", choices=["imagenet", "zero_one"],
                        default="imagenet",
                        help="归一化模式: imagenet=官方HF, zero_one=ComfyUI兼容 (默认: imagenet)")

    # 形态学参数
    morph_group = parser.add_argument_group("形态学处理 (处理漏色)")
    morph_group.add_argument("--morph", choices=["erode", "dilate", "open", "close"],
                             default=None,
                             help="形态学操作: erode=腐蚀(缩边,减少漏色), "
                                  "dilate=膨胀(扩边), open=开运算, close=闭运算")
    morph_group.add_argument("--morph-kernel", type=int, default=0,
                             help="形态学核大小 (奇数, 如 3, 5, 7)")
    morph_group.add_argument("--morph-iterations", type=int, default=1,
                             help="形态学迭代次数 (默认: 1)")

    # Mask 扩展/收缩
    expand_group = parser.add_argument_group("Mask 扩展/收缩 (以像素为单位)")
    expand_group.add_argument("--mask-expand", type=int, default=0,
                              help="主体向外扩展 N 像素 (膨胀). 如 --mask-expand 20")
    expand_group.add_argument("--mask-shrink", type=int, default=0,
                              help="主体向内收缩 N 像素 (腐蚀). 如 --mask-shrink 5")

    # Mask 值域
    range_group = parser.add_argument_group("Mask 值域映射 (仅 --mode mask 时生效)")
    range_group.add_argument("--mask-min", type=int, default=0,
                             help="mask 输出最小值 (0~255, 默认 0). 如 127 表示非主体区域=127")
    range_group.add_argument("--mask-max", type=int, default=255,
                             help="mask 输出最大值 (0~255, 默认 255). 如与 --mask-min 配合使用")

    # 边缘平滑
    smooth_group = parser.add_argument_group("边缘平滑")
    smooth_group.add_argument("--edge-smooth", type=int, default=0,
                              help="高斯模糊半径 (奇数), 用于边缘羽化, 减少锯齿. 如 3, 5")

    # 其他
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已存在的输出文件")

    args = parser.parse_args()

    # 验证输入
    if not os.path.exists(args.input):
        print(f"[ERROR] 输入路径不存在: {args.input}")
        sys.exit(1)

    # 处理 weight 参数: 如果传了空字符串则视为不使用本地权重
    weight_path = args.weight if args.weight else None
    if weight_path and not os.path.isfile(weight_path):
        print(f"[WARN] 本地权重文件不存在: {weight_path}")
        print("[INFO] 将使用 HuggingFace 在线权重 (首次使用会自动下载).")
        weight_path = None

    # 加载模型
    model, transform_image = load_model(
        weight_path=weight_path,
        device=args.device,
        norm_mode=args.norm,
    )

    # 批量处理
    process_batch(
        model=model,
        transform_image=transform_image,
        input_dir=args.input,
        output_dir=args.output,
        mode=args.mode,
        morph_op=args.morph,
        morph_kernel=args.morph_kernel,
        morph_iterations=args.morph_iterations,
        edge_smooth=args.edge_smooth,
        mask_expand=args.mask_expand,
        mask_shrink=args.mask_shrink,
        mask_min=args.mask_min,
        mask_max=args.mask_max,
        device=args.device,
        skip_existing=args.skip_existing,
    )


if __name__ == "__main__":
    main()
