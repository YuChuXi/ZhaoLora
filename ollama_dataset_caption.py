#!/usr/bin/env python3
"""
对 afd/** 图片执行两步标注:
1. 用 SmilingWolf/wd-swinv2-tagger-v3 和 fancyfeast/joytag 在 0.1 阈值下生成参考标签，写入 jsonl。
2. 用本地 Ollama gemma4:26b 结合图片和参考标签生成最终 caption，写到图片旁的同名 .txt。
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np  # type: ignore
from PIL import Image, ImageOps  # type: ignore

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
WD14_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
JOYTAG_REPO = "fancyfeast/joytag"
DEFAULT_THRESHOLD = 0.1


@dataclass(frozen=True)
class ScoredTag:
    name: str
    score: float
    source: str
    category: str = "general"


def list_images(root: Path, recursive: bool) -> list[Path]:
    paths = root.rglob("*") if recursive else root.iterdir()
    return sorted(p.resolve() for p in paths if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def flatten_alpha(im: Any, background: tuple[int, int, int]) -> Any:
    has_alpha = im.mode in ("RGBA", "RGBa", "LA") or (
        im.mode == "P" and (im.info.get("transparency") is not None or "transparency" in im.info)
    )
    if not has_alpha:
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, background)
    bg.paste(rgba, mask=rgba.split()[3])
    return bg


def image_to_b64(path: Path, max_edge: int | None) -> str:
    data = path.read_bytes()
    suf = path.suffix.lower()
    resize = max_edge is not None and max_edge > 0
    maybe_alpha = suf in (".png", ".webp", ".gif", ".bmp")
    if not resize and not maybe_alpha:
        return base64.b64encode(data).decode("ascii")

    im = flatten_alpha(Image.open(io.BytesIO(data)), (0, 0, 0))
    if resize:
        w, h = im.size
        m = max(w, h)
        if m > max_edge:
            s = max_edge / m
            nw, nh = max(1, int(w * s)), max(1, int(h * s))
            im = im.resize((nw, nh), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def find_mask_for_image(image_path: Path, mask_dir: Path) -> Path | None:
    """在 mask_dir 中寻找与 image_path 同主文件名的 mask 文件。"""
    stem = image_path.stem
    for ext in IMAGE_EXTS:
        candidate = mask_dir / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def image_to_b64_with_mask(path: Path, max_edge: int | None, mask_path: Path) -> str:
    """将图片编码为 base64 PNG，先乘上 mask（遮罩区域变黑）。mask 必须与图片同尺寸。"""
    im = open_rgb_image(path, alpha_on_white=False, mask_path=mask_path)

    if max_edge is not None and max_edge > 0:
        w, h = im.size
        m = max(w, h)
        if m > max_edge:
            s = max_edge / m
            nw, nh = max(1, int(w * s)), max(1, int(h * s))
            im = im.resize((nw, nh), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def open_rgb_image(path: Path, *, alpha_on_white: bool, mask_path: Path | None = None) -> Any:
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    im = flatten_alpha(im, (255, 255, 255) if alpha_on_white else (0, 0, 0))
    if mask_path is not None:
        mask_im = Image.open(mask_path).convert("L")
        if mask_im.size != im.size:
            raise ValueError(f"mask 尺寸 {mask_im.size} 与图片尺寸 {im.size} 不匹配: {mask_path}")
        mask_array = np.asarray(mask_im, dtype=np.float32) / 255.0
        im_array = np.asarray(im, dtype=np.float32)
        for c in range(3):
            im_array[:, :, c] *= mask_array
        im = Image.fromarray(np.clip(im_array, 0, 255).astype(np.uint8))
    return im


def normalize_tag_name(name: str) -> str:
    return name.strip().replace("_", " ")


def pad_square_white(image: Any) -> Any:
    width, height = image.size
    size = max(width, height)
    padded = Image.new("RGB", (size, size), (255, 255, 255))
    padded.paste(image, ((size - width) // 2, (size - height) // 2))
    return padded


class RepeatedOutputError(RuntimeError):
    pass


class LoopDetector:
    def __init__(self, size: int=200, min_repeats: int = 4):
        self.size = size
        self.min_repeats = min_repeats
        self.buf = ""
        self.period_len = None

    def feed(self, string: str) -> bool:
        self.buf = (self.buf + string)[-self.size:]
        self.period_len = None
        if len(self.buf) < self.size: return False
        s = self.buf
        pi = [0] * len(s)
        for i in range(1, len(s)):
            j = pi[i - 1]
            while j and s[i] != s[j]:
                j = pi[j - 1]
            pi[i] = j + (s[i] == s[j])
        p = len(s) - pi[-1]
        if p < len(s) and p * self.min_repeats <= len(s):
            self.period_len = p
            return True
        return False


class WD14Tagger:
    def __init__(self, repo_id: str = WD14_REPO) -> None:
        import huggingface_hub  # type: ignore
        import onnxruntime as ort  # type: ignore

        model_dir = Path(
            huggingface_hub.snapshot_download(
                repo_id,
                allow_patterns=["model.onnx", "selected_tags.csv"],
            )
        )
        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        input_shape = self.session.get_inputs()[0].shape
        self.image_size = int(input_shape[1]) if isinstance(input_shape[1], int) else 448
        self.tags = self._load_tags(model_dir / "selected_tags.csv")

    @staticmethod
    def _load_tags(csv_path: Path) -> list[tuple[str, str]]:
        category_names = {"0": "general", "4": "character", "9": "rating"}
        tags: list[tuple[str, str]] = []
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                category = category_names.get(row["category"], row["category"])
                tags.append((normalize_tag_name(row["name"]), category))
        return tags

    def predict(self, image_path: Path, threshold: float, mask_path: Path | None = None) -> list[ScoredTag]:
        image = pad_square_white(open_rgb_image(image_path, alpha_on_white=True, mask_path=mask_path))
        if image.size != (self.image_size, self.image_size):
            image = image.resize((self.image_size, self.image_size), Image.Resampling.BICUBIC)
        array = np.asarray(image, dtype=np.float32)[:, :, ::-1]
        batch = np.expand_dims(array, axis=0)
        scores = self.session.run(None, {self.input_name: batch})[0][0]

        result: list[ScoredTag] = []
        for (name, category), score in zip(self.tags, scores, strict=False):
            score_f = float(score)
            if category in {"general", "character"} and score_f >= threshold:
                result.append(ScoredTag(name=name, score=score_f, source="wd14", category=category))
        return result


class JoyTagger:
    def __init__(self, repo_id: str = JOYTAG_REPO) -> None:
        import huggingface_hub  # type: ignore
        import torch  # type: ignore

        model_dir = Path(huggingface_hub.snapshot_download(repo_id))
        code_dir = Path(
            huggingface_hub.snapshot_download(
                repo_id,
                repo_type="space",
                allow_patterns=["Models.py"],
            )
        )
        sys.path.insert(0, str(code_dir))
        from Models import VisionModel  # type: ignore

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = VisionModel.load_model(str(model_dir)).to(self.device)
        self.model.eval()
        self.image_size = int(self.model.image_size)
        self.tags = [
            normalize_tag_name(line)
            for line in (model_dir / "top_tags.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def prepare_image(image: Any, target_size: int) -> Any:
        import torchvision.transforms.functional as tvf  # type: ignore

        padded = pad_square_white(image)
        if padded.size != (target_size, target_size):
            padded = padded.resize((target_size, target_size), Image.Resampling.BICUBIC)
        image_tensor = tvf.pil_to_tensor(padded) / 255.0
        return tvf.normalize(
            image_tensor,
            mean=[0.48145466, 0.4578275, 0.40821073],
            std=[0.26862954, 0.26130258, 0.27577711],
        )

    def predict(self, image_path: Path, threshold: float, mask_path: Path | None = None) -> list[ScoredTag]:
        import torch  # type: ignore

        image = open_rgb_image(image_path, alpha_on_white=True, mask_path=mask_path)
        image_tensor = self.prepare_image(image, self.image_size).unsqueeze(0).to(self.device)
        with torch.no_grad():
            preds = self.model({"image": image_tensor})
            scores = preds["tags"].sigmoid().detach().cpu()[0]
        return [
            ScoredTag(name=tag, score=float(score), source="joytag")
            for tag, score in zip(self.tags, scores, strict=False)
            if float(score) >= threshold
        ]


def chat_ollama_vision_stream(
    host: str,
    model: str,
    image_b64: str,
    prompt: str,
    *,
    num_ctx: int | None,
    num_predict: int | None,
    timeout_sec: int,
    think: bool | str | None,
) -> str:
    """返回 message.content 全文；思考过程只在终端显示。think 为 None 时不传 think 字段。"""
    url = host.rstrip("/") + "/api/chat"
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }
        ],
        "stream": True,
    }
    options: dict[str, int] = {}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    if num_predict is not None and num_predict > 0:
        options["num_predict"] = num_predict
    if options:
        body["options"] = options
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    parts: list[str] = []
    loop_detector = LoopDetector()
    in_thinking = False
    start_time = time.monotonic()
    socket_timeout = timeout_sec if timeout_sec > 0 else None
    with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
        for raw in resp:
            if timeout_sec > 0 and time.monotonic() - start_time > timeout_sec:
                raise TimeoutError(f"单张生成超过 {timeout_sec} 秒")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            obj = json.loads(line)
            msg = obj.get("message") or {}
            t_chunk = msg.get("thinking") or msg.get("reasoning") or obj.get("thinking") or ""
            c_chunk = msg.get("content") or ""
            if t_chunk:
                if not in_thinking:
                    sys.stdout.write("\n── 思考 ──\n")
                    in_thinking = True
                sys.stdout.write(t_chunk)
                sys.stdout.flush()
                if loop_detector.feed(t_chunk):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise RepeatedOutputError("检测到最近 100 字内输出循环")
            if c_chunk:
                if in_thinking:
                    sys.stdout.write("\n── 最终输出 ──\n")
                    in_thinking = False
                parts.append(c_chunk)
                sys.stdout.write(c_chunk)
                sys.stdout.flush()
                if loop_detector.feed(c_chunk):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise RepeatedOutputError("检测到最近 100 字内输出循环")
            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))
            if obj.get("done"):
                break
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(parts)


def merge_tags(tags: list[ScoredTag]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in tags:
        tag = merged.setdefault(
            item.name,
            {"name": item.name, "score": item.score, "scores": {}, "categories": set()},
        )
        tag["score"] = max(float(tag["score"]), item.score)
        tag["scores"][item.source] = max(float(tag["scores"].get(item.source, 0.0)), item.score)
        tag["categories"].add(item.category)
    return sorted(
        [
            {
                "name": tag["name"],
                "score": round(float(tag["score"]), 6),
                "scores": {k: round(float(v), 6) for k, v in sorted(tag["scores"].items())},
                "categories": sorted(tag["categories"]),
            }
            for tag in merged.values()
        ],
        key=lambda x: (-float(x["score"]), str(x["name"])),
    )


def load_reference_map(jsonl_path: Path) -> dict[str, dict[str, Any]]:
    if not jsonl_path.is_file():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        path = rec.get("path")
        if isinstance(path, str):
            records[path] = rec
    return records


def write_reference_record(jsonl_path: Path, record: dict[str, Any]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_reference_record(
    root: Path,
    image_path: Path,
    wd14: WD14Tagger,
    joytag: JoyTagger,
    threshold: float,
    mask_path: Path | None = None,
) -> dict[str, Any]:
    rel = image_path.relative_to(root) if image_path.is_relative_to(root) else image_path.name
    tags = merge_tags(wd14.predict(image_path, threshold, mask_path=mask_path) + joytag.predict(image_path, threshold, mask_path=mask_path))
    return {
        "path": str(image_path),
        "rel_path": str(rel).replace("\\", "/"),
        "threshold": threshold,
        "models": {"wd14": WD14_REPO, "joytag": JOYTAG_REPO},
        "tags": tags,
    }


def compact_reference_for_prompt(
    record: dict[str, Any],
    limit: int,
    *,
    character_only: bool = False,
) -> str:
    tags = record.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    character_names = [
        tag["name"]
        for tag in tags
        if isinstance(tag, dict)
        and isinstance(tag.get("name"), str)
        and "character" in tag.get("categories", [])
    ]
    if character_only:
        if not character_names:
            return "No character tag candidates above the threshold."
        if limit > 0:
            character_names = character_names[:limit]
        return "Character tag candidates: " + ", ".join(character_names)

    names = [
        tag["name"]
        for tag in tags[: max(0, limit)]
        if isinstance(tag, dict) and isinstance(tag.get("name"), str)
    ]
    if not names:
        return "No reference tags above the threshold."
    parts = ["All tag candidates: " + ", ".join(names)]
    if character_names:
        parts.insert(0, "Character tag candidates: " + ", ".join(character_names))
    return "\n".join(parts)


def build_caption_prompt(
    reference_record: dict[str, Any],
    tag_limit: int,
    *,
    character_only: bool = False,
) -> str:
    reference = compact_reference_for_prompt(reference_record, tag_limit, character_only=character_only)
    print(f"reference: {reference}", flush=True)
    return f"""You are captioning images for a Danbooru-style dataset. Training captions must be long and exhaustive, not short summaries.

