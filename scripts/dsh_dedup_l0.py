"""dsh L0 重复清理（T-90 双链路去重收尾）。

背景：历史批量导入用 dsh-{目录名} 做 session_key，实时链路（sgme-bridge session-sync）
用 dsh-{首条消息毫秒}——同一逻辑会话两条链路各写一份 raw_files（实测 21/60 重复）。
本脚本识别这类重复并输出归档方案；原件永不删（归档可恢复）。

识别逻辑：
1. 拉取 NAS 全部 dsh- 前缀 key（实时链路毫秒形态 + 批量导入目录名形态）
2. 扫描本地 ~/.dsh/sessions/<workspace>/<id>/session.jsonl.zstd，
   取首条 user 消息毫秒
3. 重复判定（双 key 都命中才成立）：
   - dir_key（dsh-{目录名}）已在 NAS → 批量导入写过一份
   - ms_key（dsh-{首条毫秒}）已在 NAS → 实时链路也写过一份（增量、内容更全）
   → 两条件同时满足 = 同一逻辑会话两份 L0，目录名形态为重复
4. 归档：把重复 raw 文件移入 raw/.archive/ 并标记 raw_files.status='archived'
   （SGME 提炼只处理 status='new'，archived 不再提炼；可改回 new 恢复）

用法：
  .venv/Scripts/python.exe scripts/dsh_dedup_l0.py            # dry-run（默认）
  .venv/Scripts/python.exe scripts/dsh_dedup_l0.py --apply    # 输出归档命令清单（NAS 侧执行）
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "dsh"))

import import_history  # noqa: E402

_BASE_URL = import_history._BASE_URL
_DSH_HOME = import_history._DSH_HOME


def fetch_existing_keys() -> set[str]:
    """拉取 NAS 全部 dsh- 前缀 session_key（Admin Key 分页）。"""
    cli = import_history._http()
    if cli is None:
        return set()
    keys: set[str] = set()
    try:
        page = 1
        while True:
            r = cli.get(
                f"{_BASE_URL}/v1/admin/sessions",
                params={"agent_id": "dsh", "page": page, "limit": 100},
                headers={"X-API-Key": import_history._ADMIN_KEY},
            )
            if r.status_code != 200:
                break
            body = r.json()
            for it in body.get("items") or []:
                k = it.get("session_key", "")
                if k.startswith("dsh-"):
                    keys.add(k)
            total = int(body.get("total", 0))
            if page * 100 >= total or not body.get("items"):
                break
            page += 1
    except Exception as e:
        print(f"拉取已有会话失败: {e}")
    finally:
        cli.close()
    return keys


def find_duplicates(existing: set[str]) -> list[dict]:
    """扫描本地会话，识别被实时链路覆盖的目录名形态重复。"""
    ms_keys = {k for k in existing if re.match(r"^dsh-\d+$", k)}
    dups: list[dict] = []
    for sdir in sorted(_DSH_HOME.joinpath("sessions").rglob("*/session.jsonl.zstd")):
        ws = sdir.parent.parent.name
        sid = sdir.parent.name
        if import_history._is_test_workspace(ws):
            continue
        if not re.match(r"^(session-)?[0-9a-f-]{36}$", sid):
            continue  # 只处理 uuid 形态（批量导入的目录名 key 都来自这类目录）
        msgs = import_history.parse_session_file(sdir)
        first_ms = next((int(m["ms"]) for m in msgs if m.get("role") == "user" and m.get("ms")), None)
        if first_ms is None:
            continue
        ms_key = f"dsh-{first_ms}"
        dir_key = f"dsh-{sid}"
        # 双 key 都命中才算重复：目录名形态（批量导入）+ 毫秒形态（实时链路）都写过
        if dir_key in existing and ms_key in ms_keys:
            dups.append({
                "workspace": ws,
                "dir_name": sid,
                "dir_key": dir_key,
                "ms_key": ms_key,
                "msg_count": len(msgs),
            })
    return dups


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="dsh L0 重复识别（双链路 key 对齐收尾）")
    parser.add_argument("--apply", action="store_true", help="输出归档命令清单（NAS 侧执行）")
    args = parser.parse_args()

    existing = fetch_existing_keys()
    dups = find_duplicates(existing)

    ms_form = [k for k in existing if re.match(r"^dsh-\d+$", k)]
    print(f"NAS dsh 会话 key 总数: {len(existing)}（实时链路毫秒形态 {len(ms_form)}）")
    print(f"识别重复 L0: {len(dups)} 条")
    for d in dups:
        print(f"  {d['workspace']}/{d['dir_name'][:12]}… dir_key={d['dir_key']} ↔ ms_key={d['ms_key']}（{d['msg_count']} 条消息）")

    if not args.apply:
        print("dry-run 结束（未输出归档命令；加 --apply 生成 NAS 归档清单）")
        return 0

    # 归档命令清单：NAS 容器内执行（raw 文件移入 .archive + raw_files 标记 archived）
    print("\n=== NAS 归档命令（SGME 容器内执行，原件移 .archive 可恢复）===")
    print("docker exec sgme bash -c 'mkdir -p /data/raw/.archive'")
    for d in dups:
        print(
            "docker exec sgme bash -c "
            + json.dumps(
                f"mv /data/raw/{d['dir_key']}.md /data/raw/.archive/ 2>/dev/null; "
                + f"echo \"UPDATE raw_files SET status='archived' WHERE session_key='{d['dir_key']}' AND status!='archived';\" | sqlite3 /data/memory.db"
            )
        )
    print("\n说明：archived 状态不参与提炼（仅 new 会提炼）；恢复 = 移回 raw/ 并置 status='new'。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
