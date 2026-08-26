#!/usr/bin/env python
"""scripts/migrate_wiki_skills.py：wiki skill 页批量迁移（ST-36 M4a）。

把 wiki 里混入的 ``skill:*`` 页面机械迁出为正式技能文件（默认 dry-run，
不写任何远端/本地状态）。设计依据 docs/design/SGME-Skills管理模块设计-v0.2.md §七 M4a：
读全文 → 补 frontmatter → 产出报告人工过目；误挂标签的知识页只列「建议摘标签」
清单（不自动改）。**原件永不删**：wiki 原页只置 superseded 指向新技能，不删除。

输入源两种模式（二选一）：
- ``--db <wiki.db路径>``：直连 SQLite（表 wiki_pages：page_id/title/content/
  category/tags/status，tags 是 JSON 数组字符串）
- ``--api <base_url> --key-env <环境变量名>``：走 GET /v1/wiki/pages、
  GET /v1/wiki/pages/{page_id}（X-API-Key 头）

筛选：status='active' 且 tags 含 "skill"；title 约定 ``skill:<name>``，
无前缀的页面用 title 归一化为技能名。

产出：对每页生成目标 SKILL.md 内容（frontmatter + 原文 body）。
- ``--out-dir <dir>``：写到本地目录树 <out-dir>/<name>/SKILL.md 供人工检查；
- ``--apply``（仅 --api 模式）：PUT /v1/admin/skills/{name} 推远端，然后 PATCH
  /v1/wiki/pages/{page_id} 把原页置 superseded——PATCH 端点不存在时打印 SKIP
  提示手工处理（原页不动，符合原件永不删）。

幂等：同名已存在（--out-dir 已有文件或 API GET 探测到）跳过并记 skipped。

用法示例：
  python scripts/migrate_wiki_skills.py --db data/wiki.db --out-dir tmp/migrated
  python scripts/migrate_wiki_skills.py --api http://nas:9910 --key-env SGME_AGENT_KEY \
      --apply --report tmp/migration-report.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class WikiPage:
    """wiki_pages 行的最小投影（脚本只关心这几个字段）。"""

    page_id: str
    title: str
    content: str
    category: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class SkillDraft:
    """一页 wiki skill 页对应的待产出技能草稿。"""

    page: WikiPage
    name: str            # 技能名（title 去 skill: 前缀或 title 归一化）
    version: str         # 原文内 version 行，缺省 0.1.0
    category: str        # 原 category 去 skill/ 前缀
    description: str     # 正文首个非标题段前 57 字
    triggers: list[str]  # 从原文「触发词」行提取（若有）
    body: str            # 原文 body


@dataclass
class Report:
    """迁移报告：总数/成功/跳过/失败 + 摘标签建议。"""

    total: int = 0
    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    entries: list[str] = field(default_factory=list)       # 每项一行原因
    untag_suggestions: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.entries.append(line)


# ---------------------------------------------------------------------------
# 解析与 frontmatter 生成
# ---------------------------------------------------------------------------

# title 归一化：非 [a-z0-9-] 一律折叠为连字符（中文页名转拼音超出本脚本职责，
# 归一化保证产物是合法目录名即可；同名冲突时后到者记 failed 并在报告中说明）。
_TITLE_NORM_RE = re.compile(r"[^a-z0-9\-]+")
_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)
_TRIGGERS_RE = re.compile(r"^触发词[:：]\s*(.+)$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6}\s")


def normalize_title(title: str) -> str:
    """title → 合法技能名：小写、非字母数字折叠为连字符、去首尾连字符。"""
    lowered = (title or "").strip().lower()
    return _TITLE_NORM_RE.sub("-", lowered).strip("-") or "unnamed-skill"


def parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """剥离页首 YAML frontmatter（--- 围栏），返回 (kv字典, body)。

    wiki 页可能已带 frontmatter（如 version 行）；没有则 body 即原文。
    """
    text = content.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, content
    lines = text.splitlines()
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, content
    kv: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" in line:
            key, _, value = line.partition(":")
            kv[key.strip()] = value.strip()
    return kv, "\n".join(lines[end_idx + 1 :]).lstrip("\n")


def extract_triggers(body: str) -> list[str]:
    """从原文「触发词」行提取触发词列表（逗号/顿号/分号分隔均可）。"""
    match = _TRIGGERS_RE.search(body)
    if not match:
        return []
    parts = re.split(r"[,，;；、\s]+", match.group(1).strip())
    return [p for p in (part.strip() for part in parts) if p]


def build_draft(page: WikiPage) -> SkillDraft:
    """一页 wiki 页 → 技能草稿（frontmatter 字段全部按任务书规则补齐）。"""
    fm, raw_body = parse_frontmatter(page.content)
    # title 带 skill: 前缀则去掉；无前缀的用 title 归一化为名。
    if page.title.lower().startswith("skill:"):
        name = normalize_title(page.title.split(":", 1)[1])
    else:
        name = normalize_title(page.title)
    version = fm.get("version") or "0.1.0"
    m = _VERSION_RE.search(raw_body)
    if not fm.get("version") and m:
        # 正文里的独立 version: 行（frontmatter 外）也认
        version = m.group(1)
    category = page.category or ""
    if category.startswith("skill/"):
        category = category[len("skill/") :]
    body = raw_body
    description = ""
    for para in re.split(r"\n\s*\n", body):
        stripped = para.strip()
        if not stripped or _HEADING_RE.match(stripped):
            continue
        description = stripped[:57]
        break
    triggers = extract_triggers(body)
    return SkillDraft(
        page=page,
        name=name,
        version=version,
        category=category,
        description=description,
        triggers=triggers,
        body=body,
    )


def render_skill_md(draft: SkillDraft) -> str:
    """草稿 → 目标 SKILL.md 文本（frontmatter + 原文 body 原样保留）。"""
    fm_lines = [
        "---",
        f"name: {draft.name}",
        f"version: {draft.version}",
        "pattern: manual",  # PR-7：迁移件默认按需检索（热集甄选后升 auto）
    ]
    if draft.category:
        fm_lines.append(f"category: {draft.category}")
    desc = draft.description.replace('"', "'")
    fm_lines.append(f'description: "{desc}"')
    if draft.triggers:
        quoted = ", ".join(f'"{t}"' for t in draft.triggers)
        fm_lines.append(f"triggers: [{quoted}]")
    fm_lines.append("---")
    body = draft.body if draft.body.startswith("\n") else "\n" + draft.body
    return "\n".join(fm_lines) + "\n" + body


# ---------------------------------------------------------------------------
# 输入源：SQLite / API
# ---------------------------------------------------------------------------

SKILL_TAG = "skill"


def load_pages_from_db(db_path: Path) -> list[WikiPage]:
    """直连 SQLite 读 wiki_pages 全表（tags 是 JSON 数组字符串）。"""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT page_id, title, content, category, tags, status FROM wiki_pages"
        ).fetchall()
    finally:
        conn.close()
    pages: list[WikiPage] = []
    for page_id, title, content, category, tags_json, status in rows:
        try:
            tags = json.loads(tags_json) if tags_json else []
            if not isinstance(tags, list):
                tags = []
        except (json.JSONDecodeError, TypeError):
            tags = []
        pages.append(
            WikiPage(
                page_id=str(page_id),
                title=title or "",
                content=content or "",
                category=category or "",
                tags=[str(t) for t in tags],
                status=status or "active",
            )
        )
    return pages


def load_pages_from_api(base_url: str, api_key: str) -> list[WikiPage]:
    """走 API 列表端点分页拉全量页（limit≤200 + offset 循环直到取空）。

    服务端默认 limit=50 且按 updated_at 降序——不分页会静默漏掉旧页
    （2026-08-26 实测：NAS 200+ 页只回前 50 条近期研究页，skill 页全漏）。
    失败抛 RuntimeError。
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key}
    pages: list[WikiPage] = []
    seen_ids: set[str] = set()
    offset = 0
    while True:
        resp = requests.get(
            f"{base}/v1/wiki/pages",
            params={"limit": 200, "offset": offset},
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"GET /v1/wiki/pages 失败: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        items = data.get("pages") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise RuntimeError("GET /v1/wiki/pages 响应格式异常：既非列表也无 pages 键")
        batch_new = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("page_id", ""))
            if pid in seen_ids:  # 防御：服务端异常时死循环保护
                continue
            seen_ids.add(pid)
            batch_new += 1
            tags = item.get("tags")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except json.JSONDecodeError:
                    tags = [tags]
            pages.append(
                WikiPage(
                    page_id=pid,
                    title=item.get("title", "") or "",
                    content=item.get("content", "") or "",
                    category=item.get("category", "") or "",
                    tags=[str(t) for t in tags] if isinstance(tags, list) else [],
                    status=item.get("status", "active") or "active",
                )
            )
        # 本批无新条目（取空或翻页越界）→ 结束
        if batch_new == 0 or len(items) < 1:
            break
        offset += len(items)
        if len(pages) > 10000:  # 绝对上限防失控
            break
    return pages


