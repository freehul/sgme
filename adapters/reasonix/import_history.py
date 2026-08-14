"""Reasonix 历史会话全量导入（适配层专用方法，对称 adapters/hermes 的导出）。

适配层职责（架构 v0.6 §8）：每个 Agent 适配器提供「会话导入」专用方法——
不同 Agent 会话保存形式不同（Hermes state.db / Reasonix jsonl / Codex…），
格式解析在适配层收敛，SGME 只收标准 L0。

本模块：发现 Reasonix 存量会话（projects/*/sessions + archive）→ 幂等 append L0
→ 触发 trigger_async 批量提炼。agent 可自主执行（AGENTS.md 已声明）。

安全设计：
- 幂等：session_key 已在 raw_files 的记录跳过（重跑安全）
- 排除测试会话（2026-08-07 探针/端到端，避免 e2e 污染记忆池）
- 只读 Reasonix 存档 + 只写 SGME L0——不删不改任何原件
- --limit 试跑；--dry-run 只统计不写入；--no-refine 导入后不触发提炼

用法：
  .venv/Scripts/python.exe adapters/reasonix/import_history.py [--limit N] [--dry-run]
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))  # adapters/reasonix

import bridge  # noqa: E402
from sgme.data.db import get_session_conn  # noqa: E402

# 2026-08-07 探针/端到端测试会话（HOOK_TEST_OK / MEMORY_E2E_OK），排除避免污染
TEST_SESSION_PREFIXES = (
    "20260807-021051",
    "20260807-021122",
    "20260807-021137",
    "20260807-022241",
)

_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")


def discover_sessions() -> list[Path]:
    """扫描主会话 + archive 归档（排除辅助文件）。"""
    base = Path(os.environ["APPDATA"]) / "reasonix"
    files: list[Path] = []
    for proj in (base / "projects").glob("*/sessions/*.jsonl"):
        if "events" in proj.name or "ckpt" in proj.name:
            continue
        files.append(proj)
    for arch in (base / "archive").glob("*.jsonl"):
        files.append(arch)
    # 辅助文件显式排除：recovery（崩溃恢复副本）/ conflicts（冲突副本）——非独立会话
    return [f for f in files if "recovery" not in f.name and "conflicts" not in f.name]


def is_test_session(path: Path) -> bool:
    return path.stem.startswith(TEST_SESSION_PREFIXES)


def trigger_refine() -> bool:
    """导入完成后触发批量提炼（fire-and-forget，Gateway 后台执行）。"""
    try:
        import httpx
        with httpx.Client(timeout=8.0, trust_env=False) as cli:
            r = cli.post(
                f"{bridge._BASE_URL}/v1/admin/refine/trigger_async",
                json={"limit": 50},
                headers={"X-API-Key": _ADMIN_KEY},
            )
            return r.status_code == 200
    except Exception as e:
        print(f"提炼触发失败（可稍后手动触发）: {e}")
        return False


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reasonix 历史会话全量导入 SGME")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--no-refine", action="store_true", help="导入后不触发提炼")
    args = parser.parse_args()

    session_conn = get_session_conn()
    existing = {
        r[0] for r in session_conn.execute(
            "SELECT session_key FROM raw_files WHERE session_key LIKE 'reasonix-%'"
        ).fetchall()
    }
    session_conn.close()

    files = sorted(
        (f for f in discover_sessions() if not is_test_session(f)),
        key=lambda p: str(p),
    )
    todo = [f for f in files if f"reasonix-{f.stem}" not in existing]

    print(f"发现 {len(files)} 个会话（排除测试 {len(discover_sessions()) - len(files)} 个），"
          f"待导入 {len(todo)} 个（已存在 {len(existing)} 个跳过）")
    if args.dry_run:
        for f in todo[: args.limit or len(todo)]:
            print(f"  [待导入] {f.name}")
        print("dry-run 结束（未写入）")
        return 0

    ok = fail = 0
    t0 = time.time()
    for i, f in enumerate(todo[: args.limit or len(todo)], 1):
        try:
            messages = bridge.parse_session_file(f)
            if not messages:
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（无消息）: {f.name}")
                continue
            l0_text = bridge.to_l0(messages)
            session_key = f"reasonix-{f.stem}"
            started_at = messages[0].get("created_at", "")
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
