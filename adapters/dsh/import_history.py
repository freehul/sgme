"""dsh 历史会话全量导入（适配层专用方法）。

适配层职责（架构 v0.6 §8）：每个 Agent 适配器提供「会话导入」专用方法——
不同 Agent 会话保存形式不同（Hermes state.db / dsh SQLite…），
格式解析在适配层收敛，SGME 只收标准 L0。

dsh 会话存储：dsh v0.1 使用 SQLite 持久化 session（packages/session/），
会话文件位于 ~/.dsh/sessions/ 或 DSH_HOME（待实测确认具体路径与 schema）。
本模块提供导入骨架，具体 session 表解析待 dsh 稳定后补全（见 TODO）。

安全设计：
- 幂等：session_key 已在 raw_files 的记录跳过（重跑安全）
- 只读 dsh 存档 + 只写 SGME L0——不删不改任何原件
- --limit 试跑；--dry-run 只统计不写入；--no-refine 导入后不触发提炼

用法：
  .venv/Scripts/python.exe adapters/dsh/import_history.py [--limit N] [--dry-run]
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

# 加载 adapters/dsh/.env（install.py 写入的 key）
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

_BASE_URL = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910").rstrip("/")
_AGENT_KEY = os.environ.get("SGME_AGENT_KEY", "dev-agent-key-change-me")
_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_AGENT_ID = os.environ.get("SGME_DSH_AGENT_ID", "dsh")

# dsh 会话存储根（待实测确认；优先 DSH_HOME 环境变量，兜底 ~/.dsh）
_DSH_HOME = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))


def discover_sessions() -> list[Path]:
    """扫描 dsh 存量会话文件。

    TODO（dsh 稳定后补全）：
    - dsh v0.1 session 存储路径与格式待实测确认（packages/session/ SQLite 或 jsonl）
    - 当前返回空列表，待确认后实现具体发现逻辑
    - 排除测试会话（避免 e2e 污染记忆池）
    """
    # 占位：待 dsh 会话存储路径确认后实现
    # 候选路径：~/.dsh/sessions/*.jsonl 或 ~/.dsh/sessions.db
    sessions_dir = _DSH_HOME / "sessions"
    if not sessions_dir.exists():
        return []
    # 临时实现：扫描 jsonl（若 dsh 用 SQLite 则需改为 sqlite3 查询）
    return sorted(sessions_dir.glob("*.jsonl"))


def parse_session_file(path: Path) -> list[dict]:
    """解析 dsh 会话文件 → 消息列表（[{role, content, ts}]）。

    TODO（dsh 稳定后补全）：
    - dsh session 事件结构待实测（turn/end 事件的 payload schema）
    - 当前实现假设 jsonl 行格式与常见会话 jsonl 类似（role/content/createdAt）
    - dsh 实际格式确认后调整字段映射
    """
    msgs: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = d.get("role", "")
        if role == "system":
            continue
        content = d.get("content") or d.get("raw_content") or ""
        if not content.strip():
            continue
        ts = d.get("createdAt") or d.get("ts") or ""
        msgs.append({"role": role, "content": content, "ts": ts})
    return msgs


def to_l0(messages: list[dict]) -> str:
    """消息列表 → SGME L0 消息块文本（与 L0 格式约定一致）。

    格式：`# {ts} user` / `## {ts} assistant|tool`（tool 块首行 `**tool**: {name}`）。
    """
    blocks: list[str] = []
    for m in messages:
        if m["role"] == "user":
            blocks.append(f"# {m['ts']} user\n{m['content']}")
        elif m["role"] == "tool":
            blocks.append(f"## {m['ts']} tool\n**tool**: {m.get('name', 'tool')}\n{m['content']}")
        else:
            blocks.append(f"## {m['ts']} assistant\n{m['content']}")
    return "\n\n".join(blocks) + "\n"


def _http() -> httpx.Client | None:
    if httpx is None:
        return None
    return httpx.Client(timeout=5.0, trust_env=False)


def append_to_sgme(l0_text: str, session_key: str, started_at: str) -> bool:
    """L0 写入 SGME（/v1/append，agent key）。"""
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/append",
            json={
                "session_key": session_key,
                "started_at": started_at,
                "content": l0_text,
                "agent_id": _AGENT_ID,
            },
            headers={"X-API-Key": _AGENT_KEY},
        )
        return r.status_code == 200
    except Exception as e:
        print(f"append 异常: {e}")
        return False
    finally:
        cli.close()


def trigger_refine() -> bool:
    """导入完成后触发批量提炼（fire-and-forget，Gateway 后台执行）。"""
    cli = _http()
    if cli is None:
        return False
    try:
        r = cli.post(
            f"{_BASE_URL}/v1/admin/refine/trigger_async",
            json={"limit": 50},
            headers={"X-API-Key": _ADMIN_KEY},
        )
        return r.status_code in (200, 202)
    except Exception as e:
        print(f"提炼触发失败（可稍后手动触发）: {e}")
        return False
    finally:
        cli.close()


def get_existing_session_keys() -> set[str]:
    """查 SGME raw_files 已有的 dsh 会话 key（幂等去重）。"""
    from sgme.data.db import get_session_conn
    conn = get_session_conn()
    try:
        rows = conn.execute(
            "SELECT session_key FROM raw_files WHERE session_key LIKE 'dsh-%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="dsh 历史会话全量导入 SGME")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--no-refine", action="store_true", help="导入后不触发提炼")
    args = parser.parse_args()

    existing = get_existing_session_keys()
    files = discover_sessions()
    todo = [f for f in files if f"dsh-{f.stem}" not in existing]

    print(f"发现 {len(files)} 个会话，待导入 {len(todo)} 个（已存在 {len(existing)} 个跳过）")
    if not todo:
        print("无待导入会话")
        return 0
    if args.dry_run:
        for f in todo[: args.limit or len(todo)]:
            print(f"  [待导入] {f.name}")
        print("dry-run 结束（未写入）")
        return 0

    ok = fail = 0
    t0 = time.time()
    for i, f in enumerate(todo[: args.limit or len(todo)], 1):
        try:
            messages = parse_session_file(f)
            if not messages:
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（无消息）: {f.name}")
                continue
            l0_text = to_l0(messages)
            session_key = f"dsh-{f.stem}"
            started_at = messages[0].get("ts", "") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if append_to_sgme(l0_text, session_key, started_at):
                ok += 1
            else:
                fail += 1
                print(f"[{i}/{len(todo)}] 写入失败: {f.name}")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(todo)}] 异常 {f.name}: {e}")
        if i % 10 == 0:
            print(f"  进度 {i}/{len(todo)} | 成功 {ok} 失败 {fail} | {time.time()-t0:.0f}s")

    print(f"导入完成: 成功 {ok}，失败 {fail}（失败项重跑本脚本即可补漏）")

    if ok and not args.no_refine:
        if trigger_refine():
            print("已触发批量提炼（Gateway 后台执行，可用 /v1/admin/refine 查询进度）")
        else:
            print("⚠️ 提炼触发失败——导入数据在 L0 等待，可重跑本脚本或手动触发")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
