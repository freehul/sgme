# -*- coding: utf-8 -*-
"""sgme/wiki/routes.py：wiki 知识库 HTTP 端点（v0.7 §10.2）。

扩展模块路由：wiki.enabled=true 时由 server/app.py 挂载。
鉴权：读/写均 require_agent_key（知识库是 Agent 可写资产，与记忆同权限级）。

端点：
- GET  /v1/wiki/pages                列表（category 过滤 + 分页）
- GET  /v1/wiki/pages/{page_id}      详情 JSON（?view=html → 渲染 HTML）
- GET  /v1/wiki/pages/{page_id}/export  自包含 HTML 导出
- GET  /v1/wiki/search               搜索（wiki_fts BM25 + LIKE 兜底）
- GET  /v1/wiki/raw/{hash}           原件下载
- POST /v1/wiki/ingest               提交提炼任务（对接 refinery，PR-11 后启用）
- GET  /v1/wiki/ingest/{task_id}     任务进度（PR-11 后启用）
"""
from __future__ import annotations

import html
import threading
import uuid
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from sgme.server.app import api_error, require_agent_key
from sgme.wiki import fts as wiki_fts_mod
from sgme.data import db as db_mod
from sgme.data import ingest_dao
from sgme.data import wiki_dao

router = APIRouter()

# T-13：ingest 任务持久化——原 `_TASKS` 内存字典 → SQLite ingest_tasks 表（wiki.db）。
# 任务创建/状态流转/查询全走 ingest_dao，进程重启后 queued/running 任务可恢复。
# `_RECOVERED` 为进程内「启动恢复」惰性开关：服务重启后首次触碰 ingest API 时
# 执行一次恢复（数据模型语义：running → error 标记中断 / queued 保留可重跑）。
_RECOVERED = False
_RECOVER_LOCK = threading.Lock()


def _recover_tasks_if_needed(conn: sqlite3.Connection) -> None:
    """进程内启动恢复（惰性触发，幂等）：首次调用 ingest API 时执行一次。

    T-13 语义（数据模型 §二 ingest_tasks「启动时恢复」）：status IN (queued, running)
    的任务置回 queued（可重跑）或 error（标记中断），由守护重试策略决定。
    恢复动作本身幂等，重复执行无副作用。
    """
    global _RECOVERED
    if _RECOVERED:
        return
    with _RECOVER_LOCK:
        if _RECOVERED:
            return
        ingest_dao.recover_interrupted_tasks(conn)
        _RECOVERED = True


class WikiIngestRequest(BaseModel):
    """ingest 请求：source_type + 三选一来源 + 可选元数据。"""

    source_type: str = "text"  # text / file / url
    content: str | None = None
    path: str | None = None
    url: str | None = None
    title: str | None = None
    category: str | None = None


class WikiPageUpdateRequest(BaseModel):
    """PATCH 请求（W3）：按 id 精确更新/追加，append 默认追加（ADD-only + hash 去重）。"""

    content: str
    append: bool = True
    title: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    author: str | None = None
    status: str | None = None  # PR-7：显式置 superseded（M4a 迁移原页归档；仅 active/superseded 两值）


class WikiEvolveRequest(BaseModel):
    """自进化触发请求（W4 方案 v0.3 §5.4）。"""

    session_key: str | None = None   # 指定会话；缺省扫未处理会话
    limit: int = 5                   # 缺省模式最大处理会话数
    min_rounds: int = 5              # 费用门禁：会话消息块下限


class WikiPageCreateRequest(BaseModel):
    """直接写入请求（T-55，不走提炼通道）：原样入库。"""

    title: str
    content: str
    category: str | None = None
    tags: list[str] | None = None
    source_type: str = "text"  # text / file / url
    source_url: str | None = None
    source_file: str | None = None
    description: str | None = None  # L1 摘要（描述即索引，W3 打通 create 链路）
    author: str | None = None
    status: str | None = None
    supersedes: str | None = None

# ---------- 渲染（最小安全实现，不引第三方） ----------