def fetch_page_content_api(base_url: str, api_key: str, page_id: str) -> str:
    """详情端点取单页全文（列表响应可能不含 content 时兜底）。"""
    url = f"{base_url.rstrip('/')}/v1/wiki/pages/{page_id}"
    resp = requests.get(url, headers={"X-API-Key": api_key}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"GET /v1/wiki/pages/{page_id} 失败: HTTP {resp.status_code}")
    payload = resp.json()
    if isinstance(payload, dict):
        return payload.get("content", "") or ""
    return ""


def remote_skill_exists(base_url: str, api_key: str, name: str) -> bool:
    """API 幂等探测：GET /v1/admin/skills/{name} 是否已存在该技能。"""
    url = f"{base_url.rstrip('/')}/v1/admin/skills/{name}"
    try:
        resp = requests.get(url, headers={"X-API-Key": api_key}, timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"探测技能 {name} 失败: {exc}") from exc
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# 迁移主流程
# ---------------------------------------------------------------------------


def select_skill_pages(pages: list[WikiPage]) -> tuple[list[WikiPage], list[WikiPage]]:
    """筛 status='active' 且 tags 含 "skill" 的页；同时识别误挂标签知识页。

    返回 (skill_pages, untag_suggestions)：后者是 title 不带 skill: 前缀但
    tags 含 "skill" 的页面（建议摘标签清单，不自动改）。
    """
    skill_pages: list[WikiPage] = []
    untag: list[WikiPage] = []
    for page in pages:
        if (page.status or "").lower() != "active":
            continue
        if SKILL_TAG not in page.tags:
            continue
        if page.title.lower().startswith("skill:"):
            skill_pages.append(page)
        else:
            # 无前缀但挂了 skill 标签：大概率知识页误挂标签 → 只进报告不改数据
            untag.append(page)
    return skill_pages, untag


