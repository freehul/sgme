"""operations/skill_cache.py：技能落盘缓存的服务端接口（2.0 设计 D-59）。

客户端两级缓存，避免每次全量拉取技能目录：
  第 1 级（目录级短路）：比对 ``catalog_version``——目录指纹没变，直接用本地目录缓存；
  第 2 级（单技能比对）：比对 ``skill_manifest`` 的 ``package_sha256``——决定该技能
  是否需要重拉，拉到包后用同一哈希做完整性校验（两级缓存闭环）。

服务端契约（本模块即契约实现，入口层接线为 HTTP 端点，只做协议翻译）：
  catalog_version(conn) -> str              目录指纹，前 16 位十六进制；空表 ``empty``
  skill_manifest(conn, name) -> dict|None   单技能摘要（含 package_sha256）
  skill_package(conn, name) -> bytes|None   确定性 tar.gz（同内容产出同字节）

分层铁律（照抄 idea.py 样板）：本模块不认识协议——不 import fastapi、不知道 HTTP
状态码；依赖（conn）由入口层显式传入。三函数**只读**，不写库。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import tarfile

# 包内 SKILL.json 的字段集（skills 表列投影；固定 key 集 = 确定性的组成部分）
_META_COLUMNS: tuple[str, ...] = (
    "name",
    "sha256",
    "description",
    "category",
    "tags",
    "version",
    "updated_at",
)


def catalog_version(conn: sqlite3.Connection) -> str:
    """目录版本指纹：skills 全表 (name, sha256) 按 name 排序拼接后取 sha256 前 16 位。

    空表返回字面量 ``empty``（客户端语义：服务端无目录，清空本地缓存即可）。只读。
    """
    rows = conn.execute("SELECT name, sha256 FROM skills ORDER BY name").fetchall()
    if not rows:
        return "empty"
    payload = "".join(f"{row['name']}:{row['sha256']}\n" for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_package(row: sqlite3.Row, files: list[tuple[str, bytes]] | None = None) -> bytes:
    """单条 skills 行 → 确定性 tar.gz 字节（纯函数，只依赖行内容 + 附属文件）。

    确定性三要素（tests/test_skill_cache.py 逐项回归）：
      1. 成员按文件名排序（sorted）；
      2. tar 成员 mtime/uid/gid/uname/gname 全部归零；
      3. 元数据 JSON 用 sort_keys + 固定分隔符序列化；gzip 头 mtime=0
        （tarfile "w:gz" 会把当前时间写进 gzip 头，故先组 tar 再单独 gzip.compress）。

    ``files``（T-404 新增）：``[(rel_path, bytes), ...]`` 来自 skill_files 表。
    为空时包与 1.x 完全一致（向后兼容）；有值时按 rel_path 平铺进包，
    客户端落盘即为 ``<skill>/references/x.md`` 这样的原始布局。
    """
    content = row["content"]
    files = list(files or [])
    blob = content.encode("utf-8") if isinstance(content, str) else bytes(content)

    # tags 与 DAO 同规：JSON 串还原为列表（还原失败按逗号拆）
    tags = row["tags"]
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = [t for t in tags.split(",") if t]
    elif tags is None:
        tags = []

    meta = {k: row[k] for k in _META_COLUMNS}
    meta["tags"] = tags
    # 包清单：让客户端知道包内含哪些附属文件（不含正文 SKILL.md）
    meta["files"] = [{"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for p, d in files]
    meta_json = json.dumps(
        meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    buf = io.BytesIO()
    # format 显式钉死 PAX（Python 默认值可能随版本变化，钉死防跨版本字节漂移）
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        members = [("SKILL.json", meta_json), ("SKILL.md", blob)]
        members.extend(files)
        for arcname, data in sorted(members):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            # 数据卫生：归属信息归零（不落服务端用户/组标识）
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))

    return gzip.compress(buf.getvalue(), mtime=0)


_PACKAGE_COLUMNS = (
    "name, sha256, description, category, tags, version, content, content_len, updated_at"
)


def _load_files(conn: sqlite3.Connection, name: str) -> list[tuple[str, bytes]]:
    """读该技能的全部附属文件（skill_files 表）；表不存在时返回空。

    表缺失不报错是刻意的：老库（T-401 之前）没有 skill_files，此时技能包
    退化为「只有正文」的 1.x 形态，接口照常可用而不是 500。
    """
    try:
        rows = conn.execute(
            "SELECT rel_path, content FROM skill_files WHERE skill_name = ? ORDER BY rel_path",
            (name,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[tuple[str, bytes]] = []
    for r in rows:
        data = r["content"]
        out.append((r["rel_path"], data if isinstance(data, bytes) else bytes(data)))
    return out


def skill_manifest(conn: sqlite3.Connection, name: str) -> dict | None:
    """单技能缓存摘要；技能不存在返回 None。只读。

    ``package_sha256`` 取**确定性包字节**的 sha256，而非库里的行级 ``sha256`` 列，
    仅作**两级缓存的指纹**（客户端拿它决定是否重拉），**不可当传输字节的校验和**：

    - 它 ≠ ``GET /v1/skills/{name}/package`` 响应体（流式 tar.gz）字节的 sha256
      （2026-11-03 实测：同一技能三者互不相等）；
    - 它 ≠ 响应头 ``X-Skill-Sha256``（后者 = 包内 ``MANIFEST.sha256`` 的聚合哈希）；
    - 包内容完整性由包内 ``MANIFEST.sha256`` 逐成员校验（客户端侧），不靠本字段。

    行级 ``sha256`` 只锚定 content，覆盖不到元数据与附属文件的变化，故本字段
    对「DB 行 + 附属文件」做确定性打包后取哈希，充当缓存指纹。

    ``files``（T-404）：附属文件清单（path/sha256/size），不含 SKILL.md。
    """
    row = conn.execute(
        f"SELECT {_PACKAGE_COLUMNS} FROM skills WHERE name = ?",  # noqa: S608（列名常量，无注入面）
        (name,),
    ).fetchone()
    if row is None:
        return None
    files = _load_files(conn, name)
    return {
        "name": row["name"],
        "version": row["version"] or "",
        "package_sha256": hashlib.sha256(_build_package(row, files)).hexdigest(),
        "content_len": int(row["content_len"] or 0),
        "updated_at": row["updated_at"] or "",
        "files": [
            {"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
            for p, d in files
        ],
    }


def skill_file(conn: sqlite3.Connection, name: str, rel_path: str) -> bytes | None:
    """读单个附属文件原文（T-404）；不存在返回 None。"""
    if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
        return None
    try:
        row = conn.execute(
            "SELECT content FROM skill_files WHERE skill_name = ? AND rel_path = ?",
            (name, rel_path),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    data = row["content"]
    return data if isinstance(data, bytes) else bytes(data)


def skill_package(conn: sqlite3.Connection, name: str) -> bytes | None:
    """把该技能打成一个确定性 tar.gz（内存中）；技能不存在返回 None。只读。

    包内布局：``SKILL.md``（content 全文）+ ``SKILL.json``（元数据投影，含行级
    sha256 与附属文件清单）+ 各附属文件（按 rel_path 平铺，如 ``references/x.md``）。
    """
    row = conn.execute(
        f"SELECT {_PACKAGE_COLUMNS} FROM skills WHERE name = ?",  # noqa: S608（列名常量，无注入面）
        (name,),
    ).fetchone()
    if row is None:
        return None
    return _build_package(row, _load_files(conn, name))
