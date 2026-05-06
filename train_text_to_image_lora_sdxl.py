#!/usr/bin/env python
# coding=utf-8
# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fine-tuning script for Stable Diffusion XL for text2image with support for LoRA."""

import argparse
import logging
import math
import os
import random
import shutil
from contextlib import nullcontext
from pathlib import Path

from PIL import Image as PILImage

import datasets
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import DistributedDataParallelKwargs, DistributedType, ProjectConfiguration, set_seed
from datasets import load_dataset
from huggingface_hub import create_repo, upload_folder
from packaging import version
from peft import LoraConfig, set_peft_model_state_dict
from peft.utils import get_peft_model_state_dict
from torchvision import transforms
from torchvision.transforms.functional import crop
from tqdm.auto import tqdm
from transformers import AutoTokenizer, PretrainedConfig

import diffusers
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    StableDiffusionXLPipeline,
    UNet2DConditionModel,
)
from diffusers.loaders import StableDiffusionLoraLoaderMixin
from diffusers.optimization import get_scheduler
from diffusers.training_utils import _set_state_dict_into_text_encoder, cast_training_params, compute_snr
from diffusers.utils import (
    check_min_version,
    convert_state_dict_to_diffusers,
    convert_state_dict_to_kohya,
    convert_unet_state_dict_to_peft,
    is_wandb_available,
)
from diffusers.utils.hub_utils import load_or_create_model_card, populate_model_card
from diffusers.utils.import_utils import is_torch_npu_available, is_xformers_available
from diffusers.utils.torch_utils import is_compiled_module


if is_wandb_available():
    import wandb

# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
check_min_version("0.35.0")

logger = get_logger(__name__)
if is_torch_npu_available():
    torch.npu.config.allow_internal_format = False


def pyramid_noise_like(noise: torch.Tensor, iterations: int, discount: float) -> torch.Tensor:
    """Multi-resolution (pyramid) noise; Kohya / Whitaker formulation."""
    b, c, height, width = noise.shape
    for i in range(iterations):
        r = random.random() * 2 + 2
        hn = max(1, int(height / (r**i)))
        wn = max(1, int(width / (r**i)))
        small = torch.randn(b, c, hn, wn, device=noise.device, dtype=noise.dtype)
        upsampled = F.interpolate(small, size=(height, width), mode="bilinear", align_corners=False)
        noise = noise + upsampled * (discount**i)
        if hn == 1 or wn == 1:
            break
    return noise / noise.std().clamp(min=1e-8)


def save_model_card(
    repo_id: str,
    images: list = None,
    base_model: str = None,
    dataset_name: str = None,
    train_text_encoder: bool = False,
    repo_folder: str = None,
    vae_path: str = None,
):
    img_str = ""
    if images is not None:
        for i, image in enumerate(images):
            image.save(os.path.join(repo_folder, f"image_{i}.png"))
            img_str += f"![img_{i}](./image_{i}.png)\n"

    model_description = f"""
# LoRA text2image fine-tuning - {repo_id}

These are LoRA adaption weights for {base_model}. The weights were fine-tuned on the {dataset_name} dataset. You can find some example images in the following. \n
{img_str}

LoRA for the text encoder was enabled: {train_text_encoder}.

Special VAE used for training: {vae_path}.
"""
    model_card = load_or_create_model_card(
        repo_id_or_path=repo_id,
        from_training=True,
        license="creativeml-openrail-m",
        base_model=base_model,
        model_description=model_description,
        inference=True,
    )

    tags = [
        "stable-diffusion-xl",
        "stable-diffusion-xl-diffusers",
        "text-to-image",
        "diffusers",
        "diffusers-training",
        "lora",
    ]
    model_card = populate_model_card(model_card, tags=tags)

    model_card.save(os.path.join(repo_folder, "README.md"))


def log_validation(
    pipeline,
    args,
    accelerator,
    epoch,
    is_final_validation=False,
):
    logger.info(
        f"Running validation... \n Generating {args.num_validation_images} images with prompt:"
        f" {args.validation_prompt}."
    )
    pipeline = pipeline.to(accelerator.device)
    pipeline.set_progress_bar_config(disable=True)

    # run inference
    generator = torch.Generator(device=accelerator.device).manual_seed(args.seed) if args.seed is not None else None
    pipeline_args = {"prompt": args.validation_prompt}
    if torch.backends.mps.is_available():
        autocast_ctx = nullcontext()
    else:
        autocast_ctx = torch.autocast(accelerator.device.type)

    with torch.no_grad():
        with autocast_ctx:
            images = [
                pipeline(**pipeline_args, generator=generator).images[0] for _ in range(args.num_validation_images)
            ]

    # 保存到本地
    save_dir = os.path.join(args.output_dir, "samples")
    os.makedirs(save_dir, exist_ok=True)
    for i, img in enumerate(images):
        img_name = f"sample_{epoch}_{i}.png" if not is_final_validation else f"final_{i}.png"
        img.save(os.path.join(save_dir, img_name))

    for tracker in accelerator.trackers:
        phase_name = "test" if is_final_validation else "validation"
        if tracker.name == "tensorboard":
            np_images = np.stack([np.asarray(img) for img in images])
            tracker.writer.add_images(phase_name, np_images, epoch, dataformats="NHWC")
        if tracker.name == "wandb":
            tracker.log(
                {
                    phase_name: [
                        wandb.Image(image, caption=f"{i}: {args.validation_prompt}") for i, image in enumerate(images)
                    ]
                }
            )
    return images


def import_model_class_from_model_name_or_path(
    pretrained_model_name_or_path: str, revision: str, subfolder: str = "text_encoder"
):
    if pretrained_model_name_or_path.endswith(".safetensors"):
        if subfolder == "text_encoder":
            from transformers import CLIPTextModel
            return CLIPTextModel
        elif subfolder == "text_encoder_2":
            from transformers import CLIPTextModelWithProjection
            return CLIPTextModelWithProjection

    text_encoder_config = PretrainedConfig.from_pretrained(
        pretrained_model_name_or_path, subfolder=subfolder, revision=revision
    )
    model_class = text_encoder_config.architectures[0]

    if model_class == "CLIPTextModel":
        from transformers import CLIPTextModel

        return CLIPTextModel
    elif model_class == "CLIPTextModelWithProjection":
        from transformers import CLIPTextModelWithProjection

        return CLIPTextModelWithProjection
    else:
        raise ValueError(f"{model_class} is not supported.")