def migrate(
    *,
    pages: list[WikiPage],
    out_dir: Path | None = None,
    apply_remote: bool = False,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Report:
    """核心迁移流程（纯函数式入口，测试直接调它免 subprocess）。"""
    report = Report()
    skill_pages, untag = select_skill_pages(pages)
    candidates = sorted(skill_pages, key=lambda p: p.page_id)
    report.total = len(candidates)
    seen_names: set[str] = set()
    for page in candidates:
        try:
            draft = build_draft(page)
        except Exception as exc:  # 单页解析失败不阻断整批
            report.failed += 1
            report.add(f"- ❌ FAIL {page.page_id} `{page.title}`：解析失败 {exc}")
            continue
        if draft.name in seen_names:
            report.failed += 1
            report.add(f"- ❌ FAIL {page.page_id} `{page.title}`：技能名 `{draft.name}` 与前序页冲突")
            continue
        seen_names.add(draft.name)
        exists_locally = out_dir is not None and (out_dir / draft.name / "SKILL.md").exists()
        exists_remote = False
        if apply_remote and base_url and api_key:
            exists_remote = remote_skill_exists(base_url, api_key, draft.name)
        if exists_locally or exists_remote:
            report.skipped += 1
            reason = "out-dir 已有同名文件" if exists_locally else "远端已存在同名技能"
            report.add(f"- ⏭️ SKIP {page.page_id} `{page.title}` → `{draft.name}`（{reason}）")
            continue
        rendered = render_skill_md(draft)
        try:
            if apply_remote and base_url and api_key:
                push_and_supersede(base_url, api_key, draft, page.page_id)
                report.migrated += 1
                report.add(f"- ✅ OK {page.page_id} `{page.title}` → 远端技能 `{draft.name}`，原页置 superseded")
            elif out_dir is not None:
                target_dir = out_dir / draft.name
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / "SKILL.md").write_text(rendered, encoding="utf-8")
                report.migrated += 1
                report.add(f"- ✅ OK {page.page_id} `{page.title}` → {target_dir / 'SKILL.md'}")
            else:
                report.migrated += 1
                report.add(f"- ✅ OK(预览) {page.page_id} `{page.title}` → `{draft.name}`（未落盘：未给 --out-dir/--apply）")
        except Exception as exc:
            report.failed += 1
            report.add(f"- ❌ FAIL {page.page_id} `{page.title}`：{exc}")
    for page in untag:
        report.untag_suggestions.append(f"- `{page.title}` (page_id={page.page_id})：无 skill: 前缀但挂了 skill 标签，建议摘标签保留 wiki")
    return report


