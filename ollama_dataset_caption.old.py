#!/usr/bin/env python3
"""
连接本地 Ollama，对数据集目录内所有图片调用多模态模型生成结构化艺术解析描述。
不向 API 传 format/约束解码，仅靠提示词约定 JSON；默认开启 think，终端流式打印「思考」与正文；jsonl 仅保存解析后的 caption，不写入思考文本。

依赖: 标准库；可选 Pillow（大图缩放、PNG/WebP 等带 alpha 时在黑色背景上压平后编码；未安装则原样发送）

示例:
  python ollama_dataset_caption.py datasets/foo --model gemma4:26b
  python ollama_dataset_caption.py datasets/foo -r --host http://127.0.0.1:11434

同名 .txt 参考: 若图片旁存在与 stem 相同的 .txt（如 foo.png 与 foo.txt），默认将其内容作为「标签参考」注入提示词；用 --no-tag-ref 关闭。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

VISION_PROMPT = f"""你的任务是使用结构化的自然语言尽可能完整的编码画面内容, 要求可以根据你的描述重建完整图像
需要详细描述画面中的所有的可见信息. 
输出格式参考:
```json
{{
    "整体": "",
    "画风": "",
    "角色": [""],
    "物品": [""],
    "环境": "",
    "主观感受": "",
    "补充":""
}}
```

