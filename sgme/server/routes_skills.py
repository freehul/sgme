# -*- coding: utf-8 -*-
"""sgme/server/routes_skills.py：技能管理读侧 HTTP 端点（ST-36 M2 四级披露）。

扩展模块路由：skills.enabled=true 时由 server/app.py 挂载（镜像 care 模块开关
方式——禁用时整个 router 不注册，/v1/skills* 自然 404，核心零影响）。
鉴权：全部 require_agent_key（读侧是 agent 消费面，与记忆同权限级）。

与 /v1/admin/skills（routes_admin.py，skills_hub 写仓 CRUD + admin key）的边界：
- 写侧治理（put/remove/sync）归 admin 端点（另一工作流）；
- 本文件只做**读侧披露**——L0/L1/L2/L3 四级 + 统一搜索 skills 层的数据源端点。

端点：
- GET  /v1/skills                        L0 索引列表（budget 截断）
- GET  /v1/skills/{name}/digest          L1 摘要（frontmatter+骨架+uses）
- GET  /v1/skills/{name}?section=        L2 全文 / 节选
- POST /v1/skills/{name}/materialize     L3 字节保真落盘 {dest_dir}（**落盘在服务端**）
- GET  /v1/skills/{name}/package         跨机完整包：流式 tar.gz + MANIFEST.sha256（T-281）
- GET  /v1/skills/{name}/files/{relpath} 跨机单文件原始字节 + X-File-Sha256（T-281）

业务逻辑全在 ``sgme.operations.skills``，本模块只做协议翻译（入口禁止写业务）；
包通道的 tar.gz 组包/流式、HTTP 头与 304 判定属**协议层**（枚举/哈希/配额仍在
operations 层，先算哈希再开流）。
索引缓存：app.state.skills_index 惰性建 BM25 索引对象 + rebuild_if_stale 复用
（百条规模重建毫秒级，缓存只为省重复扫描 IO；记录集变化自动失效重建）。
"""
from __future__ import annotations

import io
import logging
import sqlite3
import tarfile

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from sgme.server.app import api_error, require_agent_key, run_operation

logger = logging.getLogger("sgme.server.skills")

router = APIRouter()


class MaterializeRequest(BaseModel):
    """L3 物化请求体：目标目录（**服务端视角**路径——物化落盘在服务端文件系统；
    跨机调用（agent 在本地、SGME 在远端）改用 GET /v1/skills/{name}/package
    下载完整包，或 skill_get 取正文自行写盘）。"""

    dest_dir: str


def _ensure_index(request: Request):
    """惰性建/复用 app.state.skills_bm25 索引对象（rebuild_if_stale 判过期）。

    - 记录集现扫一次（git 工作区 ∪ wiki skill 页），与上次内容 SHA/数量一致
      则复用既有 SkillsBm25 对象，变化才整表重建（百条规模毫秒级）；
    - L0 列表端点消费 len(records)；L1/L2/L3 仍走 operations 层现查
      （单技能取回无需全量索引）。
    """
    from sgme.skills.bm25 import rebuild_if_stale
    from sgme.skills.config import parse_skills_config
    from sgme.skills.indexer import index_all

    cfg = request.app.state.cfg
    sc = parse_skills_config(cfg)
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    records = index_all(sc.source_dirs, wiki_conn)[: sc.budget]
    cached = getattr(request.app.state, "skills_bm25", None)
    index = rebuild_if_stale(cached, records)
    request.app.state.skills_bm25 = index  # 写回（新索引或原对象）
    return records


# ---------- GET /v1/skills （L0 索引列表） ----------

@router.get("/v1/skills")
def list_skills(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1),
    _: str = Depends(require_agent_key),
):
    """L0 索引列表：name/description/category/tags（受 budget 截断）。

    v0.7 规范：业务下沉 operations.skills，本函数只做协议翻译。
    """
    from sgme.operations.skills import list_skills as list_skills_operation

    cfg = request.app.state.cfg
    _ensure_index(request)  # 惰性建/复用 BM25 索引缓存（app.state.skills_bm25）
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    # T-112：库可用时走 skills.db（结构化查询 + 免全量扫描），None 时操作层回退内存索引
    skills_conn: sqlite3.Connection | None = getattr(request.app.state, "skills_conn", None)
    return run_operation(list_skills_operation, cfg, wiki_conn,
                        offset=offset, limit=limit, skills_conn=skills_conn)