def push_and_supersede(base_url: str, api_key: str, draft: SkillDraft, page_id: str) -> None:
    """--apply --api 模式的真实推远端：PUT 技能 + PATCH 原页置 superseded。

    PATCH 端点不存在（404/405）→ 打印 SKIP 提示手工处理，不算失败（原页未动）。
    """
    put_url = f"{base_url.rstrip('/')}/v1/admin/skills/{draft.name}"
    resp = requests.put(
        put_url,
        headers={"X-API-Key": api_key},
        # skip_limits：历史存量整体入库（2026-08-26 用户裁决），超 8K 大件放行为警告
        json={"content": render_skill_md(draft), "skip_limits": True},
        timeout=30,
    )
    # 429 限流退避重试（最多 4 次，读 Retry-After 头；2026-08-26 批量迁移实测必需）
    attempt = 0
    while resp.status_code == 429 and attempt < 4:
        retry_after = int(resp.headers.get("Retry-After", "30")) + 2
        import sys as _sys
        print(f"  wait {draft.name}: rate-limited, backoff {retry_after}s (retry {attempt+1}/4)",
              file=_sys.stderr)
        import time as _time
        _time.sleep(retry_after)
        resp = requests.put(
            put_url,
            headers={"X-API-Key": api_key},
            json={"content": render_skill_md(draft), "skip_limits": True},
            timeout=30,
        )
        attempt += 1
    if resp.status_code >= 400:
        raise RuntimeError(f"PUT {put_url} 失败: HTTP {resp.status_code}: {resp.text[:200]}")
    patch_url = f"{base_url.rstrip('/')}/v1/wiki/pages/{page_id}"
    patch_resp = requests.patch(
        patch_url,
        headers={"X-API-Key": api_key},
        json={"status": "superseded"},
        timeout=30,
    )
    if patch_resp.status_code >= 400:
        print(f"SKIP 提示：PATCH {patch_url} 置 superseded 失败 "
              f"(HTTP {patch_resp.status_code})——技能内容已推送成功，请手工处理原页状态。")
    else:
        print(f"已 PATCH 原页 {page_id} → superseded（原件保留不删）")


def render_report(report: Report) -> str:
    """Report → markdown 文本（分节 + 每项一行 + 摘标签建议清单）。"""
    lines: list[str] = [
        "# wiki skill 页迁移报告（ST-36 M4a）",
        "",
        "## 总览",
        "",
        f"- 总数: {report.total}",
        f"- 成功: {report.migrated}",
        f"- 跳过: {report.skipped}",
        f"- 失败: {report.failed}",
        "",
        "## 明细",
        "",
    ]
    if report.entries:
        lines.extend(report.entries)
    else:
        lines.append("（无候选页面）")
    lines.extend(["", "## 建议摘标签（不自动改）", ""])
    if report.untag_suggestions:
        lines.extend(report.untag_suggestions)
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wiki skill 页批量迁移（ST-36 M4a，默认 dry-run）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="wiki.db 路径（直连 SQLite）")
    src.add_argument("--api", help="SGME Server base_url（走 HTTP API）")
    ap.add_argument("--key-env", default="SGME_ADMIN_KEY", help="API Key 所在环境变量名（--api 模式必配）")
    ap.add_argument("--out-dir", default=None, help="生成的 SKILL.md 树输出目录（供人工检查）")
    ap.add_argument("--apply", action="store_true", help="真正执行迁移（--api 模式才生效；缺省 dry-run）")
    ap.add_argument("--report", default=None, help="markdown 报告输出路径（缺省打印 stdout）")
    args = ap.parse_args(argv)

    if args.api:
        key_name = args.key_env
        api_key = os.environ.get(key_name, "")
        if not api_key:
            print(f"错误：环境变量 {key_name} 未设置（--api 模式需要 X-API-Key）", file=sys.stderr)
            return 2
        base_url: str | None = args.api
    else:
        api_key = None
        base_url = None

    # 拉取页面
    if args.db:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"错误：数据库不存在: {db_path}", file=sys.stderr)
            return 2
        pages = load_pages_from_db(db_path)
    else:
        assert base_url is not None
        pages = load_pages_from_api(base_url, api_key or "")

    # API 列表响应可能不含全文，逐页补齐 content
    if args.api and any(not p.content for p in pages):
        assert base_url is not None
        for page in pages:
            if not page.content:
                page.content = fetch_page_content_api(base_url, api_key or "", page.page_id)

    out_dir = Path(args.out_dir) if args.out_dir else None
    report = migrate(
        pages=pages,
        out_dir=out_dir,
        apply_remote=args.apply and bool(args.api),
        base_url=base_url,
        api_key=api_key,
    )
    text = render_report(report)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
        print(f"报告已写入: {report_path}")
    print(text)
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