Use the reference tag list below as the main tag candidate list. It is merged from WD14/JoyTag and sorted by confidence.
Before writing JSON, silently audit the candidates against the image:
- Do not blindly copy the reference list.
- The "tags" field is the primary output. It must be a long, dense Danbooru/ComfyUI-style tag list. Brevity is wrong: under-tagging hurts training.
- Work through this checklist and add every verified item: subject count; character; rating-safe traits; hair (length, style, color, bangs, accessories); face (expression, mouth, blush, tears); eyes (color, shape, pupils, direction); body (pose, limbs, hands, legs, feet, skin); clothing (each garment, color, state open/closed, layers); accessories; held items; background type and objects; shot type; camera angle; crop; composition; borders/UI if present.
- Keep every candidate tag that is visibly present or strongly plausible from the image, including small clothing, expression, pose, accessory, body-part, background, camera angle, crop, and composition tags.
- Remove candidate tags that are contradicted by the image, mutually exclusive with a more accurate visible tag, or only come from model noise.
- Add obvious missing Danbooru tags for count, subject type, face, eyes, hair, clothing, accessories, pose, hands, legs, background, view angle, framing, and image composition.
- Prioritize non-foreground content: background type and elements, props, scenery, borders, frames, UI overlays, and anything outside the main subject silhouette.
- Prioritize character body language: full-body or partial pose, limb position, hand/arm/leg/feet gestures, head tilt, gaze direction, action verbs (sitting, running, reaching, etc.), and interaction with objects or other characters.
- Prioritize composition and framing: shot type (close-up, bust, cowboy shot, full body, etc.), camera angle (from above, from below, dutch angle), subject placement (centered, off-center), crop boundaries, depth of field, and negative space.
- Prefer expressing visual facts as tags when a Danbooru-style tag exists. Do not compress multiple visible facts into one vague tag; split them.
- Merge tags with overlapping semantics into a single precise compound tag. For example, instead of "single sock", "white socks", "white over-knee socks" as three separate tags, use "single white over-knee sock" as one tag. Combine count/quantity, color, type, and position into one compound tag rather than listing them separately.
- Tags support natural-language qualifiers such as "left", "right", "only", "single", "mismatched", and "asymmetrical" to precisely describe asymmetric clothing or body parts (e.g., "left white glove", "right bare leg", "single white over-knee sock on left leg", "mismatched legwear").
- CRITICAL — "details" MUST explicitly cover these categories for every image: background — setting type, objects, scenery, depth; action — pose, motion, what the character is doing, interaction with objects; clothing — every garment layer, color, state open/closed, fit; limbs — exact position and posture of arms, legs, hands, feet, head, gaze direction; occlusion — what overlaps what, which body parts or objects are in front of others, any partially hidden elements; etc.
- Put confirmed character tags first, then important subject tags, then the remaining verified detail tags.
- Ignore watermarks, artist signatures, copyright notices, website URLs, social media handles, and any other overlaid text or logo that is not part of the scene content. Do not tag or describe these artifacts; treat them as non-content noise.

