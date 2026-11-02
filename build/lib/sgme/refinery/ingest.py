"""refinery/ingest.py：输入处理。

- ingest(source) → (text, metadata)：统一入口，自动识别输入类型
  - 纯文本 → 直接透传
  - 本地文件（md/txt）→ 读取为纯文本
  - URL（http/https）→ httpx GET 拉取 HTML 转 markdown
- pdf/docx/图片/视频：预留接口，后续版本实现（raise NotImplementedError 标注）
- 失败统一抛 IngestError
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import httpx

# 支持的本地文本文件扩展名（pdf/docx 后续版本支持）
_TEXT_EXTS = {".md", ".markdown", ".txt"}
# 已知但暂不支持的输入类型（预留接口，标注后续）
_UNSUPPORTED_EXTS = {
    ".pdf": "PDF 解析后续版本支持",
    ".docx": "DOCX 解析后续版本支持",
    ".doc": "DOC 解析后续版本支持",
    ".png": "图片理解后续版本支持（视觉模型描述）",
    ".jpg": "图片理解后续版本支持（视觉模型描述）",
    ".jpeg": "图片理解后续版本支持（视觉模型描述）",
    ".webp": "图片理解后续版本支持（视觉模型描述）",
    ".gif": "图片理解后续版本支持（视觉模型描述）",
    ".mp4": "视频转写后续版本支持",
    ".mkv": "视频转写后续版本支持",
    ".mp3": "音频转写后续版本支持",
    ".wav": "音频转写后续版本支持",
}


class IngestError(Exception):
    """输入处理失败（文件不存在 / 网络错误 / HTTP 非 2xx 等）。"""


def _is_url(source: str) -> bool:
    """粗略判断输入是否为 URL（http/https 前缀）。"""
    return source.startswith(("http://", "https://"))


def _is_file_path(source: str) -> bool:
    """判断输入是否像本地文件路径。

    存在即视为文件；不存在但带已知文本/预留扩展名（.md/.txt/.pdf/...）也视为
    文件意图 → 交由 ingest_file 抛 IngestError（文件不存在）。
    """
    if Path(source).is_file():
        return True
    ext = Path(source).suffix.lower()
    return ext in _TEXT_EXTS or ext in _UNSUPPORTED_EXTS


def ingest(source: str) -> tuple[str, dict]:
    """输入处理统一入口：文本透传 / 本地文件 / URL。

    Args:
        source: 纯文本、本地文件路径或 URL。

    Returns:
        (text, metadata)：text 为纯文本内容；metadata 含
        source_type（text/file/url）、title、source_file、source_url。

    Raises:
        IngestError: 文件不存在或 URL 拉取失败。
        NotImplementedError: pdf/docx/图片/视频等暂不支持的输入。
    """
    if _is_url(source):
        return ingest_url(source)
    if _is_file_path(source):
        return ingest_file(source)
    # 其余一律按纯文本透传
    return source, {
        "source_type": "text",
        "title": None,
        "source_file": None,
        "source_url": None,
    }


def ingest_file(path: str) -> tuple[str, dict]:
    """读取本地文本文件（md/txt）。

    pdf/docx 及图片、视频扩展名 → NotImplementedError 标注后续支持。
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _UNSUPPORTED_EXTS:
        raise NotImplementedError(_UNSUPPORTED_EXTS[ext])
    if ext not in _TEXT_EXTS:
        raise IngestError(f"不支持的文件类型: {ext or '(无扩展名)'}（仅支持 {'/'.join(sorted(_TEXT_EXTS))}）")
    if not p.is_file():
        raise IngestError(f"文件不存在: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise IngestError(f"文件编码不是 UTF-8，无法读取: {p}") from e
    metadata = {
        "source_type": "file",
        "title": p.stem,
        "source_file": str(p),
        "source_url": None,
    }
    return text, metadata


def ingest_url(url: str, timeout_s: float = 20.0) -> tuple[str, dict]:
    """拉取 URL 内容并转为 markdown 文本。

    - trust_env=False：防 Clash 等系统代理劫持（项目铁律）
    - HTTP 非 2xx 或网络异常 → IngestError
    """
    try:
        resp = httpx.get(
            url,
            timeout=timeout_s,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "SGME-Refinery/0.7"},
        )
    except httpx.HTTPError as e:
        raise IngestError(f"URL 拉取失败: {url} ({e})") from e
    if resp.status_code != 200:
        raise IngestError(f"URL 返回 HTTP {resp.status_code}: {url}")

    markdown, title = _html_to_markdown(resp.text)
    if not markdown.strip():
        raise IngestError(f"URL 内容为空: {url}")
    metadata = {
        "source_type": "url",
        "title": title or _url_title(url),
        "source_file": None,
        "source_url": url,
    }
    return markdown, metadata


def _url_title(url: str) -> str:
    """URL 无 <title> 时，用域名兜底作为标题。"""
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


# ---------- HTML → Markdown 轻量转换（正则实现，不引入额外依赖） ----------

def _clean(fragment: str) -> str:
    """去掉片段内残留标签并反转义实体。"""
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).strip()


def _html_to_markdown(raw_html: str) -> tuple[str, str | None]:
    """极简 HTML → Markdown 转换，返回 (markdown, title)。

    覆盖常见博客/文档页结构；复杂页面后续可换 Firecrawl/web_extract。
    """
    title: str | None = None
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.S | re.I)
    if m:
        title = _clean(m.group(1))

    text = raw_html
    # 剔除 script/style 等非可见内容
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    # 标题 → # 前缀
    for level in range(6, 0, -1):
        text = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m: f"\n{'#' * level} {_clean(m.group(1))}\n",
            text,
            flags=re.S | re.I,
        )
    # 链接 → [文本](href)
    text = re.sub(
        r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        lambda m: f"[{_clean(m.group(2))}]({m.group(1)})",
        text,
        flags=re.S | re.I,
    )
    # 列表项 → "- "（须在块级标签替换之前处理，否则 <li> 已被吃掉）
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    # 块级标签 → 换行
    text = re.sub(r"<(p|div|br|tr|blockquote|pre)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6]|blockquote|pre|table)>", "\n", text, flags=re.I)
    # 残留标签剥除 + 实体反转义
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # 压缩连续空行
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip(), title
