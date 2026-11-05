"""tests/test_refine_fail_circuit.py：E3 失败熔断 + E4 体积闸（2026-11-05）。

《SGME2.0-收尾交接》E3/E4 验收覆盖：
1. 可重试失败（块级缺口）：失败 1..N-1 次仍 status=new（含 fail_count 递增），
   第 N 次转 status=error 且 last_error 有值，熔断后批扫不再拾起
2. 成功路径（commit_refine 落库成功）：fail_count / last_error 清零
3. 终态失败（L0 解析失败）也记账，status 仍直接 error（不改变既有终态语义）
4. 批扫异常路径（refine 入口之外，如 persist 落库失败）递增计数，达上限终止
5. 体积闸：超限文件不解析、不调 LLM，直接 error + last_error='file_too_large'
6. 体积闸兜底：size 列为 NULL 时读磁盘实际大小判定
7. 旧库幂等迁移：raw_files 缺 fail_count/last_error → connect_session 幂等补齐
8. 配置键与写白名单（DEFAULT_REFINE_CONFIG / SECTION_KEYS）

全部零真实 LLM：L1 一律 monkeypatch；体积闸用「调用即断言失败」的桩证明零调用。
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from sgme import config
from sgme.data import db as db_mod, memory_dao, session_dao
from sgme.engine import batch_scan as batch_scan_mod
from sgme.engine import pipeline as pipeline_mod
from sgme.engine import refine as refine_mod
from sgme.raw import store


# ---------- fixtures（与 tests/test_engine.py 同款隔离） ----------

@pytest.fixture
def cfg():
    return config.load_config()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    rd = tmp_path / "raw"
    rd.mkdir()
    monkeypatch.setattr(config, "RAW_DIR", rd)
    return rd


@pytest.fixture
def mem_conn(tmp_path, cfg):
    conn = db_mod.connect_memory(tmp_path)
    memory_dao.import_registry(conn, cfg["dimensions"], cfg["aliases"])
    yield conn
    conn.close()


@pytest.fixture
def session_conn(tmp_path):
    conn = db_mod.connect_session(tmp_path)
    yield conn
    conn.close()


# ---------- 工具 ----------

def _setup_raw_file(raw_dir, session_conn, file_id="f-circuit", messages=None):
    """构造 raw 文件（默认 2 条消息）+ raw_files 行（status=new，带 size）。"""
    msgs = messages or [
        {"timestamp": "2026-11-05T10:00:00Z", "role": "user", "content": "我用 Python 写 SGME"},
        {"timestamp": "2026-11-05T10:01:00Z", "role": "assistant", "content": "了解"},
    ]
    store.write_new_file(
        file_id=file_id, session_key="sess_circuit",
        started_at="2026-11-05T10:00:00Z", agent_id="test",
        first_messages=msgs,
    )
    session_dao.insert_raw_file(
        session_conn, file_id=file_id, path=store.relative_path(file_id),
        session_key="sess_circuit", started_at="2026-11-05T10:00:00Z",
        agent_id="test", status="new", size=store.file_size(file_id),
    )
    return file_id


def _raw(session_conn, file_id):
    return session_dao.get_raw_file(session_conn, file_id)


def _stub_chunk_failure(monkeypatch, failures=1, total=2):
    """L1 桩：返回空记忆 + chunk_failures/total（不调 LLM）。"""

    def fake_extract(*args, **kwargs):
        return [], "agnes", {
            "stage": "l1_extraction", "version": "v019", "variant": None,
            "chunk_failures": failures, "chunk_total": total,
        }

    monkeypatch.setattr(refine_mod.l2_extraction, "extract_memories", fake_extract)


# ======================================================================
# 1. 可重试失败（块级缺口）：1..N-1 次仍 new，第 N 次 error
# ======================================================================

def test_chunk_failure_trips_breaker_at_max_attempts(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    fid = _setup_raw_file(raw_dir, session_conn)
    cfg["refine"]["max_file_attempts"] = 3
    _stub_chunk_failure(monkeypatch, failures=1, total=2)

    for attempt in (1, 2):
        result, _stats = pipeline_mod.refine_one(fid, mem_conn, session_conn, cfg)
        assert result.status == "partial", "块级缺口必须被提交闸拦下"
        rf = _raw(session_conn, fid)
        assert rf["status"] == "new", f"第 {attempt} 次失败后应保持 new"
        assert rf["fail_count"] == attempt
        assert rf["last_error"] and "块级失败" in rf["last_error"]

    # 第 N 次 → 熔断：status=error，不再进入批扫视野
    result, _stats = pipeline_mod.refine_one(fid, mem_conn, session_conn, cfg)
    assert result.status == "partial"
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["fail_count"] == 3
    assert rf["last_error"]
    assert session_dao.list_by_status(session_conn, "new") == []


def test_chunk_failure_below_threshold_stays_retryable(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    """默认阈值 5：单次块级失败不终止重试（仍 new），且 last_error 留痕。"""
    fid = _setup_raw_file(raw_dir, session_conn)
    _stub_chunk_failure(monkeypatch, failures=2, total=5)

    result, _stats = pipeline_mod.refine_one(fid, mem_conn, session_conn, cfg)
    assert result.status == "partial"
    rf = _raw(session_conn, fid)
    assert rf["status"] == "new"
    assert rf["fail_count"] == 1
    assert "2/5" in rf["last_error"]


# ======================================================================
# 2. 成功路径清零
# ======================================================================

def test_success_resets_failure_counter(raw_dir, mem_conn, session_conn, cfg):
    fid = _setup_raw_file(raw_dir, session_conn)
    session_dao.record_refine_failure(session_conn, fid, "旧失败一", max_attempts=5)
    session_dao.record_refine_failure(session_conn, fid, "旧失败二", max_attempts=5)
    assert _raw(session_conn, fid)["fail_count"] == 2

    # 无增量（游标已到末条）→ refine_file 产出 refined 结果，不调 LLM
    session_dao.update_refine_cursor(session_conn, fid, 2)
    session_dao.mark_status(session_conn, fid, "new")
    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert result.status == "refined"
    # 清零只在落库成功后的 commit_refine 发生（refine_file 不动计数）
    assert _raw(session_conn, fid)["fail_count"] == 2
    assert refine_mod.commit_refine(result, session_conn) is True

    rf = _raw(session_conn, fid)
    assert rf["fail_count"] == 0
    assert rf["last_error"] is None
    assert rf["status"] == "refined"


# ======================================================================
# 3. 终态失败（L0 解析失败）也记账
# ======================================================================

def test_l0_parse_failure_records_failure_count(raw_dir, mem_conn, session_conn, cfg):
    fid = "f-bad-l0"
    path = store.file_path(fid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("无 frontmatter", encoding="utf-8")
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path=store.relative_path(fid),
        session_key="sess_bad_l0", started_at="2026-11-05T11:00:00Z", status="new",
    )

    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert result.status == "error"          # 终态语义不变（与修复前一致）
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["fail_count"] == 1
    assert "L0 解析失败" in rf["last_error"]


# ======================================================================
# 4. 批扫异常路径（refine 入口之外）递增计数 + 熔断
# ======================================================================

def test_batch_scan_exception_counts_and_trips(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    fid = _setup_raw_file(raw_dir, session_conn)
    cfg["refine"]["max_file_attempts"] = 2

    def boom(file_id, *args, **kwargs):
        raise RuntimeError("injected persist failure")

    monkeypatch.setattr(batch_scan_mod.pipeline_mod, "refine_one", boom)

    s1 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s1["failed"] == 1
    rf = _raw(session_conn, fid)
    assert rf["status"] == "new"
    assert rf["fail_count"] == 1
    assert "persist failure" in rf["last_error"]

    s2 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s2["failed"] == 1
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["fail_count"] == 2

    # 熔断后：批扫不再拾起（scanned=0），无更多失败计数
    s3 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s3["scanned"] == 0
    assert _raw(session_conn, fid)["fail_count"] == 2


# ======================================================================
# 5. E4 体积闸：超限文件不解析、不调 LLM
# ======================================================================

def test_oversized_file_gated_before_parse_and_llm(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    cfg["refine"]["max_file_bytes"] = 1024
    fid = "f-huge"
    # 刻意不写磁盘：体积闸只依赖 raw_files.size，必须在 parse 之前拦下
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path="raw/sessions/f-huge.md",
        session_key="sess_huge", started_at="2026-11-05T12:00:00Z",
        status="new", size=2048,
    )

    def _no_parse(*args, **kwargs):
        raise AssertionError("体积闸生效时不应解析 L0")

    def _no_llm(request):
        raise AssertionError("体积闸生效时不应调 LLM")

    monkeypatch.setattr(refine_mod.store, "parse_file", _no_parse)
    client = httpx.Client(
        transport=httpx.MockTransport(_no_llm), trust_env=False
    )

    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg, client=client)
    assert result.status == "error"
    assert result.error == "file_too_large"
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["last_error"] == "file_too_large"
    assert rf["fail_count"] == 0, "体积闸是终态定案，不计入失败熔断次数"


def test_oversized_detected_from_disk_when_size_missing(
    raw_dir, mem_conn, session_conn, cfg
):
    """size 列为 NULL（历史行）→ 读磁盘实际大小兜底判定。"""
    cfg["refine"]["max_file_bytes"] = 512
    fid = _setup_raw_file(raw_dir, session_conn)
    store.append_messages(fid, [
        {"timestamp": "2026-11-05T10:02:00Z", "role": "user", "content": "x" * 2000},
    ])
    session_conn.execute("UPDATE raw_files SET size=NULL WHERE file_id=?", (fid,))
    session_conn.commit()

    result = refine_mod.refine_file(fid, mem_conn, session_conn, cfg)
    assert result.status == "error"
    assert result.error == "file_too_large"
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["last_error"] == "file_too_large"


# ======================================================================
# 6. 旧库迁移幂等
# ======================================================================

def test_old_db_migration_adds_fail_columns_idempotent(tmp_path):
    """旧 schema（无两列）→ connect_session 补列；二次连接不重复 ALTER。"""
    db_path = tmp_path / "session.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE raw_files (
          file_id TEXT PRIMARY KEY, path TEXT NOT NULL, session_key TEXT NOT NULL,
          agent_id TEXT, agent_model TEXT, started_at TEXT, ended_at TEXT, refined_at TEXT,
          last_refined_seq INTEGER, status TEXT NOT NULL DEFAULT 'new', size INTEGER,
          content_hash TEXT);
        INSERT INTO raw_files (file_id, path, session_key, status)
          VALUES ('f-old', 'p', 's', 'new');
        """
    )
    conn.commit()
    conn.close()

    for _ in range(2):  # 第二次连接证明迁移幂等（重复 ALTER 会抛 duplicate column）
        c = db_mod.connect_session(tmp_path)
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(raw_files)").fetchall()]
            assert "fail_count" in cols
            assert "last_error" in cols
            row = c.execute(
                "SELECT fail_count, last_error FROM raw_files WHERE file_id='f-old'"
            ).fetchone()
            assert row[0] == 0, "存量行 fail_count 必须回填默认 0"
            assert row[1] is None
        finally:
            c.close()


