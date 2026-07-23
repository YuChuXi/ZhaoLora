#!/usr/bin/env python3
"""
扫描目录，找出哪些图片截取自同目录内其它图片（支持缩放 / 轻微形变）。

方法：ORB 特征匹配 + RANSAC 单应矩阵；若截取图四角映射到原图内且内点足够，则判定为截取。

默认只报告。指定 --trash-dir 移入回收目录，或 --delete 直接删除截取图（保留原图）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass
class ImgInfo:
    path: Path
    width: int
    height: int
    gray: np.ndarray
    kp: tuple
    des: np.ndarray | None


def _collect_paths(root: Path, recursive: bool) -> list[Path]:
    root = root.resolve()
    out: list[Path] = []
    if recursive:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in IMAGE_EXTS:
                    out.append(p)
    else:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append(p)
    out.sort(key=lambda x: str(x))
    return out


def _load_one(path: Path, max_side: int, n_features: int) -> ImgInfo | None:
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if w < 16 or h < 16:
            return None
        arr = np.asarray(im)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    long_side = max(w, h)
    if long_side > max_side:
        scale = max_side / long_side
        nw = max(16, int(round(w * scale)))
        nh = max(16, int(round(h * scale)))
        gray = cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)
    orb = cv2.ORB_create(nfeatures=n_features, fastThreshold=10)
    kp, des = orb.detectAndCompute(gray, None)
    return ImgInfo(path=path, width=w, height=h, gray=gray, kp=tuple(kp or ()), des=des)


def _match_pair(
    crop: ImgInfo,
    source: ImgInfo,
    min_matches: int,
    min_inlier_ratio: float,
    max_reproj: float,
) -> dict | None:
    """判断 crop 是否截取自 source。返回命中信息或 None。"""
    if crop.des is None or source.des is None:
        return None
    if len(crop.kp) < min_matches or len(source.kp) < min_matches:
        return None

    area_c = crop.width * crop.height
    area_s = source.width * source.height
    if area_c >= area_s * 0.98:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    knn = bf.knnMatch(crop.des, source.des, k=2)
    good: list[cv2.DMatch] = []
    for pair in knn:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)
    if len(good) < min_matches:
        return None

    src_pts = np.float32([crop.kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([source.kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, max_reproj)
    if H is None or mask is None:
        return None

    inliers = int(mask.ravel().sum())
    inlier_ratio = inliers / len(good)
    if inliers < min_matches or inlier_ratio < min_inlier_ratio:
        return None

    ch, cw = crop.gray.shape[:2]
    sh, sw = source.gray.shape[:2]
    corners = np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]]).reshape(-1, 1, 2)
    mapped = cv2.perspectiveTransform(corners, H).reshape(-1, 2)

    cx, cy = mapped.mean(axis=0)
    if not (0 <= cx < sw and 0 <= cy < sh):
        return None
    inside = 0
    margin = 0.05 * max(sw, sh)
    for x, y in mapped:
        if -margin <= x < sw + margin and -margin <= y < sh + margin:
            inside += 1
    if inside < 3:
        return None

    xs, ys = mapped[:, 0], mapped[:, 1]
    x1 = float(np.clip(xs.min(), 0, sw))
    y1 = float(np.clip(ys.min(), 0, sh))
    x2 = float(np.clip(xs.max(), 0, sw))
    y2 = float(np.clip(ys.max(), 0, sh))
    rw, rh = x2 - x1, y2 - y1
    if rw < 8 or rh < 8:
        return None
    if (rw * rh) / (sw * sh) > 0.98:
        return None

    sx = source.width / sw
    sy = source.height / sh
    score = inlier_ratio * min(1.0, inliers / 40.0)

    return {
        "crop": str(crop.path),
        "crop_size": [crop.width, crop.height],
        "source": str(source.path),
        "source_size": [source.width, source.height],
        "score": round(float(score), 4),
        "inliers": inliers,
        "matches": len(good),
        "inlier_ratio": round(float(inlier_ratio), 4),
        "region": [
            int(round(x1 * sx)),
            int(round(y1 * sy)),
            int(round(rw * sx)),
            int(round(rh * sy)),
        ],
    }


def _search_one(
    crop_idx: int,
    parent_indices: list[int],
    images: list[ImgInfo],
    min_matches: int,
    min_inlier_ratio: float,
    max_reproj: float,
) -> list[dict]:
    crop = images[crop_idx]
    hits: list[dict] = []
    for pi in parent_indices:
        hit = _match_pair(crop, images[pi], min_matches, min_inlier_ratio, max_reproj)
        if hit is not None:
            hits.append(hit)
    return hits


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem = p.stem
    suffix = p.suffix
    parent = p.parent
    for i in range(1, 10_000):
        cand = parent / f"{stem}__dup{i}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"无法生成不冲突的目标文件名: {p}")


def _move_keep_rel(src: Path, data_root: Path, trash_root: Path) -> Path:
    rel = src.resolve().relative_to(data_root.resolve())
    dst = (trash_root / rel).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = _unique_path(dst)
    shutil.move(str(src), str(dst))
    return dst


def _is_under_any(path: Path, roots: list[Path]) -> bool:
    rp = path.resolve()
    for base in roots:
        if rp == base or base in rp.parents:
            return True
    return False


def _rel_to(path: Path, root: Path) -> Path:
    if path.is_relative_to(root):
        return path.relative_to(root)
    return path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dir", type=Path, help="要扫描的目录")
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p.add_argument("--max-side", type=int, default=1024, help="特征提取前最长边（默认 1024）")
    p.add_argument("--features", type=int, default=2000, help="每张图 ORB 特征数（默认 2000）")
    p.add_argument("--min-matches", type=int, default=12, help="最少内点数（默认 12）")
    p.add_argument(
        "--min-inlier-ratio",
        type=float,
        default=0.35,
        help="内点 / 好匹配 比例下限（默认 0.35）",
    )
    p.add_argument("--reproj", type=float, default=5.0, help="RANSAC 重投影阈值（默认 5）")
    p.add_argument(
        "--min-area-ratio",
        type=float,
        default=0.01,
        help="截取图面积/原图面积下限（默认 0.01）",
    )
    p.add_argument(
        "--max-area-ratio",
        type=float,
        default=0.97,
        help="截取图面积/原图面积上限（默认 0.97）",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=max(1, min(8, (os.cpu_count() or 4))),
        help="并行线程数",
    )
    p.add_argument("--json-out", type=Path, default=None, help="结果写入 JSON")
    p.add_argument(
        "--best-only",
        action="store_true",
        help="每张截取图只保留得分最高的一个原图（删除/回收时默认启用）",
    )
    p.add_argument(
        "--delete",
        action="store_true",
        help="直接删除截取图（保留原图）；可与 --dry-run 联用预览",
    )
    p.add_argument(
        "--trash-dir",
        type=Path,
        default=None,
        help="将截取图移动到该回收目录（保持相对扫描根的路径）；指定后无需再加 --delete",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将删除/移动的文件，不实际执行",
    )
    p.add_argument(
        "--no-sidecar-txt",
        action="store_true",
        help="处理截取图时不处理同名的 .txt",
    )
    p.add_argument(
        "--delete-only-subdir",
        type=Path,
        action="append",
        default=[],
        help=(
            "仅允许删除这些子目录下的截取图（相对扫描根，可重复传入）。"
            "未落在这些目录下的命中只报告、不删除。"
        ),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    root = args.dir.resolve()
    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 1

    do_remove = bool(args.delete or args.trash_dir is not None)
    if args.dry_run and not do_remove:
        print("提示: --dry-run 需配合 --delete 或 --trash-dir 使用", file=sys.stderr)

    delete_only_dirs: list[Path] = []
    for p in args.delete_only_subdir:
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (root / p).resolve()
        if not resolved.is_relative_to(root):
            print(f"错误: 子目录不在扫描根下: {p}", file=sys.stderr)
            return 1
        delete_only_dirs.append(resolved)

    paths = _collect_paths(root, recursive=not args.no_recursive)
    if len(paths) < 2:
        print(f"图片不足 2 张（找到 {len(paths)} 张）")
        return 0

    print(f"扫描到 {len(paths)} 张图片，提取特征中…")
    images: list[ImgInfo] = []
    failed: list[Path] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(_load_one, p, args.max_side, args.features): p for p in paths
        }
        for fut in as_completed(futs):
            p = futs[fut]
            info = fut.result()
            if info is None:
                failed.append(p)
            else:
                images.append(info)
    if failed:
        print(f"警告: {len(failed)} 个文件无法读取，已跳过", file=sys.stderr)
    images.sort(key=lambda x: str(x.path))
    print(f"成功加载 {len(images)} 张")

    tasks: list[tuple[int, list[int]]] = []
    for i, crop in enumerate(images):
        area_c = crop.width * crop.height
        parents = []
        for j, src in enumerate(images):
            if i == j:
                continue
            area_s = src.width * src.height
            if area_c >= area_s:
                continue
            ratio = area_c / area_s
            if args.min_area_ratio <= ratio <= args.max_area_ratio:
                parents.append(j)
        if parents:
            tasks.append((i, parents))

    total_pairs = sum(len(p) for _, p in tasks)
    print(f"待比对 {total_pairs} 对（{len(tasks)} 张候选截取图）…")

    all_hits: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [
            ex.submit(
                _search_one,
                i,
                parents,
                images,
                args.min_matches,
                args.min_inlier_ratio,
                args.reproj,
            )
            for i, parents in tasks
        ]
        for fut in as_completed(futs):
            all_hits.extend(fut.result())
            done += 1
            if done % 20 == 0 or done == len(futs):
                print(f"  进度 {done}/{len(futs)}，已命中 {len(all_hits)}", flush=True)

    # 删除/回收时强制每张截取图只对应一个原图，避免重复处理
    use_best = args.best_only or do_remove
    if use_best:
        best: dict[str, dict] = {}
        for h in all_hits:
            prev = best.get(h["crop"])
            if prev is None or h["score"] > prev["score"]:
                best[h["crop"]] = h
        all_hits = list(best.values())

    all_hits.sort(key=lambda h: (-h["score"], h["crop"]))

    print()
    if not all_hits:
        print("未发现截取关系。")
    else:
        print(f"发现 {len(all_hits)} 组截取关系：\n")
        for h in all_hits:
            crop_rel = _rel_to(Path(h["crop"]), root)
            src_rel = _rel_to(Path(h["source"]), root)
            x, y, tw, th = h["region"]
            print(
                f"  [{h['score']:.4f}] {crop_rel}  ←  {src_rel}\n"
                f"           区域 ({x},{y}) {tw}x{th}  "
                f"inliers={h['inliers']}/{h['matches']}"
            )

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(all_hits, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n已写入 {args.json_out}")

    if not do_remove:
        return 0

    to_delete: list[Path] = []
    sidecar_txt: list[Path] = []
    protected = 0
    actions: list[dict] = []

    for h in all_hits:
        crop_path = Path(h["crop"])
        if delete_only_dirs and not _is_under_any(crop_path, delete_only_dirs):
            protected += 1
            continue
        to_delete.append(crop_path)
        sidecars: list[Path] = []
        if not args.no_sidecar_txt:
            cap = crop_path.with_suffix(".txt")
            if cap.is_file():
                sidecar_txt.append(cap)
                sidecars.append(cap)
        actions.append({
            "crop": crop_path,
            "source": Path(h["source"]),
            "score": h["score"],
            "sidecars": sidecars,
        })

    to_delete = sorted({p.resolve() for p in to_delete}, key=lambda x: str(x))
    sidecar_txt = sorted({p.resolve() for p in sidecar_txt}, key=lambda x: str(x))

    trash_root = args.trash_dir.resolve() if args.trash_dir is not None else None
    verb = "移动" if trash_root is not None else "删除"
    print()
    print(f"将{verb} {len(to_delete)} 张截取图", end="")
    if not args.no_sidecar_txt:
        print(f"，及 {len(sidecar_txt)} 个旁路 .txt", end="")
    print()
    if delete_only_dirs:
        rels = [str(p.relative_to(root)) for p in delete_only_dirs]
        print(f"仅处理这些子目录下的截取图: {', '.join(rels)}")
        if protected:
            print(f"有 {protected} 张截取图因不在允许删除的子目录内而跳过")
    if trash_root is not None:
        print(f"回收目录: {trash_root}")

    for i, act in enumerate(actions, start=1):
        crop = act["crop"]
        source = act["source"]
        print(f"[{i}] score={act['score']:.4f}")
        print(f"  KEEP {_rel_to(source, root)}")
        if trash_root is None:
            print(f"  DEL  {_rel_to(crop, root)}")
            for s in act["sidecars"]:
                print(f"  DEL  {_rel_to(s, root)}")
        else:
            rel = crop.resolve().relative_to(root)
            print(f"  MOV  {_rel_to(crop, root)} -> {trash_root / rel}")
            for s in act["sidecars"]:
                srel = s.resolve().relative_to(root)
                print(f"  MOV  {_rel_to(s, root)} -> {trash_root / srel}")

    if args.dry_run:
        print(f"[dry-run] 未{verb}任何文件")
        return 0

    if trash_root is None:
        for p in to_delete:
            if not p.is_file():
                print(f"跳过（不存在）: {p}", file=sys.stderr)
                continue
            p.unlink()
        for p in sidecar_txt:
            if not p.is_file():
                continue
            p.unlink()
    else:
        for p in to_delete:
            if not p.is_file():
                print(f"跳过（不存在）: {p}", file=sys.stderr)
                continue
            _move_keep_rel(p, root, trash_root)
        for p in sidecar_txt:
            if not p.is_file():
                continue
            _move_keep_rel(p, root, trash_root)

    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