# ---------- GET /v1/skills/coldstart （冷启动包，须先于 /{name} 注册） ----------

@router.get("/v1/skills/coldstart")
def skills_coldstart(
    request: Request,
    _: str = Depends(require_agent_key),
):
    """冷启动包：索引全量 + 热集（pattern=auto）全文 + SGME 操作手册。

    新 agent 一次拉取即刻可用（设计 §三「冷启动包」）；须注册在 ``/{name}``
    动态路由之前，否则 ``coldstart`` 会被当成技能名命中 L2 端点。
    """
    from sgme.operations.skills import cold_start as cold_start_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    return run_operation(cold_start_operation, cfg, wiki_conn)


def _record_skill_usage(
    request: Request, layer: str, skill: str, note: str | None = None
) -> None:
    """T-174：技能消费记录（静默旁路；caller 由 UsageMiddleware 写入 scope state）。

    ``search`` / ``materialize`` 归本文件记（需要响应结果 / 请求体语义），
    ``digest`` / ``get`` 由中间件按路由模板记——同一次调用只落一条。
    """
    try:
        from sgme.operations.skill_usage import caller_from_state, record_skill_usage

        conn = getattr(request.app.state, "mem_conn", None)
        if conn is None:
            return
        record_skill_usage(conn, layer, skill, caller_from_state(request.state), note=note)
    except Exception:
        pass  # 统计是旁路：任何失败不得影响请求


# ---------- GET /v1/skills/search （技能检索，须先于 /{name} 注册） ----------

@router.get("/v1/skills/search")
def skills_search(
    request: Request,
    q: str = Query(..., min_length=1, description="检索词"),
    limit: int = Query(default=10, ge=1, le=20),
    _: str = Depends(require_agent_key),
):
    """技能检索（T-163 对齐补齐）：BM25+向量融合 → [{name, score, source, ...}]。

    与 MCP ``skill_search`` 同一实现（``operations.skills.search_skills``，
    向量路不可达自动降级纯 BM25）；须注册在 ``/{name}`` 动态路由之前，
    否则 ``search`` 会被当成技能名命中 L2 端点（同 coldstart 先例）。
    """
    from sgme.operations.skills import search_skills as search_skills_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    skills_conn: sqlite3.Connection | None = getattr(request.app.state, "skills_conn", None)
    items = search_skills_operation(q, cfg, wiki_conn, limit=limit, skills_conn=skills_conn)
    # T-174：检索层消费记录（含命中数/首条命中；中间件不记 search——它拿不到结果）
    try:
        from sgme.operations.skill_usage import search_note

        top = items[0].get("name") if items else None
        _record_skill_usage(request, "search", "-", note=search_note(q, len(items), top))
    except Exception:
        pass  # 统计是旁路
    return {"query": q, "count": len(items), "items": items}

# ---------- GET /v1/skills/{name}/digest （L1 摘要） ----------

