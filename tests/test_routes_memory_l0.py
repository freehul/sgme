"""tests/test_routes_memory_l0.py：L0 原文读取端点测试（0.8 ST-9，契约 §4.7）。

被测对象：
- HTTP ``GET /v1/sessions/{file_id}``（`routes_memory.get_session_raw`，Agent Key）
- operations ``sgme.operations.session.get_raw_file_content``

覆盖矩阵：
1. 正常读取：200 + 响应键集合/顺序符合契约 + content 为原文全文
2. content 与磁盘文件**逐字符一致**（含 CRLF 行尾保留、中文不损坏）
3. 真实 L0 文件（frontmatter + 消息块）读回后仍可被 raw_store 解析
4. `file_id` 不存在 → 404 `ERR_NOT_FOUND`
5. 鉴权：无 Key / 错 Key → 403；Agent Key 与 Admin Key **两级均可达**
6. `agent_id` 为 NULL 时正常返回 null
7. 纯只读：raw_files 行与磁盘文件在多次请求后均无变化
8. 子目录反推：uploads/ 下的文件同样可读
9. 索引存在但磁盘文件缺失 → ERR_INTERNAL（500），与 404 语义可区分
10. 纵深防御：含路径穿越片段的脏 file_id 不越界读盘
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sgme import config as sgme_config
from sgme.data import db as db_mod
from sgme.data import memory_dao
from sgme.data import session_dao
from sgme.operations.errors import ERR_INTERNAL, ERR_NOT_FOUND, OperationResult
from sgme.operations.session import get_raw_file_content
from sgme.raw import store as raw_store
from sgme.server.app import create_app

AGENT_KEY = "test-agent-key"
ADMIN_KEY = "test-admin-key"

# 契约 §4.7 冻结响应形态（键集合 + 顺序，任何变动即破坏性变更）
CONTRACT_KEYS = ["file_id", "session_key", "agent_id", "content"]


# ---------- fixtures ----------

@pytest.fixture
def cfg():
    return sgme_config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """隔离 raw/ 根目录（路由与 operations 均按 sgme_config.RAW_DIR 定位）。"""
    rd = tmp_path / "raw"
    (rd / "sessions").mkdir(parents=True)
    (rd / "uploads").mkdir(parents=True)
    monkeypatch.setattr(sgme_config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def conns(tmp_path, cfg):
    """三库连接（隔离 tmp_path）。"""
    mem_conn, session_conn, wiki_conn = db_mod.init_databases(tmp_path / "data")
    memory_dao.import_registry(mem_conn, cfg["dimensions"], cfg["aliases"])
    yield mem_conn, session_conn, wiki_conn
    db_mod.close(mem_conn)
    db_mod.close(session_conn)
    db_mod.close(wiki_conn)


@pytest.fixture
def session_conn(conns):
    return conns[1]


@pytest.fixture
def app(conns, cfg, raw_dir, tmp_path, monkeypatch):
    """隔离 FastAPI 应用（复用同一批连接，便于与 operations 直调对照）。"""
    monkeypatch.delenv("SGME_BEARER_TOKEN", raising=False)
    mem_conn, session_conn_, wiki_conn = conns
    return create_app(
        cfg=cfg,
        mem_conn=mem_conn,
        session_conn=session_conn_,
        wiki_conn=wiki_conn,
        admin_key=ADMIN_KEY,
        agent_key=AGENT_KEY,
        bearer_token="",  # 显式禁用 Bearer，只验 X-API-Key 层
        agent_store_path=tmp_path / "agent_keys.json",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------- 工具 ----------

def _make_raw_file(
    session_conn: sqlite3.Connection,
    raw_dir: Path,
    file_id: str,
    content: str,
    *,
    session_key: str | None = None,
    agent_id: str | None = "hermes",
    subdir: str = "sessions",
    write_bytes: bool = True,
) -> Path:
    """造一个 L0 原文：写磁盘文件 + 插 raw_files 索引行。

    Args:
        write_bytes: True 时用 write_bytes 落盘，**不做任何换行翻译**
            （测 CRLF 保留必须走这条路；Path.write_text 在 Windows 上会把
            \\n 翻成 \\r\\n，反而测不出真实差异）。

    Returns:
        磁盘文件路径。
    """
    path = raw_dir / subdir / f"{file_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if write_bytes:
        path.write_bytes(content.encode("utf-8"))
    else:
        path.write_text(content, encoding="utf-8")
    session_dao.insert_raw_file(
        session_conn,
        file_id=file_id,
        path=f"raw/{subdir}/{file_id}.md",
        session_key=session_key or f"sess-{file_id}",
        started_at="2026-08-09T21:27:43Z",
        agent_id=agent_id,
        status="new",
        size=len(content.encode("utf-8")),
    )
    return path


def _agent_get(client: TestClient, file_id: str):
    return client.get(f"/v1/sessions/{file_id}", headers={"X-API-Key": AGENT_KEY})


def _row(session_conn: sqlite3.Connection, file_id: str) -> dict | None:
    return session_dao.get_raw_file(session_conn, file_id)


SAMPLE = (
    "---\n"
    "format_version: 1\n"
    "file_id: f-sample\n"
    "session_key: sess-f-sample\n"
    "---\n"
    "# 2026-08-09T21:27:43Z user\n"
    "你好，帮我看下这个中文内容有没有乱码。\n"
)


# ---------- 1. 正常读取 ----------

def test_get_raw_returns_full_content(client, session_conn, raw_dir):
    """200 + 响应键集合与顺序符合契约 §4.7 + content 为原文全文。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-sample", SAMPLE,
                   session_key="hermes-5bf9bb", agent_id="hermes")

    # Act
    resp = _agent_get(client, "f-sample")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == CONTRACT_KEYS
    assert body["file_id"] == "f-sample"
    assert body["session_key"] == "hermes-5bf9bb"
    assert body["agent_id"] == "hermes"
    assert body["content"] == SAMPLE


