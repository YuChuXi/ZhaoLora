#!/usr/bin/env python3
"""
使用 ARNIQA (Adaptive Region-based Normalized Image Quality Assessment) 对数据集图片进行质量评分，
并导出 HTML 可视化报告。

ARNIQA 论文: "ARNIQA: Learning Distortion Manifold for Image Quality Assessment" (WACV 2024 Oral)
模型通过 torch.hub 自动下载，无需手动安装额外包。

用法:
    python aesthetic_score_dataset.py --dataset <数据集目录> [--output <输出目录>] [--device cuda|cpu]
    python aesthetic_score_dataset.py -d datasets/univerain --device cpu --json

依赖:
    pip install torch torchvision pillow numpy tqdm
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import torch
from PIL import Image, UnidentifiedImageError
from torchvision import transforms
from tqdm import tqdm

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})

COLOURS = [
    "#c0392b", "#e74c3c", "#e67e22", "#f39c12", "#d4ac0d",
    "#27ae60", "#1e8449", "#148f77", "#0e6655", "#0a4f42",
]



# ---------------------------------------------------------------------------
# ARNIQA 评分参数
# ---------------------------------------------------------------------------
# 可选的回归器数据集: "kadid10k" (默认), "live", "csiq", "tid2013", "koniq10k", "clive"
ARNIQA_REGRESSOR = "kadid10k"

# ---------------------------------------------------------------------------
# 模型加载
# ---------------------------------------------------------------------------

def load_models(device: str):
    """加载 ARNIQA 模型（通过 torch.hub 自动下载权重）。

    ARNIQA 使用 CLIP ViT-B/16 编码器 + 质量感知适配器，
    输出范围 [0, 1]，越高表示质量越好。
    """
    print(f"[info] device: {device}")
    print(f"[info] Loading ARNIQA (regressor={ARNIQA_REGRESSOR}) via torch.hub ...")

    model = torch.hub.load(
        repo_or_dir="miccunifi/ARNIQA",
        source="github",
        model="ARNIQA",
        regressor_dataset=ARNIQA_REGRESSOR,
    )
    model.eval().to(device)

    # ARNIQA 预处理: ImageNet 标准化
    preprocess = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    print("[info] ARNIQA model ready.")
    return model, preprocess


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------

def raw_to_pony(raw: float) -> int:
    """将原始分数 [0,1] 映射到 score_0 … score_9。"""
    return int(max(0.0, min(0.99, raw)) * 10)


# 目标像素数 ≈ 1536 × 1536
_TARGET_PIXELS = 1536 * 1536


def _resize_to_target(img: Image.Image) -> Image.Image:
    """将图像缩放至像素数约等于 _TARGET_PIXELS。"""
    w, h = img.size
    current_pixels = w * h
    if current_pixels <= _TARGET_PIXELS:
        return img
    scale = (_TARGET_PIXELS / current_pixels) ** 0.5
    new_w, new_h = int(w * scale), int(h * scale)
    return img.resize((new_w, new_h), Image.LANCZOS)


@torch.no_grad()
def get_score(image: Image.Image, model, preprocess, device: str) -> float:
    """使用 ARNIQA 返回图像质量分数 (0~1)，越高表示质量越好。

    图片会先被压缩到像素数约 1536×1536，再送入模型评分。
    ARNIQA 需要全尺寸和半尺寸两个输入。
    """
    img = image.convert("RGB")
    img = _resize_to_target(img)
    w, h = img.size

    # 半尺寸图像
    img_ds = transforms.Resize((h // 2, w // 2))(img)

    # 预处理
    img_tensor = preprocess(img).unsqueeze(0).to(device)
    img_ds_tensor = preprocess(img_ds).unsqueeze(0).to(device)

    with torch.cuda.amp.autocast(enabled=(device == "cuda")):
        score = model(img_tensor, img_ds_tensor, return_embedding=False, scale_score=True)

    return score.item()


# ---------------------------------------------------------------------------
# 图片收集
# ---------------------------------------------------------------------------

def collect_images(dataset_dir: str) -> List[Path]:
    """递归收集目录下所有图片文件路径。"""
    root = Path(dataset_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {dataset_dir}")

    images = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            images.append(p)
    print(f"[info] Found {len(images)} images in {dataset_dir}")
    return images


# ---------------------------------------------------------------------------
# HTML 可视化生成
# ---------------------------------------------------------------------------

def generate_html(
    results: List[Dict],
    dataset_name: str,
    output_path: str,
    dataset_dir: str,
):
    """生成完整的美学评分 HTML 报告。"""
    if not results:
        print("[warn] No results to visualize.")
        return

    scores = [r["raw"] for r in results]
    pony_scores = [r["pony"] for r in results]

    # --- 统计 ---
    mean_raw = np.mean(scores)
    median_raw = np.median(scores)
    std_raw = np.std(scores)
    min_raw = np.min(scores)
    max_raw = np.max(scores)

    # 各 pony 档位计数
    pony_counts = [0] * 10
    for ps in pony_scores:
        pony_counts[ps] += 1
    max_count = max(pony_counts) if pony_counts else 1

    # 按 raw 分数排序 (降序)
    sorted_results = sorted(results, key=lambda r: r["raw"], reverse=True)

    # --- 直方图 HTML ---
    hist_bars = ""
    for i in range(10):
        count = pony_counts[i]
        bar_height = int(count / max_count * 160) if max_count > 0 else 0
        hist_bars += f"""
        <div class="hist-col">
            <div class="hist-count">{count}</div>
            <div class="hist-bar" style="height:{bar_height}px;background:{COLOURS[i]};"></div>
            <div class="hist-label" style="color:{COLOURS[i]}">score_{i}</div>
        </div>"""

    # 计算 HTML 文件到数据集目录的相对路径
    html_dir = os.path.dirname(os.path.abspath(output_path))
    try:
        img_base = os.path.relpath(os.path.abspath(dataset_dir), html_dir)
    except ValueError:
        img_base = os.path.abspath(dataset_dir)

    # --- 卡片网格 ---
    cards = ""
    for r in sorted_results:
        raw = r["raw"]
        pony = r["pony"]
        colour = COLOURS[pony]
        rel_path = r["rel_path"]
        img_url = os.path.join(img_base, rel_path)

        cards += f"""
        <div class="card" onclick="openLightbox('{img_url}', 'score_{pony}', '{raw:.4f}', '{colour}')">
            <div class="card-score" style="background:{colour};">score_{pony}</div>
            <div class="card-raw">raw: {raw:.4f}</div>
            <div class="card-img">
                <img src="{img_url}" loading="lazy" alt="" onerror="this.parentElement.innerHTML='<div class=\\'no-img\\'>?</div>'" />
            </div>
            <div class="card-path" title="{rel_path}">{rel_path}</div>
        </div>"""

    # --- 统计条 ---
    stat_items = ""
    stats = [
        ("均值", f"{mean_raw:.4f}", COLOURS[raw_to_pony(mean_raw)]),
        ("中位数", f"{median_raw:.4f}", COLOURS[raw_to_pony(median_raw)]),
        ("标准差", f"{std_raw:.4f}", "#888"),
        ("最低", f"{min_raw:.4f}", COLOURS[raw_to_pony(min_raw)]),
        ("最高", f"{max_raw:.4f}", COLOURS[raw_to_pony(max_raw)]),
        ("图片总数", str(len(results)), "#888"),
    ]
    for label, value, color in stats:
        stat_items += f"""
            <div class="stat-item">
                <div class="stat-label">{label}</div>
                <div class="stat-value" style="color:{color};">{value}</div>
            </div>"""

    # --- 拼装完整 HTML ---
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Aesthetic Score Report — {dataset_name}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
        background: #0d0d1a;
        color: #e0e0e0;
        min-height: 100vh;
    }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}

    /* Header */
    .header {{
        text-align: center;
        padding: 32px 0 24px;
        border-bottom: 1px solid rgba(255,255,255,.08);
        margin-bottom: 28px;
    }}
    .header h1 {{
        font-size: 2rem;
        font-weight: 800;
        background: linear-gradient(135deg, #c0392b, #f39c12, #27ae60);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }}
    .header .sub {{
        color: #777;
        font-size: .88rem;
        margin-top: 6px;
    }}

    /* Stats */
    .stats {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px;
        margin-bottom: 28px;
    }}
    .stat-item {{
        background: rgba(255,255,255,.04);
        border: 1px solid rgba(255,255,255,.06);
        border-radius: 12px;
        padding: 14px 16px;
        text-align: center;
    }}
    .stat-label {{
        font-size: .75rem;
        color: #888;
        text-transform: uppercase;
        letter-spacing: .04em;
        margin-bottom: 4px;
    }}
    .stat-value {{
        font-size: 1.35rem;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }}

    /* Histogram */
    .hist-section {{
        background: rgba(255,255,255,.03);
        border: 1px solid rgba(255,255,255,.06);
        border-radius: 16px;
        padding: 24px;
        margin-bottom: 28px;
    }}
    .hist-section h2 {{
        font-size: 1.05rem;
        font-weight: 600;
        color: #aaa;
        margin-bottom: 18px;
        text-align: center;
    }}
    .hist {{ display: flex; justify-content: center; align-items: flex-end; gap: 12px; height: 220px; }}
    .hist-col {{ display: flex; flex-direction: column; align-items: center; gap: 6px; flex: 0 0 60px; }}
    .hist-count {{ font-size: .82rem; font-weight: 600; color: #ddd; }}
    .hist-bar {{
        width: 36px;
        border-radius: 6px 6px 0 0;
        transition: height .4s ease;
        min-height: 4px;
    }}
    .hist-label {{ font-size: .72rem; font-weight: 600; }}

    /* Toolbar */
    .toolbar {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
        margin-bottom: 16px;
    }}
    .toolbar .info {{ font-size: .82rem; color: #777; }}
    .toolbar select, .toolbar input {{
        background: rgba(255,255,255,.06);
        border: 1px solid rgba(255,255,255,.12);
        border-radius: 8px;
        color: #ddd;
        padding: 6px 12px;
        font-size: .82rem;
        outline: none;
    }}
    .toolbar select:focus, .toolbar input:focus {{
        border-color: #f39c12;
    }}
    .toolbar input {{ width: 200px; }}

    /* Gallery */
    .gallery {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 14px;
    }}
    .card {{
        background: rgba(255,255,255,.03);
        border: 1px solid rgba(255,255,255,.06);
        border-radius: 14px;
        overflow: hidden;
        transition: transform .2s, box-shadow .2s;
        position: relative;
    }}
    .card:hover {{
        transform: translateY(-4px);
        box-shadow: 0 8px 30px rgba(0,0,0,.5);
        border-color: rgba(255,255,255,.14);
    }}
    .card-score {{
        position: absolute;
        top: 10px;
        right: 10px;
        z-index: 2;
        color: #fff;
        font-size: .78rem;
        font-weight: 700;
        padding: 4px 10px;
        border-radius: 20px;
        letter-spacing: .03em;
        box-shadow: 0 2px 10px rgba(0,0,0,.4);
    }}
    .card-raw {{
        position: absolute;
        top: 10px;
        left: 10px;
        z-index: 2;
        background: rgba(0,0,0,.6);
        color: #aaa;
        font-size: .68rem;
        font-weight: 500;
        padding: 3px 8px;
        border-radius: 10px;
        font-variant-numeric: tabular-nums;
    }}
    .card-img {{
        width: 100%;
        aspect-ratio: 1;
        overflow: hidden;
        display: flex;
        align-items: center;
        justify-content: center;
    }}
    .card-img img {{
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
        transition: transform .3s;
    }}
    .card:hover .card-img img {{ transform: scale(1.06); }}
    .no-img {{
        color: #555;
        font-size: 2rem;
        font-weight: 700;
    }}
    .card-path {{
        padding: 8px 10px;
        font-size: .68rem;
        color: #666;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        text-align: center;
    }}

    /* Footer */
    .footer {{
        text-align: center;
        padding: 32px 0 20px;
        color: #444;
        font-size: .75rem;
        border-top: 1px solid rgba(255,255,255,.05);
        margin-top: 40px;
    }}

    /* Lightbox */
    .lightbox {{
        display: none;
        position: fixed;
        z-index: 9999;
        inset: 0;
        background: rgba(0,0,0,.92);
        flex-direction: column;
        align-items: center;
        justify-content: center;
        cursor: zoom-out;
    }}
    .lightbox.active {{ display: flex; }}
    .lightbox img {{
        max-width: 92vw;
        max-height: 78vh;
        object-fit: contain;
        border-radius: 8px;
        box-shadow: 0 0 60px rgba(0,0,0,.6);
        cursor: default;
    }}
    .lightbox-info {{
        display: flex;
        align-items: center;
        gap: 14px;
        margin-top: 14px;
        color: #ccc;
        font-size: .85rem;
    }}
    .lightbox-score {{
        color: #fff;
        font-weight: 700;
        padding: 3px 12px;
        border-radius: 20px;
        font-size: .8rem;
    }}
    .lightbox-close {{
        position: absolute;
        top: 18px;
        right: 26px;
        color: #aaa;
        font-size: 2rem;
        cursor: pointer;
        line-height: 1;
        transition: color .2s;
        z-index: 10;
    }}
    .lightbox-close:hover {{ color: #fff; }}
    .lightbox-nav {{
        position: absolute;
        top: 50%;
        transform: translateY(-50%);
        color: #aaa;
        font-size: 2.4rem;
        cursor: pointer;
        padding: 10px;
        transition: color .2s;
        user-select: none;
        z-index: 10;
    }}
    .lightbox-nav:hover {{ color: #fff; }}
    .lightbox-prev {{ left: 14px; }}
    .lightbox-next {{ right: 14px; }}
    .card {{ cursor: pointer; }}

    /* Scrollbar */
    ::-webkit-scrollbar {{ width: 6px; }}
    ::-webkit-scrollbar-track {{ background: #0d0d1a; }}
    ::-webkit-scrollbar-thumb {{ background: #333; border-radius: 3px; }}
</style>
</head>
<body>
<div class="container">

    <div class="header">
        <h1>🎨 Image Quality Score Report</h1>
        <div class="sub">
            Dataset: <strong>{dataset_name}</strong> &nbsp;|&nbsp;
            Model: <code>ARNIQA</code> &nbsp;|&nbsp;
            Regressor: <code>{ARNIQA_REGRESSOR}</code> &nbsp;|&nbsp;
            WACV 2024 Oral
        </div>
    </div>

    <div class="stats">{stat_items}
    </div>

    <div class="hist-section">
        <h2>Score Distribution</h2>
        <div class="hist">{hist_bars}</div>
    </div>

    <div class="toolbar">
        <div class="info">共 <strong>{len(results)}</strong> 张图片，按分数降序排列</div>
        <div>
            <input type="text" id="searchBox" placeholder="🔍 搜索文件名..." oninput="filterCards()" />
            <select id="scoreFilter" onchange="filterCards()">
                <option value="all">全部档位</option>
                {''.join(f'<option value="{i}">score_{i} ({pony_counts[i]})</option>' for i in range(10))}
            </select>
        </div>
    </div>

    <div class="gallery" id="gallery">{cards}
    </div>

    <!-- Lightbox -->
    <div class="lightbox" id="lightbox" onclick="closeLightbox(event)">
        <span class="lightbox-close" onclick="closeLightbox()">&times;</span>
        <span class="lightbox-nav lightbox-prev" onclick="event.stopPropagation();navLightbox(-1)">&#8249;</span>
        <span class="lightbox-nav lightbox-next" onclick="event.stopPropagation();navLightbox(1)">&#8250;</span>
        <img id="lightboxImg" src="" alt="" />
        <div class="lightbox-info">
            <span class="lightbox-score" id="lightboxScore"></span>
            <span id="lightboxRaw"></span>
        </div>
    </div>

    <div class="footer">
        Generated by aesthetic_score_dataset.py · ARNIQA (miccunifi/ARNIQA)
    </div>

</div>

<script>
// --- Lightbox ---
let lightboxItems = [];
let lightboxIndex = -1;

(function() {{
    // collect all card data for prev/next navigation
    document.querySelectorAll('.gallery .card').forEach(card => {{
        var img = card.querySelector('.card-img img');
        var scoreEl = card.querySelector('.card-score');
        var rawEl = card.querySelector('.card-raw');
        if (img && scoreEl) {{
            lightboxItems.push({{
                src: img.getAttribute('src'),
                score: scoreEl.textContent,
                raw: rawEl ? rawEl.textContent.replace('raw: ', '') : '',
                colour: scoreEl.style.backgroundColor
            }});
        }}
    }});
}})();

function openLightbox(src, score, raw, colour) {{
    var lb = document.getElementById('lightbox');
    document.getElementById('lightboxImg').src = src;
    var scoreEl = document.getElementById('lightboxScore');
    scoreEl.textContent = score;
    scoreEl.style.background = colour;
    document.getElementById('lightboxRaw').textContent = 'raw: ' + raw;
    lb.classList.add('active');
    document.body.style.overflow = 'hidden';
    // find index
    lightboxIndex = lightboxItems.findIndex(function(item) {{ return item.src === src; }});
}}

function closeLightbox(e) {{
    if (e && e.target !== document.getElementById('lightbox')) return;
    var lb = document.getElementById('lightbox');
    lb.classList.remove('active');
    document.body.style.overflow = '';
    lightboxIndex = -1;
}}

function navLightbox(delta) {{
    if (lightboxItems.length === 0) return;
    lightboxIndex = (lightboxIndex + delta + lightboxItems.length) % lightboxItems.length;
    var item = lightboxItems[lightboxIndex];
    document.getElementById('lightboxImg').src = item.src;
    var scoreEl = document.getElementById('lightboxScore');
    scoreEl.textContent = item.score;
    scoreEl.style.background = item.colour;
    document.getElementById('lightboxRaw').textContent = 'raw: ' + item.raw;
}}

// Keyboard support
document.addEventListener('keydown', function(e) {{
    var lb = document.getElementById('lightbox');
    if (!lb.classList.contains('active')) return;
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft') navLightbox(-1);
    if (e.key === 'ArrowRight') navLightbox(1);
}});

function filterCards() {{
    const query = document.getElementById('searchBox').value.toLowerCase();
    const score = document.getElementById('scoreFilter').value;
    const cards = document.querySelectorAll('.gallery .card');
    cards.forEach(card => {{
        const path = (card.querySelector('.card-path')?.textContent || '').toLowerCase();
        const pony = (card.querySelector('.card-score')?.textContent || '').replace('score_', '');
        const matchSearch = !query || path.includes(query);
        const matchScore = score === 'all' || pony === score;
        card.style.display = (matchSearch && matchScore) ? '' : 'none';
    }});
}}
</script>

</body>
</html>"""

    # 写入文件
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[info] HTML report saved to: {output_path}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="使用 ARNIQA (WACV 2024 Oral) 对数据集进行图像质量评分并导出 HTML 报告"
    )
    parser.add_argument(
        "--dataset", "-d",
        required=True,
        help="数据集目录路径",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出目录 (默认为数据集目录下的 aesthetic_report/)",
    )
    parser.add_argument(
        "--device",
        default="cuda" if torch.cuda.is_available() else "cpu",
        choices=["cuda", "cpu"],
        help="运行设备 (默认: cuda 若可用, 否则 cpu)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="批处理大小 (默认: 1)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="额外保存一份 JSON 结果文件",
    )
    args = parser.parse_args()

    dataset_dir = Path(args.dataset).resolve()
    if not dataset_dir.is_dir():
        print(f"[error] Dataset directory not found: {dataset_dir}")
        sys.exit(1)

    dataset_name = dataset_dir.name
    output_dir = Path(args.output) if args.output else dataset_dir / "aesthetic_report"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. 收集图片
    image_paths = collect_images(str(dataset_dir))
    if not image_paths:
        print("[error] No images found.")
        sys.exit(1)

    # 2. 加载模型
    device = args.device
    model, preprocess = load_models(device)

    # 3. 逐张评分
    results: List[Dict] = []
    print(f"[info] Scoring {len(image_paths)} images ...")
    start_time = time.time()

    for img_path in tqdm(image_paths, desc="Scoring", unit="img"):
        try:
            img = Image.open(img_path)
            raw = get_score(img, model, preprocess, device)
            pony = raw_to_pony(raw)
            rel = str(img_path.relative_to(dataset_dir))

            entry = {
                "path": str(img_path),
                "rel_path": rel,
                "raw": round(raw, 6),
                "pony": pony,
            }

            results.append(entry)
        except (UnidentifiedImageError, OSError, Exception) as e:
            print(f"\n[warn] Skipping {img_path}: {e}")

    elapsed = time.time() - start_time
    print(f"[info] Done. Scored {len(results)} images in {elapsed:.1f}s "
          f"({elapsed / max(len(results), 1):.2f}s/img)")

    # 4. 导出
    html_path = output_dir / "aesthetic_report.html"
    generate_html(results, dataset_name, str(html_path), str(dataset_dir))

    # 可选 JSON
    if args.json:
        json_path = output_dir / "aesthetic_scores.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[info] JSON scores saved to: {json_path}")

    # 打印摘要
    scores_arr = [r["raw"] for r in results]
    print(f"\n{'='*50}")
    print(f"  Dataset: {dataset_name}")
    print(f"  Images:  {len(results)}")
    print(f"  Mean:    {np.mean(scores_arr):.4f}  (score_{raw_to_pony(np.mean(scores_arr))})")
    print(f"  Median:  {np.median(scores_arr):.4f}  (score_{raw_to_pony(np.median(scores_arr))})")
    print(f"  Std:     {np.std(scores_arr):.4f}")
    print(f"  Min:     {np.min(scores_arr):.4f}  (score_{raw_to_pony(np.min(scores_arr))})")
    print(f"  Max:     {np.max(scores_arr):.4f}  (score_{raw_to_pony(np.max(scores_arr))})")
    print(f"  Report:  {html_path}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