@router.get("/v1/skills/{name}/digest")
def skill_digest(
    name: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """L1 摘要：frontmatter 字段 + 正文骨架 + uses 清单。审核媒介层。"""
    from sgme.operations.skills import skill_digest as digest_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    data = run_operation(digest_operation, cfg, wiki_conn, name=name)
    # L1 附带可用小节骨架的便捷提示（sections 已含标题行，无需额外字段）
    return data


# ---------- GET /v1/skills/{name}?section= （L2 全文/节选） ----------

@router.get("/v1/skills/{name}")
def skill_get(
    name: str,
    request: Request,
    section: str | None = None,
    _: str = Depends(require_agent_key),
):
    """L2 全文：正文全文注入；section 给定时截取该节（省 token）。"""
    from sgme.operations.skills import skill_get as get_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    return run_operation(get_operation, cfg, wiki_conn, name=name, section=section)


# ---------- POST /v1/skills/{name}/materialize （L3 物化落盘） ----------

@router.post("/v1/skills/{name}/materialize")
def materialize_skill(
    name: str,
    payload: MaterializeRequest,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """L3 物化：SKILL.md 原文件字节写盘 dest_dir/<name>/SKILL.md。

    字节保真铁律：不走 LLM 转写；成功记遥测日志一条（name/sha/ts）。
    dest_dir 缺失 → pydantic 422（框架层把关，镜像 idea_add 必填语义）。
    T-232：落盘发生在**服务端文件系统**（dest_dir/返回 path 均为服务端视角，响应带
    landing_side="server"）；非 Windows 服务端收到盘符 dest_dir → 400 明确报错，
    跨机取完整包改用 GET /v1/skills/{name}/package（tar.gz + MANIFEST.sha256），
    或 skill_get 取正文自行写盘。
    """
    from sgme.operations.skill_usage import dest_kind
    from sgme.operations.skills import materialize as materialize_operation

    # T-174：物化层消费记录（记调用事实与目标目录**类型**，不记真实路径）。
    # Windows 盘符路径被跨机传进来时，Linux 服务端会当相对路径造出 /app/D:/… 垃圾目录
    # ——note=dest=windows 就是该缺陷的可观测信号。
    _record_skill_usage(
        request, "materialize", name, note=f"dest={dest_kind(payload.dest_dir)}"
    )

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    return run_operation(
        materialize_operation, cfg, wiki_conn,
        name=name, dest_dir=payload.dest_dir,
    )


# ---------- GET /v1/skills/{name}/package （跨机完整包，T-281） ----------


class _ChunkBuf:
    """tarfile 写入适配器：把压缩字节攒成块供生成器 drain 出去（真流式、零新增依赖）。"""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data) -> int:  # gzip/tarfile 只依赖 write()
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:  # gzip 结束流程可能调用；空实现
        return None

    def drain(self) -> list[bytes]:
        chunks, self._chunks = self._chunks, []
        return chunks


def _stream_package_archive(name: str, members: list[dict], manifest: str):
    """tar.gz 字节流：归档根 ``MANIFEST.sha256`` + ``<name>/`` 下逐成员（不整包驻留内存）。

    组包前提：调用方已**先枚举 + 算哈希**（本生成器只搬运字节）——中途失败只能中断流
    （响应头已随 ``http.response.start`` 送出，X-Skill-* 无法回头改），客户端以 MANIFEST
    兜底校验；失败记 WARNING（anomaly 遥测）后重抛，不吞异常。
    """
    from sgme.operations.skills import MANIFEST_NAME

    buf = _ChunkBuf()
    # tarfile 流式写（"w|gz"）只依赖 fileobj.write()——类型桩不认 _ChunkBuf（运行时已实测）
    tar = tarfile.open(fileobj=buf, mode="w|gz")  # type: ignore[arg-type]
    try:
        # MANIFEST 在归档根、与 <name>/ 平级（技能目录内不混入通道附加件）
        body = manifest.encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(body)
        info.mode = 0o644
        info.mtime = 0
        tar.addfile(info, io.BytesIO(body))
        yield from buf.drain()

        for m in members:
            arcname = f"{name}/{m['path']}"
            data = m.get("data")
            src = m.get("source_path")
            if data is None and src is not None:
                info = tar.gettarinfo(str(src), arcname=arcname)
                # 数据卫生：归属信息归零（uid/gid/uname/gname 不落服务端标识）
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(src, "rb") as fh:
                    tar.addfile(info, fh)
            else:
                blob = data or b""
                info = tarfile.TarInfo(arcname)
                info.size = len(blob)
                info.mode = int(m.get("mode") or 0o644)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(blob))
            yield from buf.drain()

        tar.close()  # 结束块 + gzip 尾
        yield from buf.drain()
    except Exception as e:  # 断流语义：已发的头改不了，客户端以清单兜底
        logger.warning("skill package 流中断（客户端以 MANIFEST 兜底校验）: %s", e)
        raise


