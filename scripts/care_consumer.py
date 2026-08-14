# -*- coding: utf-8 -*-
"""scripts/care_consumer.py：Care Engine 关怀消费方（T-38，ST-25；T-58 降级）。

消费方侧（**非 SGME 内核**——SGME 只发信号不做决策，架构铁律）：
由 cron（确定性定时）/ heartbeat（条件式巡检）定时调用本脚本：

1. 触发 SGME 关怀扫描（POST /v1/admin/care/scan，幂等）
2. 拉取未消费关怀信号（GET /v1/admin/care/signals?unconsumed_only=true）
3. **幂等去重**（本地状态文件 data/care/consumer_state.json，last_notified_at 模式
   ——防 SGME 消费标记失败时的重复通知）
4. 输出待关怀事项（stdout JSON 行；空 = 无待办，静默）
5. 兜底消费（--consume 显式开启，POST .../consume 原子认领）

T-58 降级（2026-08-14）：消费权归「当前活跃 agent」（谁消费谁标记），
本脚本默认**只读**（scan + 输出，不 consume）；仅当无活跃 agent 时，
cron 加 --consume 显式开启兜底消费。

用法（在项目根，.venv 下）：
  python scripts/care_consumer.py             # 只读（默认，heartbeat/cron 巡检用）
  python scripts/care_consumer.py --consume   # 兜底消费（无活跃 agent 的 cron 用）

key 从 config/.env 读 SGME_AGENT_KEY（不落盘、不出现在对话）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
STATE_DIR = BASE / "data" / "care"
STATE_FILE = STATE_DIR / "consumer_state.json"
BASE_URL = "http://127.0.0.1:9910"


def _load_key() -> str:
    """从 config/.env 读 SGME_AGENT_KEY（不打印）。"""
    env_path = BASE / "config" / ".env"
    if not env_path.exists():
        return os.environ.get("SGME_AGENT_KEY", "")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("SGME_AGENT_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("SGME_AGENT_KEY", "")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Care Engine 关怀消费方")
    parser.add_argument("--check-only", action="store_true",
                        help="只拉取不消费（等价默认，向后兼容）")
    parser.add_argument("--consume", action="store_true",
                        help="显式开启兜底消费（无活跃 agent 场景的 cron 用）")
    # 默认 []（不吞宿主 sys.argv）：脚本入口显式传 sys.argv[1:]
    args = parser.parse_args(argv if argv is not None else [])

    key = _load_key()
    if not key:
        print("SGME_AGENT_KEY 未配置（config/.env），跳过", file=sys.stderr)
        return 0
    headers = {"X-API-Key": key}

    try:
        # 1. 触发扫描（幂等：uuid5 去重，重复扫描零重复事件）
        r = requests.post(f"{BASE_URL}/v1/admin/care/scan", headers=headers, timeout=10)
        if r.status_code != 200:
            print(f"关怀扫描失败: {r.status_code} {r.text[:120]}", file=sys.stderr)
            return 1

        # 2. 拉取未消费信号
        r = requests.get(
            f"{BASE_URL}/v1/admin/care/signals",
            params={"unconsumed_only": "true", "limit": 20},
            headers=headers, timeout=10,
        )
        if r.status_code != 200:
            print(f"关怀信号拉取失败: {r.status_code} {r.text[:120]}", file=sys.stderr)
            return 1
        signals = r.json().get("signals", [])
    except requests.RequestException as e:
        print(f"SGME 不可达: {e}", file=sys.stderr)
        return 0  # 静默降级（不阻塞宿主流程）

    if not signals:
        return 0  # 无待办，静默

    state = _load_state()
    pending: list[dict] = []
    for s in signals:
        eid = s["event_id"]
        # 幂等去重：本地状态已通知过 → 跳过（防 consume 失败后的重复）
        if state.get(eid):
            continue
        pending.append({
            "event_id": eid,
            "type": s["type"],
            "ts": s["ts"],
            "payload": json.loads(s.get("payload") or "{}"),
        })

    if not pending:
        return 0

    # 4. 输出待关怀事项（JSON 行，供上层 agent 决策话术）
    for p in pending:
        print(json.dumps(p, ensure_ascii=False))

    # 5. 兜底消费（--consume 显式开启；默认只读，消费权归当前活跃 agent）
    if args.consume:
        for p in pending:
            try:
                r = requests.post(
                    f"{BASE_URL}/v1/admin/care/signals/{p['event_id']}/consume",
                    headers=headers, timeout=10,
                )
                if r.status_code in (200, 409):
                    # 200=本次认领成功；409=已被活跃 agent 消费（原子抢失败）——
                    # 两者都视为「已处理」，记录本地状态避免下次重复拉取
                    state[p["event_id"]] = p["ts"]
                else:
                    print(f"消费标记失败 {p['event_id']}: {r.status_code}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"消费标记失败 {p['event_id']}: {e}", file=sys.stderr)
        _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
