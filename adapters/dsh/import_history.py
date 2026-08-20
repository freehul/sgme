"""dsh 历史会话全量导入（适配层专用方法）。

适配层职责（架构 v0.6 §8）：每个 Agent 适配器提供「会话导入」专用方法——
不同 Agent 会话保存形式不同（Hermes state.db / dsh jsonl.zstd…），
格式解析在适配层收敛，SGME 只收标准 L0。

dsh 会话存储（2026-08-21 T-90 实测确认，rc8 格式）：
- 目录：~/.dsh/sessions/<workspace>/<会话id>/session.jsonl.zstd
- 会话 id 目录两种命名并存：老格式 session-<uuid>/、新格式 <uuid>/（rc8 存储格式不兼容，两代并存）
- session.jsonl.zstd = zstd 多帧压缩（zstandard 库 stream_reader 解压），
  首行 {"type":"session",...} 头 + 事件流（user/message、assistant/message、
  tool/result、turn/*、step/*、chunk 类噪音事件）
- 事件结构对齐 sgme-bridge/src/session-sync.ts（T-53 解压确认同款）

安全设计：
- 幂等：session_key 已在 raw_files 的记录跳过（重跑安全）
- 只读 dsh 存档 + 只写 SGME L0——不删不改任何原件
- --limit 试跑；--dry-run 只统计不写入；--no-refine 导入后不触发提炼
- 排除测试工作区（Temp / cli-test 目录，避免 e2e 污染记忆池）

用法：
  .venv/Scripts/python.exe adapters/dsh/import_history.py [--limit N] [--dry-run]
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

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

# dsh 会话存储根（优先 DSH_HOME 环境变量，兜底 ~/.dsh）
_DSH_HOME = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
# 会话文件固定名（rc8 格式）
_SESSION_FILE = "session.jsonl.zstd"


def _is_test_workspace(ws_name: str) -> bool:
    """测试/临时工作区判定（避免 e2e 污染记忆池）。

    实测：dsh cli 测试工作区命名为 --C-Users-LEO-AppData-Local-Temp-dsh-cli-test--。
    """
    return "Temp" in ws_name or "cli-test" in ws_name


def discover_sessions() -> list[Path]:
    """扫描 dsh 存量会话文件（rc8 格式：sessions/<workspace>/<id>/session.jsonl.zstd）。

    兼容两代目录命名：老格式 session-<uuid>/ 与新格式 <uuid>/；
    排除测试工作区（Temp / cli-test）。
    """
    sessions_root = _DSH_HOME / "sessions"
    if not sessions_root.exists():
        return []
    files: list[Path] = []
    for ws_dir in sorted(sessions_root.iterdir()):
        if not ws_dir.is_dir() or _is_test_workspace(ws_dir.name):
            continue
        for sdir in sorted(ws_dir.iterdir()):
            if not sdir.is_dir():
                continue
            f = sdir / _SESSION_FILE
            if f.exists():
                files.append(f)
    return files


def _ms_to_iso(ms) -> str | None:
    """毫秒时间戳 → ISO 8601（dsh 事件 time 为毫秒）。"""
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _extract_text(content_arr) -> str:
    """提取 content 数组中的 text 块（[{type:'text', text}]，忽略 reasoning/tool-call）。"""
    if not isinstance(content_arr, list):
        return ""
    parts: list[str] = []
    for c in content_arr:
        if not isinstance(c, dict):
            continue
        if c.get("type") != "text":
            continue
        t = c.get("text")
        if isinstance(t, str) and t.strip():
            parts.append(t)
    return "\n".join(parts)


def _extract_tool_result(content_arr) -> str:
    """提取 tool-result 块内层 text（[{type:'tool-result', content:[{type:'text', text}]}]）。"""
    if not isinstance(content_arr, list):
        return ""
    parts: list[str] = []
    for c in content_arr:
        if not isinstance(c, dict) or c.get("type") != "tool-result":
            continue
        inner = c.get("content")
        if not isinstance(inner, list):
            continue
        for t in inner:
            if isinstance(t, dict) and t.get("type") == "text":
                txt = t.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt)
    return "\n".join(parts)


def _event_to_message(ev: dict) -> dict | None:
    """单个事件 → 消息（非消息事件返回 None）。

    事件结构（对齐 sgme-bridge/src/session-sync.ts）：
    - user/message:      data.content: [{type:'text', text}]
    - assistant/message: data.message.content: [{type:'text'|'reasoning'|'tool-call', text?}]
    - tool/result:       data.message.content: [{type:'tool-result', content:[{type:'text', text}]}]
    """
    etype = ev.get("type", "")
    if etype not in ("user/message", "assistant/message", "tool/result"):
        return None
    data = ev.get("data") or {}
    ts = _ms_to_iso(ev.get("time"))

    if etype == "user/message":
        text = _extract_text(data.get("content"))
        if not text:
            return None
        return {"role": "user", "content": text, "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    if etype == "assistant/message":
        message = data.get("message") or {}
        text = _extract_text(message.get("content"))
        if not text:
            return None
        return {"role": "assistant", "content": text, "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # tool/result
    message = data.get("message") or {}
    text = _extract_tool_result(message.get("content"))
    if not text:
        return None
    return {"role": "tool", "content": text, "name": "tool", "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def parse_session_file(path: Path) -> list[dict]:
    """解析 dsh 会话文件（zstd 事件流）→ 消息列表（[{role, content, ts, name?}]）。

    首行 session 头 + 噪音事件（turn/*、step/*、chunk 类、tool/call 等）自动跳过。
    """
    if zstandard is None:
        raise RuntimeError("缺少 zstandard 依赖（pip install zstandard）")

    dctx = zstandard.ZstdDecompressor()
    try:
        with open(path, "rb") as f:
            reader = dctx.stream_reader(f)
            chunks: list[bytes] = []
            while True:
                c = reader.read(65536)
                if not c:
                    break
                chunks.append(c)
        data = b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  解压失败 {path.name}: {e}")
        return []

    msgs: list[dict] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        msg = _event_to_message(ev)
        if msg:
            msgs.append(msg)
    return msgs


def to_l0(messages: list[dict]) -> str:
    """消息列表 → SGME L0 消息块文本（与 L0 格式约定一致）。

    格式：'# {ts} user' / '## {ts} assistant|tool'（tool 块首行 '**tool**: {name}'）。
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


def append_to_sgme(l0_text: str, session_key: str, started_at: str, max_retries: int = 3) -> bool:
    """L0 写入 SGME（/v1/append，agent key）。

    429 限流退避重试：解析 retry_after_sec（默认 10s），最多重试 max_retries 次；
    其他非 200 立即失败（服务端幂等兜底，重跑脚本即可补漏）。
    """
    cli = _http()
    if cli is None:
        return False
    try:
        for attempt in range(1, max_retries + 1):
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
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                retry_after = 10
                try:
                    details = r.json().get("error", {}).get("details", {})
                    retry_after = int(details.get("retry_after_sec", 10))
                except Exception:
                    pass
                if attempt < max_retries:
                    print(f"  append 限流(429)，{retry_after}s 后重试（{attempt}/{max_retries}）")
                    time.sleep(retry_after)
                    continue
            print(f"  append 失败: {session_key} HTTP {r.status_code}")
            return False
        return False
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
    """查 NAS SGME 已有的 dsh 会话 key（幂等去重，Admin Key 分页拉取）。

    历史教训（T-90）：原实现查本地项目库（raw_files 恒为空），导致每次全量
    重跑 141 个会话全部重新 append（浪费请求 + 撞 429 限流）。
    改为查 NAS /v1/admin/sessions?agent_id=dsh（生产真相源），重跑真正跳过已导入。
    查询失败回退空集合（服务端 append 幂等仍兜底，不重复落盘）。
    """
    cli = _http()
    if cli is None:
        return set()
    keys: set[str] = set()
    try:
        page = 1
        while True:
            r = cli.get(
                f"{_BASE_URL}/v1/admin/sessions",
                params={"agent_id": _AGENT_ID, "page": page, "limit": 100},
                headers={"X-API-Key": _ADMIN_KEY},
            )
            if r.status_code != 200:
                break
            body = r.json()
            items = body.get("items") or []
            for it in items:
                sk = it.get("session_key", "")
                if sk.startswith("dsh-"):
                    keys.add(sk)
            total = int(body.get("total", 0))
            if page * 100 >= total or not items:
                break
            page += 1
    except Exception as e:
        print(f"查已有会话失败（回退空集合，服务端幂等兜底）: {e}")
    finally:
        cli.close()
    return keys


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="dsh 历史会话全量导入 SGME")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--no-refine", action="store_true", help="导入后不触发提炼")
    args = parser.parse_args()

    existing = get_existing_session_keys()
    files = discover_sessions()
    todo = [f for f in files if f"dsh-{f.parent.name}" not in existing]

    print(f"发现 {len(files)} 个会话，待导入 {len(todo)} 个（已存在 {len(existing)} 个跳过）")
    if not todo:
        print("无待导入会话")
        return 0
    if args.dry_run:
        for f in todo[: args.limit or len(todo)]:
            msgs = parse_session_file(f)
            print(f"  [待导入] {f.parent.parent.name}/{f.parent.name}（消息 {len(msgs)} 条）")
        print("dry-run 结束（未写入）")
        return 0

    ok = fail = 0
    t0 = time.time()
    for i, f in enumerate(todo[: args.limit or len(todo)], 1):
        try:
            messages = parse_session_file(f)
            if not messages:
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（无消息）: {f.parent.name}")
                continue
            l0_text = to_l0(messages)
            session_key = f"dsh-{f.parent.name}"
            started_at = messages[0].get("ts", "") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if append_to_sgme(l0_text, session_key, started_at):
                ok += 1
            else:
                fail += 1
                print(f"[{i}/{len(todo)}] 写入失败: {f.parent.name}")
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(todo)}] 异常 {f.parent.name}: {e}")
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
