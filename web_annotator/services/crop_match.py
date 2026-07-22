"""
裁剪网格配置 —— 仅供前端拉取同步；吸附匹配逻辑在 crop.js。
"""

from __future__ import annotations

SIDE_MIN = 512
SIDE_MAX = 1536 * 2
SIDE_STEP = 128
ZOOM_BUCKETS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
MAX_OUTPUT_PIXELS = 1536 * 1536


def match_config() -> dict:
    return {
        "side_min": SIDE_MIN,
        "side_max": SIDE_MAX,
        "side_step": SIDE_STEP,
        "zooms": list(ZOOM_BUCKETS),
        "max_output_pixels": MAX_OUTPUT_PIXELS,
    }


def clamp_target_size(tw: int, th: int) -> tuple[int, int]:
    """保存时限制输出尺寸，不重做吸附。"""
    tw = max(1, min(int(tw), SIDE_MAX))
    th = max(1, min(int(th), SIDE_MAX))
    if tw * th <= MAX_OUTPUT_PIXELS:
        return tw, th
    scale = (MAX_OUTPUT_PIXELS / (tw * th)) ** 0.5
    tw = max(1, int(tw * scale))
    th = max(1, int(th * scale))
    return tw, th