def _etag_hit(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match 命中判定（容忍引号 / W/ 前缀 / 逗号列表；``*`` 恒命中）。"""
    if not if_none_match:
        return False
    for cand in (t.strip() for t in if_none_match.split(",")):
        if not cand:
            continue
        if cand == "*":
            return True
        if cand.removeprefix("W/").strip('"') == etag.strip('"'):
            return True
    return False


@router.get("/v1/skills/{name}/package")
def skill_package(
    name: str,
    request: Request,
    fmt: str = Query(default="tar.gz", alias="format", description="归档格式（唯一正式值 tar.gz）"),
    _: str = Depends(require_agent_key),
):
    """跨机**完整包**下载（T-281）：流式 tar.gz——归档根 ``MANIFEST.sha256`` + ``<name>/`` 逐文件。

    - 文件集与 materialize 同源同过滤（``operations.iter_package_files``）：SKILL.md +
      随附文件（scripts/ install.py locales/ package/ 等）；wiki 源 / 源文件缺失 /
      内置《技能检索协议》→ 单文件包（合法，消除「读得到正文、取不到包」的不一致）；
    - **先枚举算哈希再开流**：``X-Skill-Sha256``（canonical manifest 的 sha256）与
      ``X-Skill-Files``（``<name>/`` 下文件数，不含 MANIFEST）随响应头先送出；
    - ``ETag`` = 聚合哈希；``If-None-Match`` 命中 → 304（不重复计一次下载）；
    - ``format=zip`` 为保留参数（浏览器解包面未实现）→ 400 引导文案；
    - 未知技能 404；超配 → 400（``skills.package_quota.*``）；遥测 layer=package
      （``note="format=tar.gz;files=N"``——只记事实与数量，不记路径）。
    """
    from sgme.operations.skills import package_skill as package_operation

    if fmt != "tar.gz":
        raise api_error(
            "ERR_INVALID_ARGS",
            f"包格式仅支持 tar.gz（当前: {fmt!r}）；zip 为保留参数——"
            "下载 tar.gz 后用宿主侧 tar 解包即可（Windows 10+ 自带 bsdtar）",
        )

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    data = run_operation(package_operation, cfg, wiki_conn, name=name)

    sha256 = str(data["sha256"])
    count = int(data["count"])
    etag = f'"{sha256}"'
    if _etag_hit(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "X-Skill-Sha256": sha256, "X-Skill-Files": str(count)},
        )

    # T-174/T-281：消费记录一条（包下载；note 只记格式与文件数——数据卫生）
    _record_skill_usage(
        request, "package", str(data["name"]), note=f"format=tar.gz;files={count}"
    )

    return StreamingResponse(
        _stream_package_archive(str(data["name"]), list(data["members"]), str(data["manifest"])),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{data["name"]}.tar.gz"',
            "ETag": etag,
            "X-Skill-Sha256": sha256,
            "X-Skill-Files": str(count),
        },
    )


# ---------- GET /v1/skills/{name}/files/{relpath} （跨机单文件读，T-281） ----------


def _file_ext_note(relpath: str) -> str:
    """单文件读 note 的扩展名（**只记类型，不记真实路径**——数据卫生铁律）。"""
    leaf = str(relpath or "").rsplit("/", 1)[-1]
    dot = leaf.rfind(".")
    return leaf[dot:].lower() if dot > 0 else "-"


@router.get("/v1/skills/{name}/files/{relpath:path}")
def skill_file(
    name: str,
    relpath: str,
    request: Request,
    _: str = Depends(require_agent_key),
):
    """跨机**单文件读**（T-281）：包成员原始字节（``application/octet-stream``）+ ``X-File-Sha256``。

    - 安全 = 归一内核（禁绝对路径 / 盘符 / ``..``）+ **成员命中制**（必须命中包文件集）：
      形态非法 → 400；技能不存在 / 成员不存在（含 junk、符号链接、白名单外成员）→ 404；
    - 顶层成员（``install.py`` / ``locales/**`` / ``package/**`` 等）**可读**——读宽写严，
      写侧首段白名单不适用；
    - 用途：小文件精确取用、客户端校验、未来增量同步原料；遥测 layer=package
      （``note="file;<扩展名>"``，不记真实路径）。
    """
    from sgme.operations.skills import read_skill_file as read_file_operation

    cfg = request.app.state.cfg
    wiki_conn: sqlite3.Connection | None = getattr(request.app.state, "wiki_conn", None)
    data = run_operation(read_file_operation, cfg, wiki_conn, name=name, relpath=relpath)

    _record_skill_usage(
        request, "package", str(data["name"]),
        note=f"file;{_file_ext_note(str(data['path']))}",
    )
    return Response(
        content=data["content"],
        media_type="application/octet-stream",
        headers={"X-File-Sha256": str(data["sha256"])},
    )