def test_content_contains_whole_file_not_truncated(client, session_conn, raw_dir):
    """长文件不被截断：首尾标记均在，长度与磁盘一致。"""
    # Arrange：约 200KB 的长会话
    big = "".join(f"## 2026-08-09T21:{i % 60:02d}:00Z user\n第 {i} 条消息内容\n\n" for i in range(4000))
    content = "HEAD-MARKER\n" + big + "TAIL-MARKER\n"
    path = _make_raw_file(session_conn, raw_dir, "f-big", content)

    # Act
    body = _agent_get(client, "f-big").json()

    # Assert
    assert body["content"].startswith("HEAD-MARKER\n")
    assert body["content"].endswith("TAIL-MARKER\n")
    assert len(body["content"]) == len(content)
    assert body["content"] == path.read_bytes().decode("utf-8")


# ---------- 2. 与磁盘逐字符一致（CRLF / 中文） ----------

def test_content_identical_to_disk_bytes(client, session_conn, raw_dir):
    """content 与磁盘文件解码结果逐字符一致。"""
    # Arrange
    path = _make_raw_file(session_conn, raw_dir, "f-eq", SAMPLE)

    # Act
    content = _agent_get(client, "f-eq").json()["content"]

    # Assert：以字节为准，排除任何隐式换行翻译
    assert content == path.read_bytes().decode("utf-8")
    assert content.encode("utf-8") == path.read_bytes()


def test_crlf_line_endings_preserved(client, session_conn, raw_dir):
    """CRLF 行尾原样保留（不被通用换行模式翻译成 LF）。"""
    # Arrange：磁盘上是 \r\n
    crlf = "# 2026-08-09T21:27:43Z user\r\n第一行\r\n第二行\r\n"
    path = _make_raw_file(session_conn, raw_dir, "f-crlf", crlf)
    assert b"\r\n" in path.read_bytes()  # 前置断言：磁盘确实是 CRLF

    # Act
    content = _agent_get(client, "f-crlf").json()["content"]

    # Assert
    assert "\r\n" in content
    assert content == crlf
    assert content.count("\r\n") == 3


def test_mixed_and_lf_endings_preserved(client, session_conn, raw_dir):
    """混合行尾同样不被归一（LF 段保持 LF，CRLF 段保持 CRLF）。"""
    # Arrange
    mixed = "LF行\nCRLF行\r\n又一LF行\n"
    _make_raw_file(session_conn, raw_dir, "f-mixed", mixed)

    # Act
    content = _agent_get(client, "f-mixed").json()["content"]

    # Assert
    assert content == mixed
    assert content.count("\r\n") == 1
    assert content.count("\n") == 3


