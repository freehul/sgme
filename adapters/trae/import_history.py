"""Trae 历史会话全量导入（适配层专用方法，对称 adapters/reasonix/import_history.py）。

适配层职责（架构 v0.6 §8）：每个 Agent 适配器提供「会话导入」专用方法——
不同 Agent 会话保存形式不同（Hermes state.db / Reasonix jsonl / Trae session_memory jsonl），
格式解析在适配层收敛，SGME 只收标准 L0。

本模块：发现 Trae 存量会话（~/.trae-cn/memory/projects/*/*/session_memory_*.jsonl）
→ 幂等 append L0 → 触发 trigger_async 批量提炼。

Trae 数据源特点：
- 不是原始会话，是助手已提炼的"摘要"（intent/actions/outcome/learned）
- 每行是一个 JSON 对象，对应一轮对话的提炼结果
- 高质量结构化素材，比原始消息更适合喂给 L1 提炼

安全设计：
- 幂等：session_key 已在 raw_files 的记录跳过（重跑安全）
- 用文件 stem 去重（同一 jsonl 可能被多个项目目录软链接引用）
- 只读 Trae memory 目录 + 只写 SGME L0——不删不改任何原件
- --limit 试跑；--dry-run 只统计不写入；--no-refine 导入后不触发提炼

用法：
  .venv/Scripts/python.exe adapters/trae/import_history.py [--limit N] [--dry-run]
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))  # adapters/trae

import bridge  # noqa: E402
from sgme.data.db import get_session_conn  # noqa: E402


def discover_sessions() -> list[Path]:
    """扫描 Trae 所有项目的 session_memory jsonl。

    目录结构：~/.trae-cn/memory/projects/<proj>/<date>/session_memory_*.jsonl
    """
    base = Path(os.environ["USERPROFILE"]) / ".trae-cn" / "memory" / "projects"
    if not base.exists():
        return []
    files: list[Path] = []
    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        for date_dir in proj_dir.iterdir():
            if not date_dir.is_dir():
                continue
            files.extend(date_dir.glob("session_memory_*.jsonl"))
    return files


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Trae 历史会话全量导入 SGME")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--no-refine", action="store_true", help="导入后不触发提炼")
    args = parser.parse_args()

    # 幂等检查：已导入的 session_key 跳过
    session_conn = get_session_conn()
    existing = {
        r[0] for r in session_conn.execute(
            "SELECT session_key FROM raw_files WHERE session_key LIKE 'trae-%'"
        ).fetchall()
    }
    session_conn.close()

    files = sorted(discover_sessions(), key=lambda p: str(p))
    # 用 stem 去重（同一 jsonl 可能被多个项目目录引用，Trae 软链接机制）
    seen_stems: set[str] = set()
    todo: list[Path] = []
    for f in files:
        if f.stem in seen_stems:
            continue
        seen_stems.add(f.stem)
        if f"trae-{f.stem}" in existing:
            continue
        todo.append(f)

    print(f"发现 {len(files)} 个 jsonl（去重后 {len(seen_stems)} 个），"
          f"待导入 {len(todo)} 个（已存在 {len(existing)} 个跳过）")
    if args.dry_run:
        for f in todo[: args.limit or len(todo)]:
            try:
                rel = f.relative_to(f.parents[2])
            except ValueError:
                rel = f
            print(f"  [待导入] {rel}")
        print("dry-run 结束（未写入）")
        return 0

    ok = fail = 0
    t0 = time.time()
    for i, f in enumerate(todo[: args.limit or len(todo)], 1):
        try:
            records = bridge.parse_trae_jsonl(f)
            if not records:
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（无记录）: {f.name}")
                continue
            l0_text = bridge.to_l0(records)
            if not l0_text.strip():
                fail += 1
                print(f"[{i}/{len(todo)}] 跳过（L0 为空）: {f.name}")
                continue
            session_key = f"trae-{f.stem}"
            started_at = bridge._norm_ts(records[0].get("message_summary_time"))
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
        if bridge.trigger_refine(limit=50):
            print("已触发批量提炼（Gateway 后台执行，可用 /v1/admin/refine 查询进度）")
        else:
            print("⚠️ 提炼触发失败——导入数据在 L0 等待，可重跑本脚本或手动触发")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
