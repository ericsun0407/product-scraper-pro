"""
product-scraper-pro: miyazakichair.com 图片爬虫
递归抓取所有页面，下载高质量产品图片，按结构分类保存
"""

import os
import re
import sys
import time
import hashlib
import logging
import argparse
from urllib.parse import urljoin, urlparse, urlunparse
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image
import imagehash
import io

# ── 配置 ──────────────────────────────────────────────────────────────────────

BASE_URL = "https://miyazakichair.com"
DOMAIN = "miyazakichair.com"
OUTPUT_DIR = Path("downloads")
MAX_DEPTH = 4
REQUEST_DELAY = 1.0          # 每次请求间隔（秒）
MIN_WIDTH = 200              # 最小图片宽度（像素）
MIN_HEIGHT = 200             # 最小图片高度
MIN_FILE_SIZE = 15_000       # 最小文件大小（字节）
PHASH_THRESHOLD = 8          # 感知哈希相似度阈值

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 排除 URL 关键词
SKIP_URL_PATTERNS = [
    r"/wp-admin", r"/wp-login", r"\?s=", r"/search",
    r"/cart", r"/checkout", r"/account", r"/login",
    r"#", r"javascript:", r"mailto:", r"tel:",
]

# 排除图片 URL 关键词（缩略图/图标/装饰）
SKIP_IMG_PATTERNS = [
    r"-\d+x\d+\.",           # WordPress 缩略图 e.g. -300x200.jpg
    r"favicon",
    r"logo",
    r"/icon",
    r"loading",
    r"spinner",
    r"placeholder",
    r"avatar",
    r"social",
    r"facebook",
    r"instagram",
    r"twitter",
    r"linkedin",
    r"youtube",
    r"arrow",
    r"bullet",
    r"divider",
    r"separator",
    r"btn-",
    r"-btn\.",
    r"background-pattern",
]