def test_unicode_content_not_corrupted(client, session_conn, raw_dir):
    """中文 / emoji / 特殊符号不损坏。"""
    # Arrange
    text = "中文测试 🚀 «引号» —破折号— \t制表符\n第二行：日本語・한국어\n"
    _make_raw_file(session_conn, raw_dir, "f-uni", text)

    # Act
    content = _agent_get(client, "f-uni").json()["content"]

    # Assert
    assert content == text


# ---------- 3. 真实 L0 文件（raw_store 产出）可读回并解析 ----------

def test_real_l0_file_roundtrip_parsable(client, session_conn, raw_dir):
    """raw_store 写出的真实 L0 文件读回后仍能被 raw_store 解析（格式无损）。"""
    # Arrange：用生产写入函数造文件，避免测试自造格式与实现漂移
    file_id = "20260809_212743_5bf9bb"
    path = raw_store.write_new_file(
        file_id=file_id,
        session_key="hermes-5bf9bb",
        started_at="2026-08-09T21:27:43Z",
        agent_id="hermes",
        first_messages=[
            {"timestamp": "2026-08-09T21:27:43Z", "role": "user", "content": "第一个问题"},
            {"timestamp": "2026-08-09T21:28:10Z", "role": "assistant", "content": "回答内容"},
        ],
    )
    assert path.is_file()  # 前置断言：RAW_DIR monkeypatch 生效，文件落在 tmp_path
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=raw_store.relative_path(file_id),
        session_key="hermes-5bf9bb", started_at="2026-08-09T21:27:43Z", agent_id="hermes",
    )

    # Act
    body = _agent_get(client, file_id).json()

    # Assert：内容与磁盘**逐字节**一致
    assert body["content"] == path.read_bytes().decode("utf-8")
    parsed = raw_store.parse_text(body["content"])
    assert parsed.frontmatter["file_id"] == file_id
    assert parsed.frontmatter["session_key"] == "hermes-5bf9bb"
    assert [m.role for m in parsed.messages] == ["user", "assistant"]
    # ⚠️ 正文比较刻意忽略首尾空白：raw_store.write_new_file 用 Path.write_text 落盘，
    # Windows 上 \n 被翻成 \r\n，而本端点按契约**原样**返回磁盘字节（不归一行尾）；
    # raw_store.parse_text 只 strip("\n") 不 strip("\r")，于是正文尾部残留 "\r"。
    # 这是 raw_store 的既有行为、且生产路径不可达（parse_file 走 read_text 会归一行尾），
    # 不属 ST-9 范围，故此处只做行尾无关比较，不去改 raw_store。
    assert parsed.messages[0].content.strip() == "第一个问题"
    assert parsed.messages[1].content.strip() == "回答内容"


# ---------- 4. file_id 不存在 → 404 ----------

def test_unknown_file_id_returns_404(client):
    """`file_id` 不存在 → 404 + ERR_NOT_FOUND（契约 §4.7）。"""
    # Act
    resp = _agent_get(client, "does-not-exist")

    # Assert
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == ERR_NOT_FOUND


def test_404_body_uses_unified_error_structure(client):
    """404 响应体沿用统一错误结构 {"error":{"code","message"}}。"""
    # Act
    body = _agent_get(client, "nope").json()

    # Assert
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}
    assert body["error"]["code"] == ERR_NOT_FOUND
    assert "nope" in body["error"]["message"]


def test_orphan_disk_file_without_index_row_is_404(client, raw_dir):
    """磁盘有文件但 raw_files 无索引行 → 仍 404（索引是唯一存在性判据）。"""
    # Arrange：只写盘，不插索引
    (raw_dir / "sessions" / "f-orphan.md").write_bytes(b"orphan content\n")

    # Act
    resp = _agent_get(client, "f-orphan")

    # Assert
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ERR_NOT_FOUND


# ---------- 5. 鉴权 ----------