# ======================================================================
# 5b. 体积闸在批扫路径同样生效（超大文件不再进队列）
# ======================================================================

def test_batch_scan_gates_oversized_file_without_llm(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    cfg["refine"]["max_file_bytes"] = 1024
    fid = "f-huge-scan"
    session_dao.insert_raw_file(
        session_conn, file_id=fid, path="raw/sessions/f-huge-scan.md",
        session_key="sess_huge_scan", started_at="2026-11-05T12:30:00Z",
        status="new", size=4096,
    )

    monkeypatch.setattr(refine_mod.l2_extraction, "extract_memories",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("体积闸生效时不应进 L1 提取")))

    s1 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s1["scanned"] == 1
    assert s1["refined"] == 0
    assert s1["failed"] == 1
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["last_error"] == "file_too_large"

    # 已标 error → 下一轮不再进队列
    s2 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s2["scanned"] == 0


# ======================================================================
# 5c. 批扫 + 块级缺口：只记一次失败（不与 refine_file 重复计数）
# ======================================================================

def test_batch_scan_chunk_failure_not_double_counted(
    raw_dir, mem_conn, session_conn, cfg, monkeypatch
):
    fid = _setup_raw_file(raw_dir, session_conn)
    cfg["refine"]["max_file_attempts"] = 2
    _stub_chunk_failure(monkeypatch, failures=1, total=3)

    s1 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s1["failed"] == 1
    rf = _raw(session_conn, fid)
    assert rf["status"] == "new"
    assert rf["fail_count"] == 1, "批扫不得对 refine_file 已记账的失败重复计数"

    s2 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s2["failed"] == 1
    rf = _raw(session_conn, fid)
    assert rf["status"] == "error"
    assert rf["fail_count"] == 2

    s3 = batch_scan_mod.run_batch_scan(mem_conn, session_conn, cfg)
    assert s3["scanned"] == 0


