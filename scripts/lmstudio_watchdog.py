# -*- coding: utf-8 -*-
"""LM Studio 端点守护：探活 → 缺模型就装载 → 记日志（长跑评测的保险丝）。

为什么需要（2026-09-11 实锤）：
    refined 臂冒烟跑到判分阶段时，PC 上的 LM Studio **服务端在运行中静默停止**
    （服务端日志戛然而止、端口不再监听，事后无崩溃记录），评测随即全量报
    10061；同一天系统还因「空闲 1 小时 = 休眠」睡过一次。多天跑必须有人看门。

做法（对外只调官方 CLI，不碰 GUI）：
    1) 用 `trust_env=False` 的 httpx 探 `/v1/models`（项目铁律：防代理劫持）；
    2) 不可答 → `lms server start` 等就绪；
    3) 用 `lms ps --json` 对比「应常驻模型」清单，缺失的按预设参数 `lms load ... -y`。

用法：
    # 单次（交给计划任务，每 5 分钟一次）
    python scripts/lmstudio_watchdog.py --once
    # 常驻循环（长跑期间另开一个窗口/后台进程用）
    python scripts/lmstudio_watchdog.py --loop --interval 120
    # 换端点/换模型参数
    python scripts/lmstudio_watchdog.py --base-url http://192.168.10.130:8123/v1 \
        --keep "modelA|-c 131072 --parallel 4" --keep "modelB|--gpu max"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "logs" / "lmstudio_watchdog.log"
DEFAULT_LMS = Path.home() / ".lmstudio" / "bin" / "lms.exe"

# 应常驻的模型（refined 臂评测用；--keep 可覆盖）
DEFAULT_KEEP = [
    ("qwen3.8-9b-distill", "-c 131072 --parallel 4"),
    ("text-embedding-bge-m3-legal-euro-r7", "--gpu max"),
]


def missing_models(loaded: set[str], wanted: list[str]) -> list[str]:
    """返回应装载但当前未加载的模型（纯函数，便于单测）。"""
    return [w for w in wanted if w not in loaded]


def probe(base_url: str, timeout_s: float = 8.0) -> bool:
    """端点是否可答（trust_env=False：不读系统代理）。"""
    import httpx

    try:
        with httpx.Client(timeout=timeout_s, trust_env=False) as cli:
            r = cli.get(f"{base_url.rstrip('/')}/models")
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def loaded_ids(lms: Path) -> set[str]:
    """当前已加载的模型标识（lms ps --json）。"""
    try:
        out = subprocess.run([str(lms), "ps", "--json"], capture_output=True,
                             text=True, timeout=60)
        data = json.loads(out.stdout or "[]")
        return {m.get("identifier") or m.get("modelKey") for m in data if m}
    except Exception:  # noqa: BLE001
        return set()


def run_lms(lms: Path, args: list[str], timeout: int = 600) -> tuple[bool, str]:
    """跑一条 lms 子命令，返回 (成功, 摘要)。"""
    try:
        p = subprocess.run([str(lms), *args], capture_output=True, text=True, timeout=timeout)
        tail = (p.stdout or p.stderr or "").strip().splitlines()
        return p.returncode == 0, (tail[-1][:160] if tail else "")
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]


def once(lms: Path, base_url: str, keep: list[tuple[str, str]], log) -> bool:
    """执行一轮守护，返回「是否做过修复动作」。"""
    acted = False
    if not probe(base_url):
        log("端点不可答 → 拉起 LM Studio 服务")
        ok, msg = run_lms(lms, ["server", "start"], timeout=120)
        log(f"  lms server start {'成功' if ok else '失败'}：{msg}")
        for _ in range(15):  # 最多等 30 秒
            time.sleep(2)
            if probe(base_url):
                break
        acted = True

    if not probe(base_url):
        log("端点仍不可答，本轮放弃（下一轮再试）")
        return True

    loaded = loaded_ids(lms)
    for mid, load_args in keep:
        if mid not in loaded:
            log(f"模型缺失 → 装载 {mid}（{load_args}）")
            ok, msg = run_lms(lms, ["load", mid, *load_args.split(), "-y"])
            log(f"  {'成功' if ok else '失败'}：{msg}")
            acted = True
    return acted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://192.168.10.130:8123/v1")
    ap.add_argument("--lms", default=str(DEFAULT_LMS))
    ap.add_argument("--keep", action="append", default=None,
                    help='应常驻模型，格式 "模型id|装载参数"；可多次')
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--once", action="store_true", help="只跑一轮（计划任务用）")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--interval", type=int, default=120)
    a = ap.parse_args(argv)

    keep = [(k.split("|", 1)[0], k.split("|", 1)[1] if "|" in k else "--gpu max")
            for k in (a.keep or [])] or DEFAULT_KEEP
    lms = Path(a.lms)
    log_path = Path(a.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    if not lms.exists():
        log(f"⚠ 找不到 lms CLI：{lms}")
        return 2

    if a.once or not a.loop:
        acted = once(lms, a.base_url, keep, log)
        log("端点健康，无需动作" if not acted else "本轮已完成修复动作")
        return 0

    log(f"守护启动：端点={a.base_url} 间隔={a.interval}s 常驻模型={[k[0] for k in keep]}")
    while True:
        try:
            once(lms, a.base_url, keep, log)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 本轮异常：{str(e)[:160]}")
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