def test_missing_api_key_returns_403(client, session_conn, raw_dir):
    """无 X-API-Key → 403（不泄露资源是否存在）。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-auth", SAMPLE)

    # Act
    resp = client.get("/v1/sessions/f-auth")

    # Assert
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_invalid_api_key_returns_403(client, session_conn, raw_dir):
    """错误的 X-API-Key → 403。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-auth2", SAMPLE)

    # Act
    resp = client.get("/v1/sessions/f-auth2", headers={"X-API-Key": "wrong-key"})

    # Assert
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR_FORBIDDEN"


def test_auth_rejected_before_existence_check(client):
    """无 Key 访问不存在的 file_id → 403 而非 404（鉴权先于存在性判定）。"""
    # Act
    resp = client.get("/v1/sessions/whatever-not-exist")

    # Assert
    assert resp.status_code == 403


def test_admin_key_can_also_read(client, session_conn, raw_dir):
    """Admin Key 亦可读（`is_agent` 对 admin key 返回 True）→ 两级鉴权均可达。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-admin", SAMPLE)

    # Act
    resp = client.get("/v1/sessions/f-admin", headers={"X-API-Key": ADMIN_KEY})

    # Assert
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == SAMPLE


def test_registered_agent_key_can_read(app, client, session_conn, raw_dir):
    """注册签发的 Agent Key 同样可读（无归属校验，单用户语义）。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-reg", SAMPLE, agent_id="other-agent")
    new_key = app.state.key_store.register_agent("some-other-agent")

    # Act：用「别的 agent」的 key 读「hermes 的」文件——契约明确允许
    resp = client.get("/v1/sessions/f-reg", headers={"X-API-Key": new_key})

    # Assert
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "other-agent"


# ---------- 6. agent_id 为 NULL ----------

def test_null_agent_id_returned_as_null(client, session_conn, raw_dir):
    """raw_files.agent_id 为 NULL → 响应 agent_id 为 null（契约标注「可能 null」）。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-noagent", SAMPLE, agent_id=None)

    # Act
    body = _agent_get(client, "f-noagent").json()

    # Assert
    assert list(body.keys()) == CONTRACT_KEYS
    assert body["agent_id"] is None
    assert body["content"] == SAMPLE


def test_empty_file_returns_empty_content(client, session_conn, raw_dir):
    """空文件 → content 为空串（而非 404/500）。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-empty", "")

    # Act
    resp = _agent_get(client, "f-empty")

    # Assert
    assert resp.status_code == 200
    assert resp.json()["content"] == ""


# ---------- 7. 纯只读，无副作用 ----------

def test_read_is_side_effect_free(client, session_conn, raw_dir):
    """多次读取后：raw_files 行、磁盘内容、文件大小均无变化。"""
    # Arrange
    path = _make_raw_file(session_conn, raw_dir, "f-ro", SAMPLE)
    row_before = _row(session_conn, "f-ro")
    bytes_before = path.read_bytes()

    # Act：连读三次
    bodies = [_agent_get(client, "f-ro").json() for _ in range(3)]

    # Assert：响应稳定 + 状态零变化
    assert bodies[0] == bodies[1] == bodies[2]
    assert _row(session_conn, "f-ro") == row_before
    assert path.read_bytes() == bytes_before
    # 未被误标为已提炼
    assert _row(session_conn, "f-ro")["status"] == "new"
    assert _row(session_conn, "f-ro")["refined_at"] is None