# ======================================================================
# 7b. yaml 显式配置必须透传到运行时 cfg（B121 同类缺陷回归锁）
# ======================================================================

def _write_refine_yaml(tmp_path, body: str):
    path = tmp_path / "sgme.yaml"
    path.write_text("refine:\n" + body, encoding="utf-8")
    return path


def test_yaml_refine_circuit_keys_take_effect(tmp_path):
    """sgme.yaml 写 max_file_attempts / max_file_bytes（含同批补漏的三键）→ 运行时生效。

    历史缺陷（与 B121 llm_override 同类）：_merge_refine_config 只合并已知键，
    新键被静默丢弃——yaml 写了等于没写。本用例锁死合并链 + 引擎读取函数同源。
    """
    yaml_path = _write_refine_yaml(
        tmp_path,
        "  max_file_attempts: 2\n"
        "  max_file_bytes: 2048\n"
        "  max_chunk_failure_ratio: 0.05\n"
        "  chunk_retry_times: 3\n"
        "  chunk_retry_backoff_s: 1.5\n",
    )
    cfg = config.load_config(sgme_path=str(yaml_path))
    ref = cfg["refine"]

    assert ref["max_file_attempts"] == 2
    assert ref["max_file_bytes"] == 2048
    assert ref["max_chunk_failure_ratio"] == 0.05
    assert ref["chunk_retry_times"] == 3
    assert ref["chunk_retry_backoff_s"] == 1.5

    # 引擎读取函数必须同源（只写进 cfg、读侧不认 = 没生效）
    assert refine_mod.max_file_attempts(cfg) == 2
    assert refine_mod.max_file_bytes(cfg) == 2048
    assert refine_mod._gate_refine_size(
        {"file_id": "x", "size": 3000}, cfg
    ) == 3000  # 2048 上限 → 3000 字节被拦


