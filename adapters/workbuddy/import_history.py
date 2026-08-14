"""WorkBuddy 历史会话全量/批量导入（对标 adapters/trae/import_history.py）。

适配层职责（架构 v0.6 §8）：每个 Agent 适配器提供「会话导入」专用方法——
格式解析在适配层收敛，SGME 只收标准 L0。

本模块：发现 WorkBuddy 存量会话（~/.workbuddy/projects/*/*.jsonl）
→ 幂等 append L0 → 触发 trigger_async 批量提炼。agent_id=workbuddy。

WorkBuddy 无 SessionEnd hook（对标 trae 的 MCP 模式），故为纯离线批量导入：
- 默认全量导入
- --oldest N    ：只导入最早的 N 个会话（按会话首条消息时间戳升序）
- --session uuid：只导入指定会话（便于单会话回归/测试）
- --limit/--dry-run/--no-refine

安全设计：
- 幂等：session_key 已在 raw_files 的记录跳过（重跑安全）
- 只读 WorkBuddy 会话 + 只写 SGME L0——不删不改任何原件

用法：
  .venv/Scripts/python.exe adapters/workbuddy/import_history.py [--oldest N] [--dry-run]
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))      # adapters/workbuddy

import bridge  # noqa: E402
from sgme.data.db import get_session_conn  # noqa: E402


def _ensure_admin_key() -> None:
    """提炼触发走 HTTP 管理端点，需 SGME_ADMIN_KEY。

    优先取进程环境变量；缺失时从项目 config/.env 兜底读取（setdefault 不覆盖显式配置）。
    """
    if os.environ.get("SGME_ADMIN_KEY"):
        return
    cfg_env = Path(__file__).resolve().parents[2] / "config" / ".env"
    if not cfg_env.exists():
        return
    for line in cfg_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "SGME_ADMIN_KEY":
            os.environ.setdefault("SGME_ADMIN_KEY", v.strip())
            return


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WorkBuddy 历史会话导入 SGME")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    parser.add_argument("--oldest", type=int, default=0, help="只导入最早的 N 个会话（按首条消息时间升序）")
    parser.add_argument("--session", type=str, default="", help="只导入指定 session uuid")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--no-refine", action="store_true", help="导入后不触发提炼")
    args = parser.parse_args()

    # 幂等检查：已导入的 session_key 跳过
    session_conn = get_session_conn()
    existing = {
        r[0] for r in session_conn.execute(
            "SELECT session_key FROM raw_files WHERE session_key LIKE 'workbuddy-%'"
        ).fetchall()
    }
    session_conn.close()

    files = bridge.discover_sessions()
    seen_stems: set[str] = set()
    todo: list[Path] = []
    for f in files:
        if f.stem in seen_stems:
            continue
        seen_stems.add(f.stem)
        if f"workbuddy-{f.stem}" in existing:
            continue
        if args.session and f.stem != args.session:
            continue
        todo.append(f)

    # --oldest N：按会话首条消息时间戳升序取最早 N 个
    if args.oldest:
        todo.sort(key=lambda p: bridge.session_started_at(p))
        todo = todo[: args.oldest]

    print(f"发现 {len(files)} 个 jsonl，待导入 {len(todo)} 个（已存在 {len(existing)} 个跳过）")
    if args.dry_run:
        for f in todo[: args.limit or len(todo)]:
            print(f"  [待导入] {f}")
        print("dry-run 结束（未写入）")
        return 0

    ok = fail = 0
    t0 = time.time()
    for i, f in enumerate(todo[: args.limit or len(todo)], 1):
        try:
            messages = bridge.parse_workbuddy_jsonl(f)
            if not messages:
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（无消息）: {f.name}")
                continue
            l0_text = bridge.to_l0(messages)
            if not l0_text.strip():
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（L0 为空）: {f.name}")
                continue
            session_key = f"workbuddy-{f.stem}"
            started_at = messages[0].get("ts")
            if bridge.append_to_sgme(l0_text, session_key, started_at):
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
        _ensure_admin_key()
        if bridge.trigger_refine(limit=50, key=os.environ.get("SGME_ADMIN_KEY")):
            print("已触发批量提炼（Gateway 后台执行，可用 /v1/admin/refine 查询进度）")
        else:
            print("⚠️ 提炼触发失败——导入数据在 L0 等待，可重跑本脚本或手动触发")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