def render_markdown_simple(text: str) -> str:
    """极简 markdown → HTML：转义 + 代码块 + 标题 + 段落。

    定位：浏览/导出的可读性兜底，不追求完整 markdown 规范（v0.7 §10.1
    「浏览时实时渲染」的最小实现；复杂渲染归前端 WebUI）。
    行扫描状态机：``` 围栏间内容整体进 <pre>，不被段落拆分。
    """
    if not text:
        return ""
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = stripped[3:].strip()
            else:
                in_code = False
                code = html.escape("\n".join(code_buf))
                out.append(
                    f'<pre><code class="language-{html.escape(code_lang)}">{code}</code></pre>'
                )
                code_buf = []
            continue
        if in_code:
            code_buf.append(raw_line)
            continue
        line = html.escape(raw_line)
        if line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.strip() == "":
            out.append("<p></p>")
        else:
            out.append(f"<p>{line}</p>")
    if in_code:  # 未闭合围栏：兜底输出
        code = html.escape("\n".join(code_buf))
        out.append(f'<pre><code class="language-{html.escape(code_lang)}">{code}</code></pre>')
    return "".join(out)


def build_page_html(page: dict) -> str:
    """完整自包含 HTML 文档（导出/渲染共用）。"""
    title = html.escape(page.get("title") or page.get("page_id") or "wiki")
    body = render_markdown_simple(page.get("content") or "")
    tags = page.get("tags") or []
    tag_html = "".join(f'<span class="tag">{html.escape(str(t))}</span>' for t in tags)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; line-height: 1.7; }}
 h1,h2,h3 {{ margin-top: 1.6em; }}
 pre {{ background: #f6f8fa; padding: 1em; border-radius: 6px; overflow-x: auto; }}
 .meta {{ color: #666; font-size: .9em; margin-bottom: 1.5em; }}
 .tag {{ display: inline-block; background: #eef; padding: 2px 8px; border-radius: 10px; margin-right: 6px; font-size: .85em; }}
</style></head><body>
<h1>{title}</h1>
<div class="meta">category: {html.escape(str(page.get('category') or ''))} | updated: {html.escape(str(page.get('updated_at') or ''))}</div>
<div class="tags">{tag_html}</div>
{body}
</body></html>"""


# ---------- 端点 ----------

@router.get("/v1/wiki/pages")
def list_wiki_pages(
    request: Request,
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(require_agent_key),
):
    """wiki 页面列表（updated_at 降序；category 可选过滤）。"""
    conn: sqlite3.Connection = request.app.state.wiki_conn
    pages = wiki_dao.list_pages(conn, category=category, limit=limit, offset=offset)
    return {
        "pages": pages,
        "total": wiki_dao.count_pages(conn),
        "limit": limit,
        "offset": offset,
    }


@router.get("/v1/wiki/pages/{page_id}")
def get_wiki_page(
    page_id: str,
    request: Request,
    view: str | None = Query(default=None),
    _: str = Depends(require_agent_key),
):
    """详情：默认 JSON；?view=html → 渲染 HTML。"""
    conn: sqlite3.Connection = request.app.state.wiki_conn
    page = wiki_dao.get_page(conn, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "ERR_NOT_FOUND", "message": f"页面不存在: {page_id}"}})
    if view == "html":
        return HTMLResponse(build_page_html(page))
    # 关联页面（2026-08-18 自动关联：wiki_auto_link 建链 + 本端点展示）
    links = wiki_dao.list_links(conn, page_id)
    related = []
    for lk in links:
        other_id = lk["target_id"] if lk["source_id"] == page_id else lk["source_id"]
        other = wiki_dao.get_page(conn, other_id)
        if other:
            related.append({
                "page_id": other["page_id"],
                "title": other.get("title") or other["page_id"],
                "rel_type": lk["rel_type"],
            })
    page["links"] = related
    return page


@router.get("/v1/wiki/pages/{page_id}/export")
def export_wiki_page(
    page_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """导出自包含 HTML（下载附件）。"""
    conn: sqlite3.Connection = request.app.state.wiki_conn
    page = wiki_dao.get_page(conn, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "ERR_NOT_FOUND", "message": f"页面不存在: {page_id}"}})
    doc = build_page_html(page)
    filename = f"wiki_{page_id}.html"
    return HTMLResponse(
        content=doc,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v1/wiki/search")
def search_wiki(
    request: Request,
    q: str = Query(default="", min_length=0),
    limit: int = Query(default=10, ge=1, le=50),
    _: str = Depends(require_agent_key),
):
    """wiki 检索：wiki_fts BM25 + LIKE 兜底（空 query 返回空结果）。"""
    if not q.strip():
        return {"results": []}
    conn: sqlite3.Connection = request.app.state.wiki_conn
    results = wiki_fts_mod.search_wiki_fts(conn, q, limit=limit)
    return {"results": results}


@router.post("/v1/wiki/evolve/trigger")
def evolve_trigger_endpoint(
    payload: WikiEvolveRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """自进化触发（W4）：会话 → 经验 → 写回 wiki 手册。

    流程：费用门禁（消息块 ≥ min_rounds）→ LLM 提炼（结构化 JSON）
    → 规则闸门 → 写入（append 踩坑记录 / create 新手册页）→ 审计。
    """
    from sgme.operations.evolve import evolve_trigger as evolve_operation

    conn: sqlite3.Connection = request.app.state.wiki_conn
    session_conn: sqlite3.Connection = request.app.state.session_conn
    data_dir = request.app.state.cfg.get("paths", {}).get("data_dir")
    # 传 llm 段（含顶层 chains）：降级链 call_with_fallback 读 cfg["chains"]，完整 cfg 的 chains 在 cfg["llm"] 下
    result = evolve_operation(
        conn, session_conn, request.app.state.cfg["llm"],
        session_key=payload.session_key, limit=payload.limit,
        min_rounds=payload.min_rounds, data_dir=data_dir,
    )
    if not result.ok:
        raise api_error(result.error_code or "ERR_INTERNAL", result.message or "自进化失败")
    return result.data


@router.patch("/v1/wiki/pages/{page_id}")
def update_wiki_page(
    page_id: str,
    payload: WikiPageUpdateRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """按 page_id 精确更新/追加（自进化写回主通道，W3 方案 v0.3 §5.3）。

    append=true（默认）：content 追加到现有正文末尾，带「来源+hash」标记，
    entry hash 已存在则 noop（幂等）；description 默认不动。
    PR-7：status="superseded" 显式置原页归档（M4a 迁移收尾），优先于 content 更新执行。
    """
    from sgme.data import wiki_dao
    from sgme.operations.wiki import update_page as update_page_operation

    conn: sqlite3.Connection = request.app.state.wiki_conn
    # PR-7：显式 superseded 归档（M4a）——在内容更新前执行，独立生效
    if payload.status == "superseded":
        wiki_dao.mark_superseded(conn, page_id, supersedes_by=page_id)
        return {"page_id": page_id, "status": "superseded"}
    result = update_page_operation(
        conn, page_id,
        content=payload.content, append=payload.append,
        title=payload.title, category=payload.category, tags=payload.tags,
        description=payload.description, author=payload.author,
    )
    if not result.ok:
        raise api_error(result.error_code or "ERR_INTERNAL", result.message or "更新失败")
    return result.data


@router.post("/v1/wiki/pages")
def create_wiki_page(
    payload: WikiPageCreateRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """直接写入 wiki 页面（T-55：原样入库，不走 LLM 提炼；幂等 upsert）。

    与 POST /v1/wiki/ingest 并列——ingest 走 refinery 提炼改写，本端点原样写入。
    page_id 由「标题 slug + 内容哈希」自动生成；同 title+content 重复提交 →
    命中同一 page_id 更新（status=updated），不重复建页。
    写入后立即可被 /v1/wiki/search 检索（FTS 触发器 + 幂等 init 兜底）。
    """
    from sgme.operations.wiki import create_page as create_page_operation

    conn: sqlite3.Connection = request.app.state.wiki_conn
    result = create_page_operation(
        conn,
        title=payload.title, content=payload.content,
        category=payload.category, tags=payload.tags,
        source_type=payload.source_type,
        source_url=payload.source_url, source_file=payload.source_file,
        description=payload.description, author=payload.author,
        status=payload.status, supersedes=payload.supersedes,
    )
    if not result.ok:
        raise api_error(result.error_code or "ERR_INTERNAL", result.message or "写入失败")
    return result.data


# ---------- ingest（对接 refinery，v0.7 §9/§10.2；T-13 任务落库 ingest_tasks） ----------

@router.post("/v1/wiki/ingest")
def ingest_wiki_source(
    payload: WikiIngestRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """提交知识提炼任务：text/file/url → refinery → wiki_pages。

    同步排队 + 后台线程执行（LLM 提取可能耗时数十秒，不阻塞 HTTP）。
    任务创建即落库 ingest_tasks（重启后状态可恢复）；进度查询 GET /v1/wiki/ingest/{task_id}。
    """
    if payload.source_type not in ("text", "file", "url"):
        raise api_error("ERR_INVALID_ARGS", f"source_type 必须是 text/file/url，收到: {payload.source_type}")
    source = payload.content or payload.path or payload.url
    if not source:
        raise api_error("ERR_INVALID_ARGS", "需提供 content/path/url 之一")

    conn = request.app.state.wiki_conn
    _recover_tasks_if_needed(conn)

    task_id = uuid.uuid4().hex[:12]
    ingest_dao.create_task(
        conn,
        task_id,
        source_type=payload.source_type,
        source_ref=source,
        title=payload.title,
    )

    def run() -> None:
        conn = None
        try:
            # 线程内新开连接（sqlite 连接默认禁跨线程）
            data_dir = request.app.state.cfg.get("paths", {}).get("data_dir")
            conn = db_mod.connect_wiki(data_dir) if data_dir else db_mod.connect_wiki()

            from sgme.refinery import refine
            from sgme.refinery.output import to_wiki_page

            result = refine(source)
            if not result.ok:
                ingest_dao.update_status(conn, task_id, status="error", error=result.error)
                return
            page = to_wiki_page(result, source_url=payload.url, source_file=payload.path)
            if payload.title:
                page["title"] = payload.title
            if payload.category:
                page["category"] = payload.category
            # content_seg/updated_at 由 insert_page 内部计算，剔除
            page_row = {k: v for k, v in page.items() if k not in ("content_seg", "updated_at")}
            wiki_dao.insert_page(conn, **page_row)
            wiki_fts_mod.init_wiki_fts(conn)
            ingest_dao.update_status(conn, task_id, status="done", page_id=page["page_id"])
        except NotImplementedError as e:
            if conn is not None:
                ingest_dao.update_status(conn, task_id, status="error", error=f"暂不支持的来源类型: {e}")
        except Exception as e:
            if conn is not None:
                ingest_dao.update_status(conn, task_id, status="error", error=str(e))
        finally:
            if conn is not None:
                db_mod.close(conn)

    threading.Thread(target=run, daemon=True).start()
    return {"task_id": task_id, "status": "queued"}


@router.get("/v1/wiki/ingest/{task_id}")
def get_wiki_ingest_status(
    task_id: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """查询 ingest 任务进度（T-13：读库；result_page_id 列映射为对外 page_id 字段）。"""
    conn = request.app.state.wiki_conn
    _recover_tasks_if_needed(conn)
    task = ingest_dao.get_task(conn, task_id)
    if task is None:
        raise api_error("ERR_NOT_FOUND", f"任务不存在: {task_id}")
    # 契约兼容：表列 result_page_id ↔ 端点字段 page_id（既有端点契约零破坏）
    if task.get("result_page_id"):
        task["page_id"] = task["result_page_id"]
    task.pop("result_page_id", None)
    return task


@router.get("/v1/wiki/raw/{file_hash}")
def get_wiki_raw(
    file_hash: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """下载原件（wiki/raw/ 归档目录按 hash 查找，原件永不删除）。"""
    data_dir: Path = request.app.state.cfg.get("paths", {}).get("data_dir")
    raw_root = Path(data_dir) / "wiki_raw" if data_dir else Path("data") / "wiki_raw"
    if not raw_root.is_dir():
        raise HTTPException(status_code=404, detail={"error": {"code": "ERR_NOT_FOUND", "message": "原件目录不存在"}})
    hits = list(raw_root.rglob(f"{file_hash}.*"))
    if not hits:
        raise HTTPException(status_code=404, detail={"error": {"code": "ERR_NOT_FOUND", "message": f"原件不存在: {file_hash}"}})
    return FileResponse(hits[0])
