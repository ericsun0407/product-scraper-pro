"""
图片去重脚本：
1. 文件内容 MD5 完全去重（同一文件的多份拷贝）
2. 感知哈希去重（视觉相同但文件名/尺寸不同）—— 只保留最大尺寸版本
"""

import os
import sys
import hashlib
import io
from pathlib import Path
from collections import defaultdict

from PIL import Image
import imagehash

DOWNLOADS_DIR = Path("downloads")
PHASH_THRESHOLD = 6   # 差值 ≤6 视为相同图片（0=完全一致，10≈相似）

def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()

def phash(path: Path):
    try:
        return imagehash.phash(Image.open(path))
    except Exception:
        return None

def img_area(path: Path) -> int:
    try:
        w, h = Image.open(path).size
        return w * h
    except Exception:
        return 0

def collect_images(root: Path) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
    return [p for p in root.rglob("*") if p.suffix.lower() in exts and p.is_file()]

def run():
    images = collect_images(DOWNLOADS_DIR)
    print(f"共找到图片: {len(images)} 张")

    # ── 第一轮：MD5 完全去重 ──────────────────────────────────────
    md5_map: dict[str, list[Path]] = defaultdict(list)
    for p in images:
        md5_map[md5(p)].append(p)

    md5_deleted = 0
    for digest, paths in md5_map.items():
        if len(paths) <= 1:
            continue
        # 保留文件名最短的（通常是原图，不带哈希后缀）
        paths.sort(key=lambda p: (len(p.name), p.name))
        keep = paths[0]
        for dup in paths[1:]:
            print(f"  [MD5重复] 删除 {dup.relative_to(DOWNLOADS_DIR)}  (保留 {keep.relative_to(DOWNLOADS_DIR)})")
            dup.unlink()
            md5_deleted += 1

    print(f"\nMD5 去重删除: {md5_deleted} 张")

    # ── 第二轮：感知哈希去重 ─────────────────────────────────────
    # 重新收集（已删部分不再计入）
    images = collect_images(DOWNLOADS_DIR)

    # 每张图计算 (phash, 面积, path)
    records: list[tuple] = []
    for p in images:
        ph = phash(p)
        if ph is None:
            continue
        area = img_area(p)
        records.append((ph, area, p))

    # 按面积从大到小排序，大图优先保留
    records.sort(key=lambda x: x[1], reverse=True)

    kept: list[tuple] = []   # 已确认保留的
    phash_deleted = 0

    for ph, area, path in records:
        duplicate = False
        for kph, karea, kpath in kept:
            if abs(ph - kph) <= PHASH_THRESHOLD:
                # 当前图比已保留图小（或相同），删除当前图
                print(f"  [pHash重复] 删除 {path.relative_to(DOWNLOADS_DIR)}")
                print(f"              保留 {kpath.relative_to(DOWNLOADS_DIR)}  ({karea} px² vs {area} px²)")
                path.unlink()
                phash_deleted += 1
                duplicate = True
                break
        if not duplicate:
            kept.append((ph, area, path))

    print(f"\npHash 去重删除: {phash_deleted} 张")

    # ── 清理空目录 ────────────────────────────────────────────────
    empty = 0
    for d in sorted(DOWNLOADS_DIR.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            empty += 1

    remaining = len(collect_images(DOWNLOADS_DIR))
    print(f"\n清理空目录: {empty} 个")
    print(f"最终保留图片: {remaining} 张")

if __name__ == "__main__":
    run()