# 路径 → 分类目录映射
CATEGORY_MAP = [
    (r"/collection|/products?|/chair|/sofa|/table|/cabinet|/furniture", "products"),
    (r"/designer|/people",                                              "designers"),
    (r"/material|/wood|/upholstery|/fabric|/leather",                  "materials"),
    (r"/craftsmanship|/process|/craft|/production|/workshop",          "craftsmanship"),
    (r"/brand|/history|/about|/philosophy|/planet|/factory|/story",    "brand"),
    (r"/journal|/news|/blog",                                           "journal"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── 状态 ──────────────────────────────────────────────────────────────────────

visited_urls: set[str] = set()
downloaded_url_hashes: set[str] = set()   # URL sha256
downloaded_file_hashes: set[str] = set()  # 文件内容 md5
downloaded_phashes: list = []             # 感知哈希列表
stats = defaultdict(int)

session = requests.Session()
session.headers.update(HEADERS)

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """去掉 fragment 和末尾多余斜线，统一小写 scheme+host"""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", p.query, ""))


def is_internal(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "" or host == DOMAIN or host.endswith("." + DOMAIN)


def should_skip_url(url: str) -> bool:
    for pat in SKIP_URL_PATTERNS:
        if re.search(pat, url, re.I):
            return True
    return False


def should_skip_img(url: str) -> bool:
    for pat in SKIP_IMG_PATTERNS:
        if re.search(pat, url, re.I):
            return True
    return False


def pick_category(page_url: str) -> str:
    path = urlparse(page_url).path.lower()
    for pattern, cat in CATEGORY_MAP:
        if re.search(pattern, path, re.I):
            return cat
    return "uncategorized"


def slug_from_url(url: str, max_len: int = 60) -> str:
    """把 URL path 转成文件夹安全的 slug"""
    path = urlparse(url).path
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return slug[:max_len] or "root"


def best_src(tag) -> str | None:
    """从 <img> 标签提取最高分辨率的 src（优先 srcset 最大值）"""
    srcset = tag.get("data-srcset") or tag.get("srcset") or ""
    if srcset:
        candidates = []
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if len(tokens) >= 2:
                try:
                    w = float(tokens[1].rstrip("wx"))
                    candidates.append((w, tokens[0]))
                except ValueError:
                    pass
            elif len(tokens) == 1:
                candidates.append((0, tokens[0]))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        val = tag.get(attr)
        if val and not val.startswith("data:"):
            return val
    return None


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def file_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def phash_similar(new_hash, threshold: int = PHASH_THRESHOLD) -> bool:
    for ph in downloaded_phashes:
        if abs(new_hash - ph) <= threshold:
            return True
    return False


def clean_img_url(img_url: str) -> str:
    """去掉 WordPress 缩略图尺寸后缀，还原原图 URL"""
    # e.g. image-300x200.jpg → image.jpg
    cleaned = re.sub(r"-\d+x\d+(\.[a-z]{2,5})$", r"\1", img_url, flags=re.I)
    return cleaned


def fetch(url: str, stream: bool = False, timeout: int = 20):
    try:
        r = session.get(url, stream=stream, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        log.warning(f"请求失败 {url}: {e}")
        return None

# ── 图片下载 ──────────────────────────────────────────────────────────────────

def download_image(img_url: str, dest_dir: Path) -> bool:
    """下载单张图片，执行全套去重与质量检查，成功返回 True"""
    # 1. URL 去重
    uhash = url_hash(img_url)
    if uhash in downloaded_url_hashes:
        log.debug(f"URL 重复跳过: {img_url}")
        stats["skipped_url_dup"] += 1
        return False
    downloaded_url_hashes.add(uhash)

    # 2. 排除图标/装饰图
    if should_skip_img(img_url):
        log.debug(f"规则排除: {img_url}")
        stats["skipped_pattern"] += 1
        return False

    # 3. 尝试还原原图（去掉缩略图尺寸）
    original_url = clean_img_url(img_url)
    if original_url != img_url:
        log.debug(f"还原原图 URL: {img_url} → {original_url}")
        img_url = original_url

    # 4. 下载
    r = fetch(img_url, stream=True)
    if not r:
        return False

    content_type = r.headers.get("Content-Type", "")
    if "image" not in content_type and "octet-stream" not in content_type:
        log.debug(f"非图片 Content-Type: {content_type}")
        return False

    data = r.content
    if len(data) < MIN_FILE_SIZE:
        log.debug(f"文件过小({len(data)}B): {img_url}")
        stats["skipped_small_file"] += 1
        return False

    # 5. 文件内容 MD5 去重
    fhash = file_md5(data)
    if fhash in downloaded_file_hashes:
        log.debug(f"内容重复跳过: {img_url}")
        stats["skipped_content_dup"] += 1
        return False

    # 6. PIL 检查尺寸 + 感知哈希
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            log.debug(f"尺寸过小({w}x{h}): {img_url}")
            stats["skipped_too_small"] += 1
            return False

        ph = imagehash.phash(img)
        if phash_similar(ph):
            log.debug(f"感知哈希相似跳过: {img_url}")
            stats["skipped_phash_dup"] += 1
            return False
        downloaded_phashes.append(ph)

    except Exception as e:
        log.debug(f"图片解析失败({e}): {img_url}")
        stats["skipped_parse_error"] += 1
        return False

    # 7. 确定文件名
    parsed = urlparse(img_url)
    filename = Path(parsed.path).name
    if not filename or "." not in filename:
        ext = content_type.split("/")[-1].split(";")[0].strip()
        ext = {"jpeg": "jpg"}.get(ext, ext) or "jpg"
        filename = fhash[:16] + "." + ext

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    # 若同名文件已存在，在文件名里插入哈希片段避免覆盖
    if dest_path.exists():
        stem, suffix = dest_path.stem, dest_path.suffix
        dest_path = dest_dir / f"{stem}_{fhash[:8]}{suffix}"

    dest_path.write_bytes(data)
    downloaded_file_hashes.add(fhash)
    stats["downloaded"] += 1
    log.info(f"✓ 已下载({w}x{h}) → {dest_path.relative_to(OUTPUT_DIR)}")
    return True


# ── 页面解析 ──────────────────────────────────────────────────────────────────

def extract_images(soup: BeautifulSoup, page_url: str) -> list[str]:
    """从页面提取所有候选图片 URL（绝对路径）"""
    urls = []
    seen = set()

    def add(url):
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    # <img> 标签
    for tag in soup.find_all("img"):
        src = best_src(tag)
        if src:
            add(urljoin(page_url, src))

    # CSS background-image (style 属性 & <style> 块)
    for tag in soup.find_all(style=True):
        for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', tag["style"]):
            add(urljoin(page_url, m.group(1)))

    for style_tag in soup.find_all("style"):
        for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', style_tag.get_text()):
            add(urljoin(page_url, m.group(1)))

    # data-bg / data-background 懒加载
    for attr in ("data-bg", "data-background", "data-background-image"):
        for tag in soup.find_all(attrs={attr: True}):
            add(urljoin(page_url, tag[attr]))

    # <picture> / <source>
    for tag in soup.find_all("source"):
        srcset = tag.get("srcset", "")
        for part in srcset.split(","):
            part = part.strip().split()[0]
            if part:
                add(urljoin(page_url, part))

    return urls


def extract_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """提取页面内所有内部链接（标准化去重）"""
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        abs_url = urljoin(page_url, href)
        if not is_internal(abs_url):
            continue
        norm = normalize_url(abs_url)
        if norm in seen or should_skip_url(norm):
            continue
        seen.add(norm)
        links.append(norm)
    return links


# ── 递归抓取 ──────────────────────────────────────────────────────────────────

def scrape_page(url: str, depth: int = 0):
    if depth > MAX_DEPTH:
        return
    norm = normalize_url(url)
    if norm in visited_urls:
        return
    visited_urls.add(norm)
    stats["pages_visited"] += 1

    log.info(f"[depth={depth}] 抓取页面: {url}")
    r = fetch(url)
    if not r:
        return

    time.sleep(REQUEST_DELAY)

    soup = BeautifulSoup(r.text, "html.parser")

    # 确定保存目录：category / page-slug
    category = pick_category(url)
    page_slug = slug_from_url(url)
    dest_dir = OUTPUT_DIR / category / page_slug

    # 下载本页图片
    img_urls = extract_images(soup, url)
    for img_url in img_urls:
        download_image(img_url, dest_dir)
        time.sleep(0.3)

    # 递归子链接
    if depth < MAX_DEPTH:
        links = extract_links(soup, url)
        for link in links:
            scrape_page(link, depth + 1)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="miyazakichair.com 图片爬虫")
    parser.add_argument("--url", default=BASE_URL, help="起始 URL")
    parser.add_argument("--output", default="downloads", help="输出目录")
    parser.add_argument("--depth", type=int, default=MAX_DEPTH, help="最大抓取深度")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="请求间隔（秒）")
    args = parser.parse_args()

    global OUTPUT_DIR, MAX_DEPTH, REQUEST_DELAY
    OUTPUT_DIR = Path(args.output)
    MAX_DEPTH = args.depth
    REQUEST_DELAY = args.delay

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"=== 开始抓取: {args.url} ===")
    log.info(f"输出目录: {OUTPUT_DIR.resolve()}")

    scrape_page(args.url)

    log.info("\n=== 完成 ===")
    log.info(f"  访问页面数:   {stats['pages_visited']}")
    log.info(f"  下载图片数:   {stats['downloaded']}")
    log.info(f"  URL重复跳过:  {stats['skipped_url_dup']}")
    log.info(f"  内容重复跳过: {stats['skipped_content_dup']}")
    log.info(f"  感知哈希去重: {stats['skipped_phash_dup']}")
    log.info(f"  规则排除:     {stats['skipped_pattern']}")
    log.info(f"  尺寸过小:     {stats['skipped_too_small']}")


if __name__ == "__main__":
    main()