Reference tags:
{reference}

Output valid JSON only. Do not output Markdown, explanations, bullets, or confidence scores.

Required JSON format:
{{
  "tags": "xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, xxxxx, ",
  "details": [
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx",
    "xxxxx xxxxx xxxxxxxxx xxxxxxx xxxx xxx xxxxxxx xxx xx"
  ],
  "texts": "xxxxxx \"xxxx\"",
  "caption": "xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx, xxxxx xxxxx xxxxx,
}}

JSON field rules:
"tags": long, dense, final Danbooru tags in English only—aim for high tag count, not a minimal set. Use Danbooru-style English or romanized tags only; do not output Chinese, Japanese, or filename text in this field. Include verified tags across hair, face, eyes, skin, every clothing piece and color, accessories, pose, each visible limb, hands, legs, feet, gaze, action, background, props, lighting, shot type, angle, crop, and composition; do not move taggable facts into "details". If any character tag candidate is correct and can be expressed as an English/romanized Danbooru tag, put the confirmed character tag or tags at the very beginning. Do not use player IDs or visible text as character tags. Never output a one-line tag soup under ~25 tags when the image clearly shows a character and scene.
"details": an array of complete English sentences. Each element is one self-contained sentence. Do NOT end sentences with periods, and do NOT use commas inside any sentence — each string must contain only words separated by spaces, no punctuation. The following categories MUST all be clearly covered across the sentences: (1) background — setting, scenery, depth; (2) action — pose, motion, interaction; (3) clothing — every garment layer, color, state; (4) limbs — exact arm/leg/hand/foot/head position and posture; (5) occlusion — what overlaps what, front-to-back relationships. Use an empty array [] only when almost nothing is identifiable.
"texts": readable text in the image only. Use an empty string if there is no readable text.
"caption": a single English comma-separated paragraph made only from the final "tags" and "details". Include every comma-separated item from "tags" exactly as a caption segment, and include every sentence from "details" as one short comma-separated phrase without changing its meaning. Do not add facts that are absent from "tags" and "details", do not contradict them, and do not drop any tag item or detail sentence. Your only job is to combine those tag segments and detail phrases, then sort all segments by visual importance: confirmed character identity first, then main subject count and type, pose/action, face/expression, clothing, limbs, props, background/composition, occlusion, and finally minor visual details. Do not include bullets, periods, newlines, confidence scores, watermarks, signatures, URLs, or ignored visible text.