def parse_args(input_args=None):
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--pretrained_vae_model_name_or_path",
        type=str,
        default=None,
        help="Path to pretrained VAE model with better numerical stability. More details: https://github.com/huggingface/diffusers/pull/4038.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--variant",
        type=str,
        default=None,
        help="Variant of the model files of the pretrained model identifier from huggingface.co/models, 'e.g.' fp16",
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset). It can also be a path pointing to a local copy of a dataset in your filesystem,"
            " or to a folder containing files that 🤗 Datasets can understand."
        ),
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The config of the Dataset, leave as None if there's only one config.",
    )
    parser.add_argument(
        "--train_data_dir",
        type=str,
        default=None,
        help=(
            "A folder containing the training data. Folder contents must follow the structure described in"
            " https://huggingface.co/docs/datasets/image_dataset#imagefolder. In particular, a `metadata.jsonl` file"
            " must exist to provide the captions for the images. Ignored if `dataset_name` is specified."
        ),
    )
    parser.add_argument(
        "--image_column", type=str, default="image", help="The column of the dataset containing an image."
    )
    parser.add_argument(
        "--caption_column",
        type=str,
        default="text",
        help="The column of the dataset containing a caption or a list of captions.",
    )
    parser.add_argument(
        "--validation_prompt",
        type=str,
        default=None,
        help="A prompt that is used during validation to verify that the model is learning.",
    )
    parser.add_argument(
        "--num_validation_images",
        type=int,
        default=4,
        help="Number of images that should be generated during validation with `validation_prompt`.",
    )
    parser.add_argument(
        "--validation_epochs",
        type=int,
        default=1,
        help=(
            "Run fine-tuning validation every X epochs. The validation process consists of running the prompt"
            " `args.validation_prompt` multiple times: `args.num_validation_images`."
        ),
    )
    parser.add_argument(
        "--max_train_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        ),
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="sd-model-finetuned-lora",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument(
        "--resolution",
        type=int,
        default=1024,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--center_crop",
        default=False,
        action="store_true",
        help=(
            "Whether to center crop the input images to the resolution. If not set, the images will be randomly"
            " cropped. The images will be resized to the resolution first before cropping."
        ),
    )
    parser.add_argument(
        "--random_flip",
        action="store_true",
        help="whether to randomly flip images horizontally",
    )
    parser.add_argument(
        "--train_text_encoder",
        action="store_true",
        help="Whether to train the text encoder. If set, the text encoder should be float32 precision.",
    )
    parser.add_argument(
        "--train_batch_size", type=int, default=16, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument("--num_train_epochs", type=int, default=100)
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=int,
        default=500,
        help=(
            "Save a checkpoint of the training state every X updates. These checkpoints can be used both as final"
            " checkpoints in case they are better than the last checkpoint, and are also suitable for resuming"
            " training using `--resume_from_checkpoint`."
        ),
    )
    parser.add_argument(
        "--checkpoints_total_limit",
        type=int,
        default=None,
        help=("Max number of checkpoints to store."),
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help=(
            "Whether training should be resumed from a previous checkpoint. Use a path saved by"
            ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
        ),
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-4,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--text_encoder_lr",
        type=float,
        default=None,
        help="Initial learning rate for the text encoder (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps", type=int, default=500, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument(
        "--snr_gamma",
        type=float,
        default=None,
        help="SNR weighting gamma to be used if rebalancing the loss. Recommended value is 5.0. "
        "More details here: https://huggingface.co/papers/2303.09556.",
    )
    parser.add_argument(
        "--lora_alpha",
        type=int,
        default=None,
        help=("The alpha parameter for LoRA scaling. If None, defaults to rank."),
    )
    parser.add_argument(
        "--use_dora",
        action="store_true",
        help=(
            "Enable DoRA (Weight-Decomposed Low-Rank Adaptation, https://huggingface.co/papers/2402.09353) "
            "via PEFT LoraConfig(use_dora=True). Slightly more overhead than plain LoRA; not compatible with "
            "some PEFT options (see peft docs)."
        ),
    )
    parser.add_argument(
        "--unet_target_modules",
        type=str,
        default="to_k,to_q,to_v,to_out.0",
        help="Comma-separated list of UNet target modules for LoRA.",
    )
    parser.add_argument(
        "--text_encoder_target_modules",
        type=str,
        default="q_proj,k_proj,v_proj,out_proj",
        help="Comma-separated list of Text Encoder target modules for LoRA.",
    )
    parser.add_argument(
        "--trigger_word",
        type=str,
        default=None,
        help="A trigger word to prepend to each caption.",
    )
    parser.add_argument(
        "--shuffle_tags",
        action="store_true",
        help="Whether to shuffle tags (comma separated) in the caption.",
    )
    parser.add_argument(
        "--priority_tags",
        type=str,
        default="sketch,close-up,low quality",
        help="Comma-separated tags that always stay near the front (after trigger word) and never get truncated.",
    )
    parser.add_argument(
        "--cache_latents_to_disk",
        action="store_true",
        help="Whether to cache VAE latents to disk.",
    )
    parser.add_argument(
        "--latents_cache_dir",
        type=str,
        default="./latents_cache",
        help="Directory to store cached latents.",
    )
    parser.add_argument(
        "--precompute_vae_latents",
        action="store_true",
        help=(
            "After the dataset is built, on the main thread iterate train_dataset[i] once to write all VAE latents "
            "to --latents_cache_dir (no DataLoader), then move VAE to CPU to free VRAM. Requires --cache_latents_to_disk. "
            "Distributed: each rank only encodes its shard (DistributedSampler indices). "
            "VAE is moved back to the accelerator device only for validation / final inference."
        ),
    )
    parser.add_argument(
        "--allow_tf32",
        action="store_true",
        help=(
            "Whether or not to allow TF32 on Ampere GPUs. Can be used to speed up training. For more information, see"
            " https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices"
        ),
    )
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the data will be loaded in the main process."
        ),
    )
    parser.add_argument(
        "--use_8bit_adam", action="store_true", help="Whether or not to use 8-bit Adam from bitsandbytes."
    )
    parser.add_argument("--adam_beta1", type=float, default=0.9, help="The beta1 parameter for the Adam optimizer.")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
    parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument("--hub_token", type=str, default=None, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--prediction_type",
        type=str,
        default=None,
        help="The prediction_type that shall be used for training. Choose between 'epsilon' or 'v_prediction' or leave `None`. If left to `None` the default prediction type of the scheduler: `noise_scheduler.config.prediction_type` is chosen.",
    )
    parser.add_argument(
        "--hub_model_id",
        type=str,
        default=None,
        help="The name of the repository to keep in sync with the local `output_dir`.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`'
            ' (default), `"wandb"` and `"comet_ml"`. Use `"all"` to report to all integrations.'
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    parser.add_argument(
        "--enable_xformers_memory_efficient_attention", action="store_true", help="Whether or not to use xformers."
    )
    parser.add_argument(
        "--enable_npu_flash_attention", action="store_true", help="Whether or not to use npu flash attention."
    )
    parser.add_argument("--noise_offset", type=float, default=0, help="The scale of noise offset.")
    parser.add_argument(
        "--multires_noise_iterations",
        type=int,
        default=0,
        help=(
            "Multi-resolution (pyramid) noise; 0 disables. Typical 6–10. "
            "https://wandb.ai/johnowhitaker/multires_noise/reports/Multi-Resolution-Noise-for-Diffusion-Model-Training--VmlldzozNjYyOTU2 "
            "Can be combined with --noise_offset (offset applied first, then pyramid, same order as Kohya sd-scripts)."
        ),
    )
    parser.add_argument(
        "--multires_noise_discount",
        type=float,
        default=0.3,
        help="Per-level discount for multires noise; only used when --multires_noise_iterations > 0.",
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=4,
        help=("The dimension of the LoRA update matrices."),
    )
    parser.add_argument(
        "--debug_loss",
        action="store_true",
        help="debug loss for each image, if filenames are available in the dataset",
    )
    parser.add_argument(
        "--image_interpolation_mode",
        type=str,
        default="lanczos",
        choices=[
            f.lower() for f in dir(transforms.InterpolationMode) if not f.startswith("__") and not f.endswith("__")
        ],
        help="The image interpolation method to use for resizing images.",
    )
    parser.add_argument(
        "--image_preprocess_mode",
        type=str,
        default="original",
        choices=["original", "max_total_pixels", "max_pixels_1536sq"],
        help=(
            "图像预处理：original 保持解码后尺寸；max_total_pixels 按比例缩小使宽×高≤--max_image_total_pixels；"
            "max_pixels_1536sq 等同于 max_total_pixels 且固定为 1536×1536（兼容旧参数）。"
        ),
    )
    parser.add_argument(
        "--max_image_total_pixels",
        type=int,
        default=1536 * 1536,
        help="image_preprocess_mode=max_total_pixels 时，单张图总像素数（宽×高）上限；更小则不缩放。",
    )

    if input_args is not None:
        args = parser.parse_args(input_args)
    else:
        args = parser.parse_args()

    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    # Sanity checks
    if args.dataset_name is None and args.train_data_dir is None:
        raise ValueError("Need either a dataset name or a training folder.")
    if args.image_preprocess_mode in ("max_total_pixels", "max_pixels_1536sq") and args.max_image_total_pixels < 1:
        raise ValueError("--max_image_total_pixels must be >= 1.")
    if args.precompute_vae_latents and not args.cache_latents_to_disk:
        raise ValueError("--precompute_vae_latents requires --cache_latents_to_disk.")

    return args


DATASET_NAME_MAPPING = {
    "lambdalabs/naruto-blip-captions": ("image", "text"),
}


def _pil_resample_from_interpolation_name(name: str):
    """Map torchvision-style interpolation name to PIL Resampling."""
    key = (name or "lanczos").upper()
    return getattr(PILImage.Resampling, key, PILImage.Resampling.LANCZOS)


def resize_pil_image_max_total_pixels(image, max_pixels: int, resampling):
    """将 PIL 图像按比例缩小，使 宽×高 的总像素数不超过 max_pixels；已更小则不变。"""
    w, h = image.size
    if w * h <= max_pixels:
        return image
    scale = math.sqrt(max_pixels / (w * h))
    new_w = max(1, int(math.floor(w * scale)))
    new_h = max(1, int(math.floor(h * scale)))
    while new_w * new_h > max_pixels:
        if new_w >= new_h:
            new_w -= 1
        else:
            new_h -= 1
    return image.resize((new_w, new_h), resample=resampling)


def tokenize_prompt(tokenizer, prompt):
    text_inputs = tokenizer(
        prompt,
        padding="max_length",
        max_length=tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_input_ids = text_inputs.input_ids
    return text_input_ids


# Adapted from pipelines.StableDiffusionXLPipeline.encode_prompt
def encode_prompt(text_encoders, tokenizers, prompt, text_input_ids_list=None):
    prompt_embeds_list = []

    for i, text_encoder in enumerate(text_encoders):
        if tokenizers is not None:
            tokenizer = tokenizers[i]
            text_input_ids = tokenize_prompt(tokenizer, prompt)
        else:
            assert text_input_ids_list is not None
            text_input_ids = text_input_ids_list[i]

        prompt_embeds = text_encoder(
            text_input_ids.to(text_encoder.device), output_hidden_states=True, return_dict=False
        )

        # We are only ALWAYS interested in the pooled output of the final text encoder
        pooled_prompt_embeds = prompt_embeds[0]
        prompt_embeds = prompt_embeds[-1][-2]
        bs_embed, seq_len, _ = prompt_embeds.shape
        prompt_embeds = prompt_embeds.view(bs_embed, seq_len, -1)
        prompt_embeds_list.append(prompt_embeds)

    prompt_embeds = torch.concat(prompt_embeds_list, dim=-1)
    pooled_prompt_embeds = pooled_prompt_embeds.view(bs_embed, -1)
    return prompt_embeds, pooled_prompt_embeds


def main(args):
    if args.report_to == "wandb" and args.hub_token is not None:
        raise ValueError(
            "You cannot use both --report_to=wandb and --hub_token due to a security risk of exposing your token."
            " Please use `hf auth login` to authenticate with the Hub."
        )

    logging_dir = Path(args.output_dir, args.logging_dir)

    if torch.backends.mps.is_available() and args.mixed_precision == "bf16":
        # due to pytorch#99272, MPS does not yet support bfloat16.
        raise ValueError(
            "Mixed precision training with bfloat16 is not supported on MPS. Please use fp16 (recommended) or fp32 instead."
        )

    accelerator_project_config = ProjectConfiguration(project_dir=args.output_dir, logging_dir=logging_dir)
    kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
        kwargs_handlers=[kwargs],
    )

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

        if args.push_to_hub:
            repo_id = create_repo(
                repo_id=args.hub_model_id or Path(args.output_dir).name, exist_ok=True, token=args.hub_token
            ).repo_id

    # Load the tokenizers
    is_single_file = args.pretrained_model_name_or_path.endswith((".safetensors", ".ckpt"))
    if is_single_file:
        pipe = StableDiffusionXLPipeline.from_single_file(
            args.pretrained_model_name_or_path,
            torch_dtype=torch.float32,
        )
        tokenizer_one = pipe.tokenizer
        tokenizer_two = pipe.tokenizer_2
        text_encoder_one = pipe.text_encoder
        text_encoder_two = pipe.text_encoder_2
        vae = pipe.vae
        unet = pipe.unet
        inference_scheduler = pipe.scheduler
        ps_cfg = pipe.scheduler.config
        SDXL_BETA_START, SDXL_BETA_END = 0.00085, 0.012
        noise_scheduler = DDPMScheduler(
            num_train_timesteps=getattr(ps_cfg, "num_train_timesteps", 1000),
            beta_start=getattr(ps_cfg, "beta_start", SDXL_BETA_START),
            beta_end=getattr(ps_cfg, "beta_end", SDXL_BETA_END),
            beta_schedule=getattr(ps_cfg, "beta_schedule", "scaled_linear"),
            prediction_type=getattr(ps_cfg, "prediction_type", "epsilon"),
        )
        sc = noise_scheduler.config
        logger.info(
            f"训练 scheduler: prediction_type={sc.prediction_type}, "
            f"beta_start={sc.beta_start}, beta_end={sc.beta_end}, "
            f"beta_schedule={sc.beta_schedule}, T={sc.num_train_timesteps}"
        )
        del pipe
        torch.cuda.empty_cache()
    else:
        tokenizer_one = AutoTokenizer.from_pretrained(
            args.pretrained_model_name_or_path,
            subfolder="tokenizer",
            revision=args.revision,
            use_fast=False,
        )
        tokenizer_two = AutoTokenizer.from_pretrained(
            args.pretrained_model_name_or_path,
            subfolder="tokenizer_2",
            revision=args.revision,
            use_fast=False,
        )

        # import correct text encoder classes
        text_encoder_cls_one = import_model_class_from_model_name_or_path(
            args.pretrained_model_name_or_path, args.revision
        )
        text_encoder_cls_two = import_model_class_from_model_name_or_path(
            args.pretrained_model_name_or_path, args.revision, subfolder="text_encoder_2"
        )

        # Load scheduler and models
        noise_scheduler = DDPMScheduler.from_pretrained(args.pretrained_model_name_or_path, subfolder="scheduler")
        inference_scheduler = None
        text_encoder_one = text_encoder_cls_one.from_pretrained(
            args.pretrained_model_name_or_path, subfolder="text_encoder", revision=args.revision, variant=args.variant
        )
        text_encoder_two = text_encoder_cls_two.from_pretrained(
            args.pretrained_model_name_or_path, subfolder="text_encoder_2", revision=args.revision, variant=args.variant
        )
        vae_path = (
            args.pretrained_model_name_or_path
            if args.pretrained_vae_model_name_or_path is None
            else args.pretrained_vae_model_name_or_path
        )
        vae = AutoencoderKL.from_pretrained(
            vae_path,
            subfolder="vae" if args.pretrained_vae_model_name_or_path is None else None,
            revision=args.revision,
            variant=args.variant,
        )
        unet = UNet2DConditionModel.from_pretrained(
            args.pretrained_model_name_or_path, subfolder="unet", revision=args.revision, variant=args.variant
        )

    # We only train the additional adapter LoRA layers
    vae.requires_grad_(False)
    text_encoder_one.requires_grad_(False)
    text_encoder_two.requires_grad_(False)
    unet.requires_grad_(False)

    # For mixed precision training we cast all non-trainable weights (vae, non-lora text_encoder and non-lora unet) to half-precision
    # as these weights are only used for inference, keeping weights in full precision is not required.
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move unet, vae and text_encoder to device and cast to weight_dtype
    # The VAE is in float32 to avoid NaN losses.
    unet.to(accelerator.device, dtype=weight_dtype)

    if args.pretrained_vae_model_name_or_path is None:
        vae.to(accelerator.device, dtype=torch.float32)
    else:
        vae.to(accelerator.device, dtype=weight_dtype)
    text_encoder_one.to(accelerator.device, dtype=weight_dtype)
    text_encoder_two.to(accelerator.device, dtype=weight_dtype)

    if args.enable_npu_flash_attention:
        if is_torch_npu_available():
            logger.info("npu flash attention enabled.")
            unet.enable_npu_flash_attention()
        else:
            raise ValueError("npu flash attention requires torch_npu extensions and is supported only on npu devices.")

    if args.enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            import xformers

            xformers_version = version.parse(xformers.__version__)
            if xformers_version == version.parse("0.0.16"):
                logger.warning(
                    "xFormers 0.0.16 cannot be used for training in some GPUs. If you observe problems during training, please update xFormers to at least 0.0.17. See https://huggingface.co/docs/diffusers/main/en/optimization/xformers for more details."
                )
            unet.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError("xformers is not available. Make sure it is installed correctly")

    # now we will add new LoRA weights to the attention layers
    # Set correct lora layers
    unet_lora_config = LoraConfig(
        r=args.rank,
        lora_alpha=args.lora_alpha or args.rank,
        init_lora_weights="gaussian",
        target_modules=[m.strip() for m in args.unet_target_modules.split(",") if m.strip()],
        use_dora=args.use_dora,
    )

    unet.add_adapter(unet_lora_config)

    # The text encoder comes from 🤗 transformers, we will also attach adapters to it.
    if args.train_text_encoder:
        # ensure that dtype is float32, even if rest of the model that isn't trained is loaded in fp16
        text_lora_config = LoraConfig(
            r=args.rank,
            lora_alpha=args.lora_alpha or args.rank,
            init_lora_weights="gaussian",
            target_modules=[m.strip() for m in args.text_encoder_target_modules.split(",") if m.strip()],
            use_dora=args.use_dora,
        )
        text_encoder_one.add_adapter(text_lora_config)
        text_encoder_two.add_adapter(text_lora_config)

    def unwrap_model(model):
        model = accelerator.unwrap_model(model)
        model = model._orig_mod if is_compiled_module(model) else model
        return model

    def save_kohya_sdxl_lora(output_dir, unet_peft_state_dict, te1_peft_state_dict=None, te2_peft_state_dict=None):
        from safetensors.torch import save_file
        merged = {f"unet.{k}": v for k, v in unet_peft_state_dict.items()}
        if te1_peft_state_dict:
            merged.update({f"text_encoder.{k}": v for k, v in te1_peft_state_dict.items()})
        if te2_peft_state_dict:
            merged.update({f"text_encoder_2.{k}": v for k, v in te2_peft_state_dict.items()})
        kohya_dict = convert_state_dict_to_kohya(merged)
        save_file(kohya_dict, os.path.join(output_dir, "lora.safetensors"))

    # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
    def save_model_hook(models, weights, output_dir):
        if accelerator.is_main_process:
            # there are only two options here. Either are just the unet attn processor layers
            # or there are the unet and text encoder attn layers
            unet_lora_layers_to_save = None
            text_encoder_one_lora_layers_to_save = None
            text_encoder_two_lora_layers_to_save = None

            from safetensors.torch import save_file

            for model in models:
                if isinstance(unwrap_model(model), type(unwrap_model(unet))):
                    unet_lora_layers_to_save = convert_state_dict_to_diffusers(get_peft_model_state_dict(model))
                elif isinstance(unwrap_model(model), type(unwrap_model(text_encoder_one))):
                    text_encoder_one_lora_layers_to_save = convert_state_dict_to_diffusers(
                        get_peft_model_state_dict(model)
                    )
                elif isinstance(unwrap_model(model), type(unwrap_model(text_encoder_two))):
                    text_encoder_two_lora_layers_to_save = convert_state_dict_to_diffusers(
                        get_peft_model_state_dict(model)
                    )
                else:
                    raise ValueError(f"unexpected save model: {model.__class__}")

                # make sure to pop weight so that corresponding model is not saved again
                if weights:
                    weights.pop()

            # Save in Diffusers format
            StableDiffusionXLPipeline.save_lora_weights(
                output_dir,
                unet_lora_layers=unet_lora_layers_to_save,
                text_encoder_lora_layers=text_encoder_one_lora_layers_to_save,
                text_encoder_2_lora_layers=text_encoder_two_lora_layers_to_save,
            )

            # Save in Kohya format for ComfyUI/WebUI
            unet_peft_dict = None
            te1_peft_dict = None
            te2_peft_dict = None
            for model in models:
                if isinstance(unwrap_model(model), type(unwrap_model(unet))):
                    unet_peft_dict = get_peft_model_state_dict(model)
                elif isinstance(unwrap_model(model), type(unwrap_model(text_encoder_one))):
                    te1_peft_dict = get_peft_model_state_dict(model)
                elif isinstance(unwrap_model(model), type(unwrap_model(text_encoder_two))):
                    te2_peft_dict = get_peft_model_state_dict(model)

            if unet_peft_dict:
                save_kohya_sdxl_lora(output_dir, unet_peft_dict, te1_peft_dict, te2_peft_dict)
    

    def load_model_hook(models, input_dir):
        unet_ = None
        text_encoder_one_ = None
        text_encoder_two_ = None

        while len(models) > 0:
            model = models.pop()

            if isinstance(model, type(unwrap_model(unet))):
                unet_ = model
            elif isinstance(model, type(unwrap_model(text_encoder_one))):
                text_encoder_one_ = model
            elif isinstance(model, type(unwrap_model(text_encoder_two))):
                text_encoder_two_ = model
            else:
                raise ValueError(f"unexpected save model: {model.__class__}")

        lora_state_dict, _ = StableDiffusionLoraLoaderMixin.lora_state_dict(input_dir)
        unet_state_dict = {f"{k.replace('unet.', '')}": v for k, v in lora_state_dict.items() if k.startswith("unet.")}
        unet_state_dict = convert_unet_state_dict_to_peft(unet_state_dict)
        incompatible_keys = set_peft_model_state_dict(unet_, unet_state_dict, adapter_name="default")
        if incompatible_keys is not None:
            # check only for unexpected keys
            unexpected_keys = getattr(incompatible_keys, "unexpected_keys", None)
            if unexpected_keys:
                logger.warning(
                    f"Loading adapter weights from state_dict led to unexpected keys not found in the model: "
                    f" {unexpected_keys}. "
                )

        if args.train_text_encoder:
            _set_state_dict_into_text_encoder(lora_state_dict, prefix="text_encoder.", text_encoder=text_encoder_one_)

            _set_state_dict_into_text_encoder(
                lora_state_dict, prefix="text_encoder_2.", text_encoder=text_encoder_two_
            )

        # Make sure the trainable params are in float32. This is again needed since the base models
        # are in `weight_dtype`. More details:
        # https://github.com/huggingface/diffusers/pull/6514#discussion_r1449796804
        if args.mixed_precision == "fp16":
            models = [unet_]
            if args.train_text_encoder:
                models.extend([text_encoder_one_, text_encoder_two_])
            cast_training_params(models, dtype=torch.float32)

    accelerator.register_save_state_pre_hook(save_model_hook)
    accelerator.register_load_state_pre_hook(load_model_hook)

    if args.gradient_checkpointing:
        unet.enable_gradient_checkpointing()
        if args.train_text_encoder:
            text_encoder_one.gradient_checkpointing_enable()
            text_encoder_two.gradient_checkpointing_enable()

    # Enable TF32 for faster training on Ampere GPUs,
    # cf https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices
    if args.allow_tf32:
        torch.backends.cuda.matmul.allow_tf32 = True

    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate * args.gradient_accumulation_steps * args.train_batch_size * accelerator.num_processes
        )
        if args.text_encoder_lr:
            args.text_encoder_lr = (
                args.text_encoder_lr * args.gradient_accumulation_steps * args.train_batch_size * accelerator.num_processes
            )

    # Make sure the trainable params are in float32.
    if args.mixed_precision == "fp16":
        models = [unet]
        if args.train_text_encoder:
            models.extend([text_encoder_one, text_encoder_two])
        cast_training_params(models, dtype=torch.float32)

    # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
    if args.use_8bit_adam:
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
            )

        optimizer_class = bnb.optim.AdamW8bit
    else:
        optimizer_class = torch.optim.AdamW

    # Optimizer creation
    params_to_optimize = [
        {"params": [p for p in unet.parameters() if p.requires_grad], "lr": args.learning_rate}
    ]
    if args.train_text_encoder:
        text_encoder_lr = args.text_encoder_lr if args.text_encoder_lr else args.learning_rate
        params_to_optimize.append(
            {"params": [p for p in text_encoder_one.parameters() if p.requires_grad], "lr": text_encoder_lr}
        )
        params_to_optimize.append(
            {"params": [p for p in text_encoder_two.parameters() if p.requires_grad], "lr": text_encoder_lr}
        )

    optimizer = optimizer_class(
        params_to_optimize,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    if args.dataset_name is not None and args.dataset_name != "None":
        # Downloading and loading a dataset from the hub.
        dataset = load_dataset(
            args.dataset_name, args.dataset_config_name, cache_dir=args.cache_dir, data_dir=args.train_data_dir
        )
    else:
        data_files = {}
        if args.train_data_dir is not None:
            metadata_path = os.path.join(args.train_data_dir, "metadata.jsonl")
            if os.path.exists(metadata_path):
                data_files["train"] = metadata_path
            else:
                data_files["train"] = os.path.join(args.train_data_dir, "datasets/**")
        
        dataset = load_dataset(
            "imagefolder",
            data_files=data_files,
            cache_dir=args.cache_dir,
        )
        # See more about loading custom images at
        # https://huggingface.co/docs/datasets/v2.4.0/en/image_load#imagefolder

    # Preprocessing the datasets.
    # We need to tokenize inputs and targets.
    column_names = dataset["train"].column_names

    # 6. Get the column names for input/target.
    dataset_columns = DATASET_NAME_MAPPING.get(args.dataset_name, None)
    if args.image_column is None:
        image_column = dataset_columns[0] if dataset_columns is not None else column_names[0]
    else:
        image_column = args.image_column
        if image_column not in column_names:
            raise ValueError(
                f"--image_column' value '{args.image_column}' needs to be one of: {', '.join(column_names)}"
            )
    if args.caption_column is None:
        caption_column = dataset_columns[1] if dataset_columns is not None else column_names[1]
    else:
        caption_column = args.caption_column
        if caption_column not in column_names:
            raise ValueError(
                f"--caption_column' value '{args.caption_column}' needs to be one of: {', '.join(column_names)}"
            )

    # Preprocessing the datasets.
    # We need to tokenize input captions and transform the images.
    priority_tag_set = {t.strip().lower() for t in (args.priority_tags or "").split(",") if t.strip()}

    def _fit_tags_to_77(tags, tok1, tok2, must_keep=None):
        """Greedily add tags until either tokenizer hits 77 tokens.
        Tags in must_keep are always included (tried first to reserve budget)."""
        must_keep = must_keep or set()
        forced = [t for t in tags if t.lower() in must_keep]
        others = [t for t in tags if t.lower() not in must_keep]
        kept = list(forced)
        for t in others:
            candidate = ", ".join(kept + [t])
            l1 = len(tok1(candidate, add_special_tokens=True).input_ids)
            l2 = len(tok2(candidate, add_special_tokens=True).input_ids)
            if l1 <= 77 and l2 <= 77:
                kept.append(t)
        result = [t for t in tags if t in kept]
        return ", ".join(result) if result else (tags[0] if tags else "")

    def tokenize_captions(examples, is_train=True):
        captions = []
        for caption in examples[caption_column]:
            if isinstance(caption, str):
                pass
            elif isinstance(caption, (list, np.ndarray)):
                caption = random.choice(caption) if is_train else caption[0]
            else:
                raise ValueError(
                    f"Caption column `{caption_column}` should contain either strings or lists of strings."
                )

            if is_train:
                tags = [t.strip() for t in caption.split(",") if t.strip()]
                if args.trigger_word:
                    tags = [t for t in tags if t != args.trigger_word]
                priority = [t for t in tags if t.lower() in priority_tag_set]
                rest = [t for t in tags if t.lower() not in priority_tag_set]
                if args.shuffle_tags:
                    random.shuffle(rest)
                    for pt in priority:
                        rest.insert(random.randint(0, len(rest)), pt)
                else:
                    rest = priority + rest
                ordered = []
                if args.trigger_word:
                    ordered.append(args.trigger_word)
                ordered.extend(rest)
                caption = _fit_tags_to_77(ordered, tokenizer_one, tokenizer_two, priority_tag_set)

            captions.append(caption)
        tokens_one = tokenize_prompt(tokenizer_one, captions)
        tokens_two = tokenize_prompt(tokenizer_two, captions)
        return tokens_one, tokens_two

    # Get the specified interpolation method from the args
    interpolation = getattr(transforms.InterpolationMode, args.image_interpolation_mode.upper(), None)

    # Raise an error if the interpolation method is invalid
    if interpolation is None:
        raise ValueError(f"Unsupported interpolation mode {args.image_interpolation_mode}.")
    # Preprocessing the datasets.
    train_resize = transforms.Resize(args.resolution, interpolation=interpolation)  # Use dynamic interpolation method
    train_crop = transforms.CenterCrop(args.resolution) if args.center_crop else transforms.RandomCrop(args.resolution)
    train_flip = transforms.RandomHorizontalFlip(p=1.0)
    train_transforms = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )

    def preprocess_train(examples):
        # 缓存逻辑注入
        if args.cache_latents_to_disk:
            import hashlib
            os.makedirs(args.latents_cache_dir, exist_ok=True)
            
        images = [image.convert("RGB") for image in examples[image_column]]
        pil_resample = _pil_resample_from_interpolation_name(args.image_interpolation_mode)
        if args.image_preprocess_mode == "max_pixels_1536sq":
            max_px = 1536 * 1536
        elif args.image_preprocess_mode == "max_total_pixels":
            max_px = args.max_image_total_pixels
        else:
            max_px = None
        if max_px is not None:
            images = [resize_pil_image_max_total_pixels(im, max_px, pil_resample) for im in images]
        # image aug
        original_sizes = []
        all_images = []
        crop_top_lefts = []
        latent_paths = []
        
        for i, image in enumerate(images):
            # 获取当前图片的真实宽高（若已按总像素缩小则为缩放后的尺寸，供 SDXL time_ids）
            h_orig, w_orig = image.height, image.width
            original_sizes.append((h_orig, w_orig))
            
            # 不再进行 Resize 和 Crop，直接使用原图
            # 如果需要随机翻转
            if args.random_flip and random.random() < 0.5:
                image = train_flip(image)
            
            # 由于没有 Crop，坐标永远是 (0, 0)
            y1, x1 = 0, 0
            crop_top_left = (y1, x1)
            crop_top_lefts.append(crop_top_left)
            
            image = train_transforms(image)
            all_images.append(image)
            
            if args.cache_latents_to_disk:
                img_obj = examples[image_column][i]
                img_path = getattr(img_obj, "filename", str(i))
                h_hash = hashlib.md5(img_path.encode()).hexdigest()
                l_path = os.path.join(args.latents_cache_dir, f"{h_hash}.pt")
                
                if not os.path.exists(l_path):
                    with torch.no_grad():
                        # 临时将图片转为 latent 并保存
                        pixel_val = image.unsqueeze(0).to(accelerator.device, dtype=vae.dtype)
                        latents = vae.encode(pixel_val).latent_dist.sample()
                        latents = latents * vae.config.scaling_factor
                        torch.save(latents.cpu(), l_path)
                latent_paths.append(l_path)

        examples["original_sizes"] = original_sizes
        examples["crop_top_lefts"] = crop_top_lefts
        examples["pixel_values"] = all_images
        if args.cache_latents_to_disk:
            examples["latent_paths"] = latent_paths
            
        tokens_one, tokens_two = tokenize_captions(examples)
        examples["input_ids_one"] = tokens_one
        examples["input_ids_two"] = tokens_two
        return examples

    with accelerator.main_process_first():
        if args.max_train_samples is not None:
            dataset["train"] = dataset["train"].shuffle(seed=args.seed).select(range(args.max_train_samples))
        # Set the training transforms
        train_dataset = dataset["train"].with_transform(preprocess_train, output_all_columns=True)

    def collate_fn(examples):
        pixel_values = torch.stack([example["pixel_values"] for example in examples])
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()
        original_sizes = [example["original_sizes"] for example in examples]
        crop_top_lefts = [example["crop_top_lefts"] for example in examples]
        input_ids_one = torch.stack([example["input_ids_one"] for example in examples])
        input_ids_two = torch.stack([example["input_ids_two"] for example in examples])
        result = {
            "pixel_values": pixel_values,
            "input_ids_one": input_ids_one,
            "input_ids_two": input_ids_two,
            "original_sizes": original_sizes,
            "crop_top_lefts": crop_top_lefts,
        }
        
        if args.cache_latents_to_disk:
            latents = torch.cat([torch.load(example["latent_paths"]) for example in examples])
            result["latents"] = latents

        filenames = [example["filenames"] for example in examples if "filenames" in example]
        if filenames:
            result["filenames"] = filenames
        return result

    # DataLoaders creation:
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=collate_fn,
        batch_size=args.train_batch_size,
        num_workers=args.dataloader_num_workers,
    )

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
    )

    # Prepare everything with our `accelerator`.
    if args.train_text_encoder:
        unet, text_encoder_one, text_encoder_two, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, text_encoder_one, text_encoder_two, optimizer, train_dataloader, lr_scheduler
        )
    else:
        unet, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, optimizer, train_dataloader, lr_scheduler
        )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        accelerator.init_trackers("text2image-fine-tune", config=vars(args))

    # Train!
    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    global_step = 0
    first_epoch = 0

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(args.output_dir, path))
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch

    else:
        initial_global_step = 0

    def ensure_vae_on_accelerator_for_inference():
        if next(vae.parameters()).device == accelerator.device:
            return
        if args.pretrained_vae_model_name_or_path is None:
            vae.to(accelerator.device, dtype=torch.float32)
        else:
            vae.to(accelerator.device, dtype=weight_dtype)

    def offload_vae_to_cpu_if_precomputed():
        if not args.precompute_vae_latents:
            return
        vae.to("cpu")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    if args.precompute_vae_latents:
        logger.info("Precomputing VAE latents on main thread (train_dataset[i]), then offloading VAE to CPU.")
        vae.eval()
        from accelerate.data_loader import get_sampler
        from torch.utils.data.distributed import DistributedSampler

        inner_sampler = get_sampler(train_dataloader)
        if isinstance(inner_sampler, DistributedSampler):
            inner_sampler.set_epoch(0)
            precompute_indices = list(inner_sampler)
        else:
            precompute_indices = list(range(len(train_dataset)))
        with torch.no_grad():
            for i in tqdm(
                precompute_indices,
                desc="Precompute latents",
                disable=not accelerator.is_local_main_process,
            ):
                _ = train_dataset[i]
        accelerator.wait_for_everyone()
        offload_vae_to_cpu_if_precomputed()
        logger.info("Precompute done; VAE on CPU. Training will load latents from %s", args.latents_cache_dir)

    progress_bar = tqdm(
        range(0, args.max_train_steps),
        initial=initial_global_step,
        desc="Steps",
        # Only show the progress bar once on each machine.
        disable=not accelerator.is_local_main_process,
    )

    images = None  # last validation samples; push_to_hub / model card

    for epoch in range(first_epoch, args.num_train_epochs):
        unet.train()
        if args.train_text_encoder:
            text_encoder_one.train()
            text_encoder_two.train()
        train_loss = 0.0
        for step, batch in enumerate(train_dataloader):
            with accelerator.accumulate(unet):
                # Convert images to latent space
                if "latents" in batch:
                    model_input = batch["latents"].to(accelerator.device, dtype=weight_dtype)
                else:
                    if args.pretrained_vae_model_name_or_path is not None:
                        pixel_values = batch["pixel_values"].to(dtype=weight_dtype)
                    else:
                        pixel_values = batch["pixel_values"]

                    model_input = vae.encode(pixel_values).latent_dist.sample()
                    model_input = model_input * vae.config.scaling_factor
                    if args.pretrained_vae_model_name_or_path is None:
                        model_input = model_input.to(weight_dtype)

                # Sample noise that we'll add to the latents (Kohya order: offset then multires pyramid)
                noise = torch.randn_like(model_input)
                if args.noise_offset:
                    # https://www.crosslabs.org//blog/diffusion-with-offset-noise
                    noise = noise + args.noise_offset * torch.randn(
                        (model_input.shape[0], model_input.shape[1], 1, 1), device=model_input.device
                    )
                if args.multires_noise_iterations > 0:
                    noise = pyramid_noise_like(
                        noise, args.multires_noise_iterations, args.multires_noise_discount
                    )

                bsz = model_input.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(
                    0, noise_scheduler.config.num_train_timesteps, (bsz,), device=model_input.device
                )
                timesteps = timesteps.long()

                # Add noise to the model input according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_model_input = noise_scheduler.add_noise(model_input, noise, timesteps)

                # time ids
                def compute_time_ids(original_size, crops_coords_top_left):
                    # 在不裁切的情况下，target_size = original_size
                    target_size = original_size
                    add_time_ids = list(original_size + crops_coords_top_left + target_size)
                    add_time_ids = torch.tensor([add_time_ids])
                    add_time_ids = add_time_ids.to(accelerator.device, dtype=weight_dtype)
                    return add_time_ids

                add_time_ids = torch.cat(
                    [compute_time_ids(s, c) for s, c in zip(batch["original_sizes"], batch["crop_top_lefts"])]
                )

                # Predict the noise residual
                unet_added_conditions = {"time_ids": add_time_ids}
                prompt_embeds, pooled_prompt_embeds = encode_prompt(
                    text_encoders=[text_encoder_one, text_encoder_two],
                    tokenizers=None,
                    prompt=None,
                    text_input_ids_list=[batch["input_ids_one"], batch["input_ids_two"]],
                )
                unet_added_conditions.update({"text_embeds": pooled_prompt_embeds})
                model_pred = unet(
                    noisy_model_input,
                    timesteps,
                    prompt_embeds,
                    added_cond_kwargs=unet_added_conditions,
                    return_dict=False,
                )[0]

                # Get the target for loss depending on the prediction type
                if args.prediction_type is not None:
                    # set prediction_type of scheduler if defined
                    noise_scheduler.register_to_config(prediction_type=args.prediction_type)

                if noise_scheduler.config.prediction_type == "epsilon":
                    target = noise
                elif noise_scheduler.config.prediction_type == "v_prediction":
                    target = noise_scheduler.get_velocity(model_input, noise, timesteps)
                else:
                    raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

                if args.snr_gamma is None:
                    loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
                else:
                    # Compute loss-weights as per Section 3.4 of https://huggingface.co/papers/2303.09556.
                    # Since we predict the noise instead of x_0, the original formulation is slightly changed.
                    # This is discussed in Section 4.2 of the same paper.
                    snr = compute_snr(noise_scheduler, timesteps)
                    mse_loss_weights = torch.stack([snr, args.snr_gamma * torch.ones_like(timesteps)], dim=1).min(
                        dim=1
                    )[0]
                    if noise_scheduler.config.prediction_type == "epsilon":
                        mse_loss_weights = mse_loss_weights / snr
                    elif noise_scheduler.config.prediction_type == "v_prediction":
                        mse_loss_weights = mse_loss_weights / (snr + 1)

                    loss = F.mse_loss(model_pred.float(), target.float(), reduction="none")
                    loss = loss.mean(dim=list(range(1, len(loss.shape)))) * mse_loss_weights
                    loss = loss.mean()
                if args.debug_loss and "filenames" in batch:
                    for fname in batch["filenames"]:
                        accelerator.log({"loss_for_" + fname: loss}, step=global_step)
                # Gather the losses across all processes for logging (if we use distributed training).
                avg_loss = accelerator.gather(loss.repeat(args.train_batch_size)).mean()
                train_loss += avg_loss.item() / args.gradient_accumulation_steps

                # Backpropagate
                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    params_for_clipping = []
                    for group in params_to_optimize:
                        params_for_clipping.extend(group["params"])
                    accelerator.clip_grad_norm_(params_for_clipping, args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                accelerator.log({"train_loss": train_loss}, step=global_step)
                train_loss = 0.0

                # DeepSpeed requires saving weights on every device; saving weights only on the main process would cause issues.
                if accelerator.distributed_type == DistributedType.DEEPSPEED or accelerator.is_main_process:
                    if global_step % args.checkpointing_steps == 0:
                        # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
                        if args.checkpoints_total_limit is not None:
                            checkpoints = os.listdir(args.output_dir)
                            checkpoints = [d for d in checkpoints if d.startswith("checkpoint")]
                            checkpoints = sorted(checkpoints, key=lambda x: int(x.split("-")[1]))

                            # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                            if len(checkpoints) >= args.checkpoints_total_limit:
                                num_to_remove = len(checkpoints) - args.checkpoints_total_limit + 1
                                removing_checkpoints = checkpoints[0:num_to_remove]

                                logger.info(
                                    f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                                )
                                logger.info(f"removing checkpoints: {', '.join(removing_checkpoints)}")

                                for removing_checkpoint in removing_checkpoints:
                                    removing_checkpoint = os.path.join(args.output_dir, removing_checkpoint)
                                    shutil.rmtree(removing_checkpoint)

                        save_path = os.path.join(args.output_dir, f"checkpoint-{global_step}")
                        accelerator.save_state(save_path)
                        logger.info(f"Saved state to {save_path}")

            logs = {"step_loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)

            if global_step >= args.max_train_steps:
                break

        if accelerator.is_main_process:
            if args.validation_prompt is not None and epoch % args.validation_epochs == 0:
                ensure_vae_on_accelerator_for_inference()
                if args.mixed_precision == "fp16":
                    vae.to(weight_dtype)
                unet_u = unwrap_model(unet)
                te1_u = unwrap_model(text_encoder_one)
                te2_u = unwrap_model(text_encoder_two)
                unet_u.eval()
                if args.train_text_encoder:
                    te1_u.eval()
                    te2_u.eval()
                if is_single_file:
                    pipeline = StableDiffusionXLPipeline(
                        vae=vae,
                        text_encoder=te1_u,
                        text_encoder_2=te2_u,
                        tokenizer=tokenizer_one,
                        tokenizer_2=tokenizer_two,
                        unet=unet_u,
                        scheduler=inference_scheduler,
                    )
                else:
                    pipeline = StableDiffusionXLPipeline.from_pretrained(
                        args.pretrained_model_name_or_path,
                        vae=vae,
                        text_encoder=te1_u,
                        text_encoder_2=te2_u,
                        unet=unet_u,
                        revision=args.revision,
                        variant=args.variant,
                        torch_dtype=weight_dtype,
                    )

                images = log_validation(pipeline, args, accelerator, epoch)

                del pipeline
                offload_vae_to_cpu_if_precomputed()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

    # Save the lora layers
    accelerator.wait_for_everyone()
    if accelerator.is_main_process:
        unet_u = unwrap_model(unet)
        te1_u = unwrap_model(text_encoder_one)
        te2_u = unwrap_model(text_encoder_two)

        # Final validation on in-memory PEFT weights. Reloading from disk via load_lora_weights() can warn or
        # mis-load with DoRA + extended unet_target_modules (conv, etc.) due to diffusers/PEFT key inference.
        if args.validation_prompt and args.num_validation_images > 0:
            ensure_vae_on_accelerator_for_inference()
            if args.mixed_precision == "fp16":
                vae.to(weight_dtype)
            unet_u.eval()
            if args.train_text_encoder:
                te1_u.eval()
                te2_u.eval()
            if is_single_file:
                pipeline = StableDiffusionXLPipeline(
                    vae=vae,
                    text_encoder=te1_u,
                    text_encoder_2=te2_u,
                    tokenizer=tokenizer_one,
                    tokenizer_2=tokenizer_two,
                    unet=unet_u,
                    scheduler=inference_scheduler,
                )
            else:
                pipeline = StableDiffusionXLPipeline.from_pretrained(
                    args.pretrained_model_name_or_path,
                    vae=vae,
                    text_encoder=te1_u,
                    text_encoder_2=te2_u,
                    unet=unet_u,
                    revision=args.revision,
                    variant=args.variant,
                    torch_dtype=weight_dtype,
                )
            images = log_validation(pipeline, args, accelerator, epoch, is_final_validation=True)
            del pipeline
            offload_vae_to_cpu_if_precomputed()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        unet_peft_state_dict = get_peft_model_state_dict(unet_u)
        unet_lora_layers_to_save = convert_state_dict_to_diffusers(unet_peft_state_dict)

        if args.train_text_encoder:
            te1_peft_state_dict = get_peft_model_state_dict(te1_u)
            te2_peft_state_dict = get_peft_model_state_dict(te2_u)
            text_encoder_lora_layers = convert_state_dict_to_diffusers(te1_peft_state_dict)
            text_encoder_2_lora_layers = convert_state_dict_to_diffusers(te2_peft_state_dict)
        else:
            text_encoder_lora_layers = None
            text_encoder_2_lora_layers = None
            te1_peft_state_dict = None
            te2_peft_state_dict = None

        StableDiffusionXLPipeline.save_lora_weights(
            save_directory=args.output_dir,
            unet_lora_layers=unet_lora_layers_to_save,
            text_encoder_lora_layers=text_encoder_lora_layers,
            text_encoder_2_lora_layers=text_encoder_2_lora_layers,
        )

        # Also save in Kohya format for ComfyUI
        save_kohya_sdxl_lora(args.output_dir, unet_peft_state_dict, te1_peft_state_dict, te2_peft_state_dict)

        del unet_u, te1_u, te2_u
        del unet, text_encoder_one, text_encoder_two
        del text_encoder_lora_layers, text_encoder_2_lora_layers
        torch.cuda.empty_cache()

        if args.push_to_hub:
            save_model_card(
                repo_id,
                images=images,
                base_model=args.pretrained_model_name_or_path,
                dataset_name=args.dataset_name,
                train_text_encoder=args.train_text_encoder,
                repo_folder=args.output_dir,
                vae_path=args.pretrained_vae_model_name_or_path,
            )
            upload_folder(
                repo_id=repo_id,
                folder_path=args.output_dir,
                commit_message="End of training",
                ignore_patterns=["step_*", "epoch_*"],
            )

    accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