def test_yaml_refine_circuit_invalid_values_fall_back(tmp_path):
    """yaml 非法值（0 / 负数 / 布尔 / 字符串）→ 一律回退默认（不静默放行错配置）。"""
    yaml_path = _write_refine_yaml(
        tmp_path,
        "  max_file_attempts: 0\n"
        "  max_file_bytes: -1\n"
        "  max_chunk_failure_ratio: -0.1\n"
        "  chunk_retry_times: true\n"
        '  chunk_retry_backoff_s: "2.0"\n',
    )
    cfg = config.load_config(sgme_path=str(yaml_path))
    ref = cfg["refine"]

    assert ref["max_file_attempts"] == config.DEFAULT_REFINE_CONFIG["max_file_attempts"] == 5
    assert ref["max_file_bytes"] == config.DEFAULT_REFINE_CONFIG["max_file_bytes"] == 10 * 1024 * 1024
    assert ref["max_chunk_failure_ratio"] == 0.0
    assert ref["chunk_retry_times"] == 2
    assert ref["chunk_retry_backoff_s"] == 5.0


def test_yaml_max_file_bytes_zero_disables_gate(tmp_path):
    """yaml 显式 max_file_bytes: 0 = 关闭体积闸（合法值，不得回退默认 10 MiB）。"""
    yaml_path = _write_refine_yaml(tmp_path, "  max_file_bytes: 0\n")
    cfg = config.load_config(sgme_path=str(yaml_path))

    assert cfg["refine"]["max_file_bytes"] == 0
    assert refine_mod.max_file_bytes(cfg) == 0
    assert refine_mod._gate_refine_size(
        {"file_id": "x", "size": 50 * 1024 * 1024}, cfg
    ) is None


# ======================================================================
# 8. 配置键与写白名单
# ======================================================================

def test_refine_config_keys_and_whitelist():
    assert config.DEFAULT_REFINE_CONFIG["max_file_attempts"] == 5
    assert config.DEFAULT_REFINE_CONFIG["max_file_bytes"] == 10 * 1024 * 1024

    allowed = config.filter_keys(
        "refine",
        {"max_file_attempts": 3, "max_file_bytes": 2048, "unexpected": 1},
    )
    assert allowed == {"max_file_attempts": 3, "max_file_bytes": 2048}

    # 合并函数（老 yaml 无这两个键）仍保留默认值
    merged = config._merge_refine_config({})
    assert merged["max_file_attempts"] == 5
    assert merged["max_file_bytes"] == 10 * 1024 * 1024


def test_attempt_threshold_helper_guards_invalid_values():
    """非法阈值（0 / 负数 / 非数字）回退默认 5；体积闸 0 = 关闭。"""
    assert refine_mod.max_file_attempts({"refine": {"max_file_attempts": 0}}) == 5
    assert refine_mod.max_file_attempts({"refine": {"max_file_attempts": -3}}) == 5
    assert refine_mod.max_file_attempts({"refine": {"max_file_attempts": "x"}}) == 5
    assert refine_mod.max_file_attempts({"refine": {"max_file_attempts": 2}}) == 2
    assert refine_mod.max_file_bytes({"refine": {"max_file_bytes": 0}}) == 0
    assert refine_mod._gate_refine_size({"file_id": "x", "size": 100},
                                        {"refine": {"max_file_bytes": 0}}) is None
