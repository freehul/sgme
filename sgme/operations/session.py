"""operations/session.py：L0 原文读取操作（0.8 ST-9，契约 §4.7 / §5.6.2）。

职责：按 `file_id` 取回 L0 会话原文全文 —— **纯只读，零副作用**。

数据来源（两处，缺一不可）
--------------------------
1. **元数据**：`session.db` 的 `raw_files` 表（v0.7 三库拆分后 raw_files 归 session.db，
   勿再查 wiki.db）→ 提供 `session_key` / `agent_id` / `path`。
2. **正文**：磁盘上的 `raw/<subdir>/<file_id>.md` → 提供 `content`。

两个入口共用（重要）
--------------------
本操作同时服务契约 §4.7 的 **agent 版** `GET /v1/sessions/{file_id}` 与 §5.6.2 的
**admin 版** `GET /v1/admin/sessions/{file_id}`——两者响应体同构
（`{file_id, session_key, agent_id, content}`），**区别只在入口层的鉴权依赖**
（`require_agent_key` vs `require_admin_key`）。因此业务逻辑只此一份，
admin 路由直接复用本函数即可，不要另抄一遍读盘逻辑。

因两端形态完全一致，按 operations 层约定**不写 http_payload / mcp_payload 投影函数**
（详见 `operations/__init__.py`「新增一个操作模块的标准姿势」）。

依赖方向：只依赖 `data.session_dao`（只读）与标准库，不认识 HTTP/FastAPI。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

from sgme.data import session_dao
from sgme.operations.errors import ERR_INTERNAL, ERR_NOT_FOUND, OperationResult

# raw/ 下的合法子目录（与 raw/store.py::_file_path 的 source_type 映射保持一致）。
# raw_files 表**没有** source_type 列，故子目录只能从 path 列反推，此集合用于校验反推结果。
_ALLOWED_SUBDIRS: frozenset[str] = frozenset({"sessions", "uploads", "external"})

# 反推失败时的兜底子目录（绝大多数 L0 文件是会话原文）
_DEFAULT_SUBDIR: str = "sessions"


def _subdir_from_stored_path(stored_path: str | None) -> str:
    """从 raw_files.path 反推 raw/ 下的子目录名。

    `raw_files.path` 由 `raw/store.py::relative_path()` 写入，形如
    ``raw/sessions/<file_id>.md``（相对项目根，POSIX 分隔符；Windows 上
    落库前已统一替换反斜杠）。此处只取**父目录名**，不整段信任该路径——
    因为测试与迁移场景下 RAW_DIR 可能已不是项目根下的 raw/，
    整段拼接会指向错误位置（详见 `_resolve_raw_path` 的说明）。

    Args:
        stored_path: raw_files.path 列的值，可为 None / 空串。

    Returns:
        子目录名；无法反推或不在白名单内时返回 ``"sessions"``。
    """
    if not stored_path:
        return _DEFAULT_SUBDIR
    # 统一分隔符后按 POSIX 语义解析（存量数据可能混有 Windows 反斜杠）
    parent_name = PurePosixPath(stored_path.replace("\\", "/")).parent.name
    if parent_name in _ALLOWED_SUBDIRS:
        return parent_name
    return _DEFAULT_SUBDIR


def _resolve_raw_path(raw_dir: Path, file_id: str, stored_path: str | None) -> Path | None:
    """定位 L0 原文文件的绝对路径。

    **刻意不直接用 raw_files.path 拼绝对路径**，而是 ``raw_dir / 子目录 / <file_id>.md``
    重新组装。理由：`path` 列存的是「相对项目根」的路径，一旦 RAW_DIR 被重定向
    （测试用 monkeypatch 指向 tmp_path、或部署时 raw/ 挂到别处），
    按项目根解析就会读到错误位置甚至越界。以 raw_dir 为**唯一根**重新组装，
    行为与 `raw/store.py::_file_path()` 一致（同样是 ``config.RAW_DIR / sub / f"{file_id}.md"``）。

    安全性：`file_id` 虽来自 URL path 参数，但调用方已先用它命中 raw_files 行
    （不存在即 404，不会走到这里）；此处仍做一次「结果必须落在 raw_dir 内」的
    越界校验作为纵深防御，防止脏数据 file_id（含 ``..`` / 分隔符）逃逸出 raw 根目录。

    Args:
        raw_dir: raw 根目录（由入口层传入 ``sgme_config.RAW_DIR``）。
        file_id: 文件 id（即 raw_files 主键）。
        stored_path: raw_files.path 列的值，用于反推子目录。

    Returns:
        文件绝对路径；越界（file_id 含路径穿越片段）时返回 None。
    """
    subdir = _subdir_from_stored_path(stored_path)
    candidate = (Path(raw_dir) / subdir / f"{file_id}.md").resolve()
    root = Path(raw_dir).resolve()
    # 纵深防御：拼装结果必须仍在 raw 根目录之内
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _read_text_preserving_newlines(path: Path) -> str:
    """读取 UTF-8 文本，**原样保留行尾**（不做换行符归一）。

    ``Path.read_text()`` 走通用换行模式，会把磁盘上的 ``\\r\\n`` 静默翻译成 ``\\n``，
    导致返回的 content 与磁盘文件不逐字节一致（Windows 上写入的 L0 文件会中招）。
    此处显式 ``newline=""`` 关闭翻译，保证「所见即所存」。

    Args:
        path: 文件绝对路径。

    Returns:
        文件全文（行尾与磁盘一致）。
    """
    with path.open("r", encoding="utf-8", newline="") as f:
        return f.read()


def get_raw_file_content(
    session_conn: sqlite3.Connection,
    file_id: str,
    raw_dir: Path | str,
) -> OperationResult:
    """读取单个 L0 会话原文（契约 §4.7 / §5.6.2）。

    纯只读：不写库、不改文件状态、不发信号。

    Args:
        session_conn: session.db 连接（raw_files 元数据来源）。
        file_id: L0 文件 id（raw_files 主键）。
        raw_dir: raw 根目录，由入口层显式传入 ``sgme_config.RAW_DIR``
            （operations 层不读全局配置，保持依赖显式化）。

    Returns:
        - 成功：``OperationResult(ok=True)``，data 为
          ``{"file_id", "session_key", "agent_id", "content"}``——
          键序即契约 §4.7 响应体顺序，勿调整。
        - `file_id` 在 raw_files 中不存在：``ok=False, ERR_NOT_FOUND``（→ HTTP 404）。
        - 索引存在但磁盘文件缺失/越界：``ok=False, ERR_INTERNAL``（→ HTTP 500）。
          按契约 §4.7「`file_id` 不存在 → 404；其余 → 500」的严格读法，
          这属于**索引与磁盘不一致的数据损坏**，而非「file_id 不存在」，
          故归 500 而不是 404——两者语义必须可区分，否则运维无法从状态码
          判断是「查错了 id」还是「原文丢了」。

        sqlite / OSError 等非预期异常**原样上抛**，由入口层全局异常处理器兜底
        （operations 层不加 catch-all，见 `operations/__init__.py` 隐性契约）。
    """
    row = session_dao.get_raw_file(session_conn, file_id)
    if row is None:
        return OperationResult.fail(ERR_NOT_FOUND, f"L0 原文不存在: {file_id}")

    path = _resolve_raw_path(Path(raw_dir), file_id, row.get("path"))
    if path is None:
        # file_id 含路径穿越片段（脏数据），拒绝读取
        return OperationResult.fail(ERR_INTERNAL, f"L0 原文路径非法: {file_id}")
    if not path.is_file():
        return OperationResult.fail(
            ERR_INTERNAL, f"L0 原文文件缺失（索引存在但磁盘无此文件）: {file_id}"
        )

    content = _read_text_preserving_newlines(path)
    return OperationResult.succeed({
        "file_id": file_id,
        "session_key": row.get("session_key"),
        "agent_id": row.get("agent_id"),
        "content": content,
    })