不要中英混用.
文字信息记录到对应的对象或补充中.
如果tag中包含人物名称, 应该在角色中提及.
不应在画面描述中出现 "根据标签" 等字样, 你的任务是编码画面内容而不是回答用户.
水印被alpha遮蔽了, 可能表现为黑块, 忽略即可, 不应在画面描述中提及.
物品中只不应出现附属于角色本身的物品, 附属于角色本身的物品应该在角色中提及.
构图/布局等信息记录到整体中.
"""


def _escape_for_quoted_tags(tags: str) -> str:
    return tags.replace("\\", "\\\\").replace('"', '\\"')


def read_sidecar_tag_reference(image_path: Path) -> str | None:
    """同目录下 `{stem}.txt` 存在且非空则返回全文（去首尾空白），否则 None。"""
    txt_path = image_path.with_suffix(".txt")
    if not txt_path.is_file():
        return None
    text = txt_path.read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def build_vision_prompt(tag_reference: str | None) -> str:
    """与 Untitled-1 一致：先任务与 JSON 模板，空行后为「该图已有的标签参考」+矛盾说明。"""
    if tag_reference is None:
        return VISION_PROMPT
    esc = _escape_for_quoted_tags(tag_reference)
    tail = (
        f'该图已有的标签参考: "{esc}"\n'
        "若标签与画面明显矛盾, 以画面为准。\n"
    )
    return VISION_PROMPT.rstrip() + "\n\n" + tail


def list_images(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        found: set[Path] = set()
        for ext in IMAGE_EXTS:
            found.update(root.rglob(f"*{ext}"))
            found.update(root.rglob(f"*{ext.upper()}"))
        return sorted(p.resolve() for p in found if p.is_file())
    return sorted(
        p.resolve()
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def _flatten_alpha_on_black(im: Any) -> Any:
    """将 RGBA/LA/带透明的 P 等合成到纯黑 RGB 背景；其余转为 RGB。"""
    from PIL import Image  # type: ignore

    if im.mode in ("RGBA", "RGBa", "LA"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (0, 0, 0))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if im.mode == "P":
        if im.info.get("transparency") is not None or "transparency" in im.info:
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (0, 0, 0))
            bg.paste(rgba, mask=rgba.split()[3])
            return bg
        return im.convert("RGB")
    return im.convert("RGB")


def image_to_b64(path: Path, max_edge: int | None) -> str:
    data = path.read_bytes()
    suf = path.suffix.lower()
    resize = max_edge is not None and max_edge > 0
    maybe_alpha = suf in (".png", ".webp", ".gif", ".bmp")
    if not resize and not maybe_alpha:
        return base64.b64encode(data).decode("ascii")
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return base64.b64encode(data).decode("ascii")

    im = Image.open(io.BytesIO(data))
    im = _flatten_alpha_on_black(im)
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


def parse_model_json(text: str) -> dict[str, Any]:
    """解析模型输出：整段为 md 围栏、或前后有说明文字 + 围栏、或裸 JSON；必要时从大括号截取。"""
    s = text.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip() in ("```", ""):
            lines.pop()
        s = "\n".join(lines).strip()
    else:
        m = re.search(r"```(?:json|JSON)?\s*\r?\n?(.*?)```", s, re.DOTALL)
        if m:
            s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i = s.find("{")
        j = s.rfind("}")
        if i != -1 and j > i:
            return json.loads(s[i : j + 1])
        raise


def chat_ollama_vision_stream(
    host: str,
    model: str,
    image_b64: str,
    prompt: str,
    *,
    num_ctx: int | None,
    timeout_sec: int,
    think: bool | str | None,
) -> str:
    """返回 message.content 全文；思考过程仅流式打印到终端，不返回、不写入磁盘。think 为 None 时不传 think 字段。
    不传入 temperature 等采样参数，沿用 Ollama/模型默认。"""
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
    if num_ctx is not None:
        body["options"] = {"num_ctx": num_ctx}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    parts: list[str] = []
    in_thinking = False
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        for raw in resp:
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
            if c_chunk:
                if in_thinking:
                    sys.stdout.write("\n── 结构化输出（JSON） ──\n")
                    in_thinking = False
                parts.append(c_chunk)
                sys.stdout.write(c_chunk)
                sys.stdout.flush()
            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))
            if obj.get("done"):
                break
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(parts)


def load_done_paths(jsonl_path: Path) -> set[str]:
    if not jsonl_path.is_file():
        return set()
    done: set[str] = set()
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        p = rec.get("path")
        if isinstance(p, str):
            done.add(p)
    return done


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Ollama 数据集图片结构化描述（提示词约定 JSON，无 format 约束）")
    ap.add_argument("dataset_dir", type=Path, help="数据集根目录")
    ap.add_argument("--model", default="gemma4:26b", help="Ollama 模型名（须支持 vision）")
    ap.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama 服务地址")
    ap.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="输出 jsonl（默认:数据集目录下 ollama_captions.jsonl）",
    )
    ap.add_argument("-r", "--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--max-edge", type=int, default=0, help="最长边超过则缩放再编码（0 关闭）")
    ap.add_argument("--num-ctx", type=int, default=None, help="可选：仅设置 context 长度 num_ctx（不改采样默认值）")
    ap.add_argument("--timeout", type=int, default=900, help="单张请求超时秒数")
    ap.add_argument("--resume", action="store_true", help="跳过 out jsonl 里已有 path 的图片")
    ap.add_argument(
        "--continue-on-error",
        action="store_true",
        help="单张失败时打印错误并继续下一张（默认：首张致命错误则退出）",
    )
    ap.add_argument(
        "--no-think",
        action="store_true",
        help="关闭推理轨迹（请求体 think=false；不支持思考的模型可忽略）",
    )
    ap.add_argument(
        "--think-level",
        choices=("low", "medium", "high"),
        default=None,
        help="GPT-OSS 等：思考深度；指定时覆盖默认的 think=true",
    )
    ap.add_argument(
        "--no-tag-ref",
        action="store_true",
        help="不读取同目录同名 .txt，不在提示词中注入标签参考",
    )
    args = ap.parse_args(argv)

    root = args.dataset_dir.expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        sys.exit(1)

    out_path = args.out
    if out_path is None:
        out_path = root / "ollama_captions.jsonl"
    else:
        out_path = out_path.expanduser().resolve()

    files = list_images(root, args.recursive)
    if not files:
        print("未找到图片", file=sys.stderr)
        sys.exit(1)

    done = load_done_paths(out_path) if args.resume else set()
    max_edge = args.max_edge if args.max_edge > 0 else None

    if args.think_level:
        think_param: bool | str | None = args.think_level
    elif args.no_think:
        think_param = False
    else:
        think_param = True

    print(f"模型: {args.model}  |  图片数: {len(files)}  |  输出: {out_path}", flush=True)
    print(f"思考 think: {think_param}", flush=True)
    print("---", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    me = max_edge

    for i, path in enumerate(files, start=1):
        key = str(path)
        if key in done:
            print(f"[{i}/{len(files)}] 跳过(已存在): {path.name}", flush=True)
            continue
        rel = path.relative_to(root) if path.is_relative_to(root) else path.name
        print(f"\n[{i}/{len(files)}] {rel}", flush=True)
        tag_ref = None if args.no_tag_ref else read_sidecar_tag_reference(path)
        if tag_ref:
            print(f"  (已注入标签参考: {path.with_suffix('.txt').name})", flush=True)
        prompt = build_vision_prompt(tag_ref)
        b64 = image_to_b64(path, me)
        try:
            raw = chat_ollama_vision_stream(
                args.host,
                args.model,
                b64,
                prompt,
                num_ctx=args.num_ctx,
                timeout_sec=args.timeout,
                think=think_param,
            )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"HTTP {e.code}: {err_body}", file=sys.stderr)
            if args.continue_on_error:
                continue
            sys.exit(e.code)
        except urllib.error.URLError as e:
            print(f"连接失败: {e.reason}", file=sys.stderr)
            sys.exit(1)
        except RuntimeError as e:
            print(f"Ollama: {e}", file=sys.stderr)
            if args.continue_on_error:
                continue
            sys.exit(1)

        try:
            parsed = parse_model_json(raw)
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败: {e}\n原始输出前 500 字:\n{raw[:500]!r}", file=sys.stderr)
            if args.continue_on_error:
                continue
            sys.exit(1)
        record: dict[str, Any] = {
            "path": key,
            "rel_path": str(rel).replace("\\", "/"),
            "model": args.model,
            "caption": parsed,
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"  → 已写入 jsonl", flush=True)


if __name__ == "__main__":
    main()
