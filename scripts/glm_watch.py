# -*- coding: utf-8 -*-
"""赠额窗口看门狗：等上游恢复，恢复即自动点火（容器内常驻）。

背景（2026-11-02 夜）：23:00 额度开窗，但 ZCode 上游从 23:30 起持续返回
502/503/ECONNRESET，magpie 到上游这一段自己不通，额度充足也用不了。
人工盯着没意义，改成让脚本自己等：

1. 每 5 分钟用最小请求探一次赠额模型（16 个 token，成本可忽略）
2. 连续 3 次成功 → 判定上游恢复 → 拉起并行提炼调度器
3. 到截止时刻仍未恢复 → 记录并退出（不空转）

落盘：/data/tmp/glm_watch.log；状态：/data/tmp/glm_watch_state.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# 网关地址一律从环境变量取（数据卫生：不在代码里写死内网地址）
GW = os.environ.get("GLM_GATEWAY", "http://127.0.0.1:3425")
MODEL = os.environ.get("GLM_TARGET_MODEL", "zcode/GLM-5.3-Flash-Trial")
INTERVAL = int(os.environ.get("WATCH_INTERVAL", "300"))
NEED_OK = int(os.environ.get("WATCH_NEED_OK", "3"))
STATE = Path(os.environ.get("WATCH_STATE", "/data/tmp/glm_watch_state.json"))
SCHEDULER = os.environ.get("WATCH_SCHEDULER", "/data/glm_parallel_refine.py")
UNTIL = os.environ.get("WATCH_UNTIL", "08:30")


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def probe_once() -> tuple[bool, str]:
    """打一个最小请求，判断上游是否真的能用。"""
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "max_tokens": 16,
    }
    req = urllib.request.Request(
        f"{GW}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local-gateway"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            r.read()
        return True, f"200 {time.time() - t0:.1f}s"
    except Exception as e:  # noqa: BLE001
        detail = ""
        reader = getattr(e, "read", None)
        if callable(reader):
            try:
                detail = reader().decode()[:80]
            except Exception:  # noqa: BLE001
                pass
        return False, f"{type(e).__name__} {time.time() - t0:.1f}s {detail}"


def deadline_ts() -> float:
    hh, mm = UNTIL.split(":")
    now = datetime.now()
    t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def main() -> int:
    dl = deadline_ts()
    log(f"看门狗启动：每 {INTERVAL}s 探一次，连续 {NEED_OK} 次成功即点火；截止 {UNTIL}")
    streak = 0
    attempts = 0
    while time.time() < dl:
        attempts += 1
        ok, detail = probe_once()
        streak = streak + 1 if ok else 0
        log(f"第 {attempts} 次探测：{'成功' if ok else '失败'}（{detail}），连续成功 {streak}/{NEED_OK}")
        STATE.write_text(json.dumps({
            "attempts": attempts, "streak": streak, "last_ok": ok,
            "last_detail": detail, "at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if streak >= NEED_OK:
            log("上游已恢复，拉起并行提炼调度器")
            env = dict(os.environ, PYTHONPATH="/app")
            rc = subprocess.run(
                [sys.executable, SCHEDULER, "run", "--parallel", "9",
                 "--until", UNTIL, "--stall-min", "6"],
                env=env,
            ).returncode
            log(f"调度器退出，rc={rc}")
            return rc
        # 睡到下一次探测，但不超过截止时刻
        remain = dl - time.time()
        if remain <= 0:
            break
        time.sleep(min(INTERVAL, remain))
    log("到截止时刻仍未恢复，看门狗退出")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