Ignore black alpha-mask, watermark, artist signature, copyright notice, or any other overlaid non-content artifacts.
"""


def clean_caption_text(text: str) -> tuple[str, list[str], str, str]:
    """返回 (tags_str, details_list, texts_str, caption_str)。details 列表中每个句子不含逗号或句号，否则抛出 ValueError。"""
    raw = text.strip()
    if raw.startswith("```"):
        raw = "\n".join(line for line in raw.splitlines()[1:] if line.strip() != "```").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"模型输出不是 JSON: {raw[:300]!r}")
    obj = json.loads(raw[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("模型输出 JSON 顶层必须是对象")

    tags = str(obj.get("tags") or "").strip()
    texts = str(obj.get("texts") or "").strip()
    caption = str(obj.get("caption") or "").strip()
    if not caption:
        raise ValueError("caption 字段不能为空")

    raw_details = obj.get("details")
    if raw_details is None:
        details_list: list[str] = []
    elif isinstance(raw_details, list):
        details_list = [str(item).strip() for item in raw_details if str(item).strip()]
    else:
        raise ValueError(f"details 字段类型错误，期望 list，实际: {type(raw_details).__name__}")

    # 验证: 每个句子内部不得包含逗号或句号
    for i, sentence in enumerate(details_list):
        if "," in sentence or "." in sentence:
            raise ValueError(f"details[{i}] 包含逗号或句号: {sentence!r}")

    return tags, details_list, texts, caption


def format_caption_txt(tags: str, details_list: list[str], texts: str) -> str:
    """将标注结果格式化为 .txt 文件内容。details 用 ', ' 连接。"""
    details_line = ", ".join(details_list) if details_list else ""
    parts = [tags, details_line, texts]
    return "\n".join(parts).rstrip() + "\n"


def write_caption_jsonl(jsonl_path: Path, record: dict[str, Any]) -> None:
    """追加一条标注记录到 jsonl 文件。"""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def generate_reference_jsonl(
    root: Path,
    files: list[Path],
    jsonl_path: Path,
    threshold: float,
    overwrite: bool,
    continue_on_error: bool,
    mask_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    if overwrite and jsonl_path.exists():
        jsonl_path.unlink()
    records = load_reference_map(jsonl_path)
    print(f"加载 WD14: {WD14_REPO}", flush=True)
    wd14 = WD14Tagger()
    print(f"加载 JoyTag: {JOYTAG_REPO}", flush=True)
    joytag = JoyTagger()

    for index, image_path in enumerate(files, start=1):
        key = str(image_path)
        rel = image_path.relative_to(root) if image_path.is_relative_to(root) else image_path.name
        if key in records:
            print(f"[tags {index}/{len(files)}] 跳过: {rel}", flush=True)
            continue
        print(f"[tags {index}/{len(files)}] {rel}", flush=True)
        mask_path: Path | None = None
        if mask_dir:
            mask_path = find_mask_for_image(image_path, mask_dir)
            if mask_path is None:
                raise FileNotFoundError(f"未找到 mask: {image_path.stem}.* 在 {mask_dir}")
        try:
            record = build_reference_record(root, image_path, wd14, joytag, threshold, mask_path=mask_path)
        except Exception as exc:
            print(f"参考标签失败: {rel}: {exc}", file=sys.stderr)
            if continue_on_error:
                continue
            raise
        write_reference_record(jsonl_path, record)
        records[key] = record
        print(f"  → {len(record['tags'])} tags", flush=True)
    return records


def generate_caption_txt(
    root: Path,
    files: list[Path],
    references: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    max_edge = args.max_edge if args.max_edge > 0 else None
    if args.think_level:
        think_param: bool | str | None = args.think_level
    elif args.no_think:
        think_param = False
    else:
        think_param = True

    mask_dir = args.mask_dir.expanduser().resolve() if args.mask_dir else None
    if mask_dir and not mask_dir.is_dir():
        print(f"mask 目录不存在: {mask_dir}", file=sys.stderr)
        sys.exit(1)

    jsonl_path = root / "captions.jsonl"
    max_retries = 3
    total = len(files)
    retry_count: dict[str, int] = {}
    queue: deque[Path] = deque(files)
    processed = 0
    skipped = 0

    if args.overwrite_txt and jsonl_path.exists():
        jsonl_path.unlink()

    print(f"模型: {args.model}  |  思考 think: {think_param}  |  最大重试: {max_retries}", flush=True)

    while queue:
        image_path = queue.popleft()
        key = str(image_path)
        rel = image_path.relative_to(root) if image_path.is_relative_to(root) else image_path.name
        txt_path = image_path.with_suffix(".txt")

        if txt_path.exists() and not args.overwrite_txt:
            print(f"[caption {processed + skipped + 1}/{total}] 跳过(已存在): {rel}", flush=True)
            skipped += 1
            continue

        reference = references.get(key)
        if reference is None:
            message = f"缺少参考标签: {rel}"
            if args.continue_on_error:
                print(message, file=sys.stderr)
                skipped += 1
                continue
            raise RuntimeError(message)

        retries = retry_count.get(key, 0)
        suffix = f" (重试 {retries}/{max_retries})" if retries > 0 else ""
        print(f"\n[caption {processed + skipped + 1}/{total}] {rel}{suffix}", flush=True)
        prompt = build_caption_prompt(
            reference,
            args.prompt_tag_limit,
            character_only=args.character_tags_only,
        )
        if mask_dir:
            mask_path = find_mask_for_image(image_path, mask_dir)
            if mask_path is None:
                raise FileNotFoundError(f"未找到 mask: {image_path.stem}.* 在 {mask_dir}")
            b64 = image_to_b64_with_mask(image_path, max_edge, mask_path)
        else:
            b64 = image_to_b64(image_path, max_edge)
        try:
            raw = chat_ollama_vision_stream(
                args.host,
                args.model,
                b64,
                prompt,
                num_ctx=args.num_ctx,
                num_predict=args.num_predict,
                timeout_sec=args.timeout,
                think=think_param,
            )
            tags, details_list, texts, caption = clean_caption_text(raw)
        except Exception as exc:
            print(f"处理失败: {rel}: {exc}", file=sys.stderr)
            if retries < max_retries:
                retry_count[key] = retries + 1
                queue.append(image_path)
                print(f"  → 重新放入队列末尾 (第 {retries + 1} 次重试)", flush=True)
                continue
            else:
                print(f"  → 已达最大重试次数 ({max_retries})，跳过", flush=True)
                if args.continue_on_error:
                    skipped += 1
                    continue
                raise

        caption_text = format_caption_txt(tags, details_list, texts)
        txt_path.write_text(caption_text, encoding="utf-8")

        # 保存 JSONL 记录
        jsonl_record: dict[str, Any] = {
            "path": key,
            "rel_path": str(rel).replace("\\", "/"),
            "tags": tags,
            "details": details_list,
            "texts": texts,
            "caption": caption,
        }
        write_caption_jsonl(jsonl_path, jsonl_record)

        print(f"  → 已写入 {txt_path.name}", flush=True)
        processed += 1

    print(f"\n完成: 处理 {processed} 张, 跳过 {skipped} 张, 总计 {total} 张", flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="用 WD14/JoyTag + Ollama 为 afd 图片生成同名 txt 标注")
    ap.add_argument("dataset_dir", nargs="?", type=Path, default=Path("afd"), help="数据集根目录，默认 afd")
    ap.add_argument("--model", default="gemma4:26b", help="Ollama 视觉模型名")
    ap.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama 服务地址")
    ap.add_argument("--tag-jsonl", type=Path, default=None, help="参考标签 jsonl，默认数据集目录 wd14_joytag_tags.jsonl")
    ap.add_argument("--steps", choices=("all", "tags", "captions"), default="all", help="执行步骤")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="WD14/JoyTag 标签阈值")
    ap.add_argument("--recursive", dest="recursive", action="store_true", default=True, help="递归扫描，默认开启")
    ap.add_argument("--no-recursive", dest="recursive", action="store_false", help="只扫描根目录")
    ap.add_argument("--max-edge", type=int, default=0, help="Ollama 输入最长边缩放，0 为不缩放")
    ap.add_argument("--num-ctx", type=int, default=None, help="Ollama num_ctx")
    ap.add_argument("--num-predict", type=int, default=8192, help="Ollama 最大生成 token 数，0 为不限制")
    ap.add_argument("--timeout", type=int, default=60, help="单张 Ollama 请求超时秒数")
    ap.add_argument("--prompt-tag-limit", type=int, default=0, help="传给 Ollama 的参考标签数量上限")
    ap.add_argument(
        "--character-tags-only",
        action="store_true",
        help="只把 WD14 角色标签传给 Ollama，不提供通用标签候选",
    )
    ap.add_argument("--overwrite-tags", action="store_true", help="重新生成参考标签 jsonl")
    ap.add_argument("--overwrite-txt", action="store_true", help="覆盖图片旁已存在的同名 txt")
    ap.add_argument("--continue-on-error", action="store_true", help="单张失败时继续下一张")
    ap.add_argument("--no-think", action="store_true", help="请求体 think=false")
    ap.add_argument("--think-level", choices=("low", "medium", "high"), default=None, help="指定思考深度")
    ap.add_argument("--mask-dir", type=Path, default=None, help="mask 文件夹，图片发送 Ollama 前乘上同名 mask（遮罩区域变黑）")
    args = ap.parse_args(argv)

    root = args.dataset_dir.expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        sys.exit(1)

    tag_jsonl = args.tag_jsonl
    if tag_jsonl is None:
        tag_jsonl = root / "wd14_joytag_tags.jsonl"
    else:
        tag_jsonl = tag_jsonl.expanduser().resolve()

    files = list_images(root, args.recursive)
    if not files:
        print("未找到图片", file=sys.stderr)
        sys.exit(1)

    print(f"图片数: {len(files)}  |  参考标签: {tag_jsonl}", flush=True)
    print("---", flush=True)

    references = load_reference_map(tag_jsonl)
    if args.steps in {"all", "tags"}:
        mask_dir = args.mask_dir.expanduser().resolve() if args.mask_dir else None
        if mask_dir and not mask_dir.is_dir():
            print(f"mask 目录不存在: {mask_dir}", file=sys.stderr)
            sys.exit(1)
        references = generate_reference_jsonl(
            root,
            files,
            tag_jsonl,
            args.threshold,
            args.overwrite_tags,
            args.continue_on_error,
            mask_dir=mask_dir,
        )
    if args.steps in {"all", "captions"}:
        references = load_reference_map(tag_jsonl)
        generate_caption_txt(root, files, references, args)


if __name__ == "__main__":
    main()