def test_read_does_not_create_files(client, session_conn, raw_dir):
    """读取不产生任何新文件（不写缓存、不建目录）。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-nofile", SAMPLE)
    before = sorted(p.relative_to(raw_dir).as_posix() for p in raw_dir.rglob("*"))

    # Act
    _agent_get(client, "f-nofile")
    _agent_get(client, "f-missing-id")

    # Assert
    after = sorted(p.relative_to(raw_dir).as_posix() for p in raw_dir.rglob("*"))
    assert after == before


# ---------- 8. 子目录反推 ----------

def test_uploads_subdir_file_is_readable(client, session_conn, raw_dir):
    """path 指向 raw/uploads/ 时按 uploads 子目录定位（非会话来源同样可读）。"""
    # Arrange
    text = "上传文档正文\n"
    _make_raw_file(session_conn, raw_dir, "f-upload", text, subdir="uploads")

    # Act
    resp = _agent_get(client, "f-upload")

    # Assert
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == text


def test_unknown_subdir_falls_back_to_sessions(client, session_conn, raw_dir):
    """path 列异常（子目录不在白名单）→ 回落 sessions/ 仍能读到。"""
    # Arrange：文件实际在 sessions/，但索引 path 写成了怪值
    (raw_dir / "sessions" / "f-weird.md").write_bytes(SAMPLE.encode("utf-8"))
    session_dao.insert_raw_file(
        session_conn, file_id="f-weird", path="某个/历史遗留/怪路径.md",
        session_key="sess-weird", agent_id=None,
    )

    # Act
    resp = _agent_get(client, "f-weird")

    # Assert
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == SAMPLE


# ---------- 9. 索引存在但磁盘缺失 → ERR_INTERNAL ----------

def test_index_without_disk_file_returns_500(client, session_conn, raw_dir):
    """raw_files 有行但磁盘无文件 → 500 ERR_INTERNAL（与 404 语义区分）。"""
    # Arrange：只插索引，不写盘
    session_dao.insert_raw_file(
        session_conn, file_id="f-ghost", path="raw/sessions/f-ghost.md",
        session_key="sess-ghost", agent_id="hermes",
    )

    # Act
    resp = _agent_get(client, "f-ghost")

    # Assert
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == ERR_INTERNAL
    # 与「查错 id」可区分：文案点明是原文缺失，不是 id 不存在
    assert "缺失" in resp.json()["error"]["message"]


# ---------- 10. operations 层直调 ----------

def test_operation_returns_operation_result_ok(session_conn, raw_dir):
    """get_raw_file_content 返回 OperationResult(ok=True) + 契约 data 形态。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-op", SAMPLE, session_key="sk", agent_id="ag")

    # Act
    res = get_raw_file_content(session_conn, "f-op", raw_dir)

    # Assert
    assert isinstance(res, OperationResult)
    assert res.ok is True
    assert res.error_code is None
    assert list(res.data.keys()) == CONTRACT_KEYS
    assert res.data == {
        "file_id": "f-op", "session_key": "sk", "agent_id": "ag", "content": SAMPLE,
    }


def test_operation_not_found(session_conn, raw_dir):
    """未知 file_id → ok=False + ERR_NOT_FOUND，且 data 为 None。"""
    # Act
    res = get_raw_file_content(session_conn, "ghost-id", raw_dir)

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_NOT_FOUND
    assert res.data is None


def test_operation_missing_disk_file(session_conn, raw_dir):
    """索引存在、磁盘缺失 → ok=False + ERR_INTERNAL。"""
    # Arrange
    session_dao.insert_raw_file(
        session_conn, file_id="f-gone", path="raw/sessions/f-gone.md",
        session_key="sk", agent_id=None,
    )

    # Act
    res = get_raw_file_content(session_conn, "f-gone", raw_dir)

    # Assert
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL


def test_operation_rejects_path_traversal_file_id(session_conn, raw_dir, tmp_path):
    """脏 file_id 含路径穿越片段 → 拒绝越界读盘（纵深防御）。"""
    # Arrange：raw_dir 之外放一个「机密」文件，并伪造一条指向它的索引
    secret = tmp_path / "secret.md"
    secret.write_bytes("SECRET-CONTENT".encode("utf-8"))
    evil_id = "../secret"
    session_dao.insert_raw_file(
        session_conn, file_id=evil_id, path="raw/sessions/x.md",
        session_key="sk", agent_id=None,
    )

    # Act
    res = get_raw_file_content(session_conn, evil_id, raw_dir)

    # Assert：绝不返回 raw_dir 之外的内容
    assert res.ok is False
    assert res.error_code == ERR_INTERNAL
    assert res.data is None


def test_operation_accepts_str_raw_dir(session_conn, raw_dir):
    """raw_dir 传字符串同样工作（入口层可能传 str 路径）。"""
    # Arrange
    _make_raw_file(session_conn, raw_dir, "f-str", SAMPLE)

    # Act
    res = get_raw_file_content(session_conn, "f-str", str(raw_dir))

    # Assert
    assert res.ok is True
    assert res.data["content"] == SAMPLE
