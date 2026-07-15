# product-scraper-pro

智能家具产品图片爬虫 — 专为 miyazakichair.com 设计，支持递归抓取、高质量图片筛选与分类存储。

## 功能

- 递归遍历网站所有内部页面（可配置深度）
- 自动提取 `<img>` / `srcset` / CSS `background-image` / 懒加载图片
- 三重去重：URL sha256 / 文件内容 MD5 / 感知哈希（phash）
- 自动过滤缩略图、图标、Logo、社交图标等无关资源
- 按网站结构自动分类：products / designers / materials / craftsmanship / brand / journal / uncategorized
- 还原 WordPress 缩略图为原图 URL
- 遵守访问频率限制，内置延迟

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
# 默认参数（抓取 miyazakichair.com，输出到 downloads/）
python scraper.py

# 自定义参数
python scraper.py \
  --url https://miyazakichair.com \
  --output downloads \
  --depth 4 \
  --delay 1.0
```

## 输出结构

```
downloads/
├── products/
│   └── <page-slug>/     # 产品页图片
├── designers/
├── materials/
├── craftsmanship/
├── brand/
├── journal/
└── uncategorized/
```

## 筛选规则

**排除**：缩略图（-WxH.jpg）、favicon、logo、icon、loading、社交图标、arrow、尺寸 < 200×200px、文件 < 15KB

**保留**：产品主图、场景图、工艺细节图、材质图、设计师及品牌高清图
