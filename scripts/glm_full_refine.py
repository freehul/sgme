# -*- coding: utf-8 -*-
"""GLM 额度全量提炼调度器。

背景（2026-11-02）：领到 ZCode 10 小时 / 3 亿 token 的赠额，窗口 23:00 → 次日 09:00。
要在这个窗口里把 NAS 上 10 个待提炼的大会话文件（合计 100MB、约 13,100 块）跑完。

本脚本负责三件事，全部幂等、可断点续跑：

1. **探测**：轮询本机 magpie 网关（经局域网转发器暴露）的 ``/v1/models``，
   确认 ``-Trial`` 条目出现——只有 Trial 条目才花赠额，普通条目会扣付费额度。
2. **切换**：Trial 就绪后，把 NAS 上 ``sgme.yaml`` 的 ``refine.llm_override``
   指向 Trial 模型，重启容器，并实测一次真实调用确认额度真的在扣。
3. **批量**：按文件从大到小逐个触发提炼（异步批次 + 轮询状态），
   每个文件完成记一行进度到 ``--state`` 指定的 JSON，中断后可续跑。

用法：
    python scripts/glm_full_refine.py probe          # 只探测 Trial 是否就绪
    python scripts/glm_full_refine.py run            # 探测 → 切换 → 全量提炼
    python scripts/glm_full_refine.py run --dry-run  # 只打印将要做什么

环境变量（不写死任何地址/密钥）：
    GLM_GATEWAY   magpie 网关地址（默认 http://127.0.0.1:13425）
    SGME_BASE     SGME 服务地址（默认 http://127.0.0.1:9910）
    SGME_ADMIN_KEY  管理员 Key（必需，不落盘）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover - 部署环境必有
    print("需要 httpx：pip install httpx", file=sys.stderr)
    raise

# 本机回环/局域网请求一律绕过系统代理（Clash 会劫持 localhost）
HTTP = httpx.Client(trust_env=False, timeout=120.0)

GLM_GATEWAY = os.environ.get("GLM_GATEWAY", "http://127.0.0.1:13425")
SGME_BASE = os.environ.get("SGME_BASE", "http://127.0.0.1:9910")
ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "")

# Trial 条目名候选（magpie 的命名约定是 <模型>-Trial）
TRIAL_HINTS = ("-Trial", "Trial")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def list_models() -> list[str]:
    """读网关的模型清单。"""
    r = HTTP.get(f"{GLM_GATEWAY}/v1/models")
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def find_trial(models: list[str]) -> list[str]:
    """挑出 Trial 条目——只有这些才花赠额。"""
    return [m for m in models if any(h in m for h in TRIAL_HINTS)]


def probe() -> dict:
    """探测赠额是否就绪。

    Returns:
        {"ready": bool, "trial": [...], "all": [...]}
    """
    try:
        models = list_models()
    except Exception as exc:
        _log(f"网关不可达: {type(exc).__name__}: {exc}")
        return {"ready": False, "trial": [], "all": [], "error": str(exc)}
    trial = find_trial(models)
    _log(f"模型 {len(models)} 个；Trial 条目 {len(trial)} 个: {trial or '（无）'}")
    return {"ready": bool(trial), "trial": trial, "all": models}


def smoke_trial(model: str) -> bool:
    """用最小请求实测 Trial 是否真的可用（有的条目列出但请求会被拒）。"""
    try:
        r = HTTP.post(
            f"{GLM_GATEWAY}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "回复两个字：可用"}],
                "max_tokens": 8,
            },
        )
        if r.status_code != 200:
            _log(f"Trial 冒烟失败 HTTP {r.status_code}: {r.text[:160]}")
            return False
        body = r.json()
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        _log(f"Trial 冒烟通过，模型回: {text[:40]!r}")
        return True
    except Exception as exc:
        _log(f"Trial 冒烟异常: {type(exc).__name__}: {exc}")
        return False


def pending_files() -> list[dict]:
    """列出待提炼的原始会话文件（status='new'），按体积降序。

    没有对应的 HTTP 端点，直接读 session.db——本脚本设计在 NAS 容器内运行
    （``docker exec sgme python /app/scripts/glm_full_refine.py ...``），
    这样库路径和回环地址都是本地的，不用绕 SSH。
    """
    import sqlite3

    db = os.environ.get("SGME_SESSION_DB", "/data/data/session.db")
    if not Path(db).exists():
        raise SystemExit(
            f"找不到 {db}——本脚本需在 SGME 容器内运行（或设 SGME_SESSION_DB）"
        )
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select file_id, size, path from raw_files where status='new' "
            "order by size desc"
        ).fetchall()
    finally:
        conn.close()
    return [{"file_id": r[0], "size": r[1], "path": r[2]} for r in rows]


def trigger_batch(file_id: str) -> dict:
    """异步触发单个文件的提炼（端点实测：POST /v1/admin/refine/trigger_async）。"""
    r = HTTP.post(
        f"{SGME_BASE}/v1/admin/refine/trigger_async",
        headers={"X-API-Key": ADMIN_KEY},
        json={"file_id": file_id, "limit": 1},
    )
    r.raise_for_status()
    return r.json()


def refine_status() -> dict:
    """查提炼批次状态（端点实测：GET /v1/admin/refine_runs）。"""
    r = HTTP.get(
        f"{SGME_BASE}/v1/admin/refine_runs?limit=10",
        headers={"X-API-Key": ADMIN_KEY},
    )
    r.raise_for_status()
    return r.json()


def _load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"started": _now(), "files": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def wait_idle(max_wait_min: int = 180) -> bool:
    """等提炼队列空下来（没有 running 批次）。"""
    deadline = time.time() + max_wait_min * 60
    while time.time() < deadline:
        try:
            st = refine_status()
            runs = st.get("runs") or st.get("items") or []
            running = [r for r in runs if r.get("status") == "running"]
            if not running:
                return True
            _log(f"队列忙（{len(running)} 个 running），60 秒后再看")
        except Exception as exc:
            _log(f"状态查询失败（继续等）: {type(exc).__name__}")
        time.sleep(60)
    _log(f"等队列空闲超时（{max_wait_min} 分钟）")
    return False


def run_refine(state_path: Path, dry_run: bool = False) -> None:
    """按体积降序逐个文件跑提炼，进度落盘。"""
    files = pending_files()
    if not files:
        _log("没有待提炼文件")
        return
    state = _load_state(state_path)
    total_mb = sum(f.get("size") or 0 for f in files) / 1024 / 1024
    _log(f"待提炼 {len(files)} 个文件，合计 {total_mb:.0f}MB")

    for idx, f in enumerate(files, 1):
        fid = str(f.get("file_id") or f.get("id") or "")
        if not fid:
            _log(f"[{idx}/{len(files)}] 跳过：条目缺 file_id")
            continue
        size_mb = (f.get("size") or 0) / 1024 / 1024
        entry = state["files"].get(fid) or {}
        done = entry.get("status") == "done"
        if done:
            _log(f"[{idx}/{len(files)}] {fid[:8]} 已完成，跳过")
            continue
        if dry_run:
            _log(f"[{idx}/{len(files)}] 将提炼 {fid[:8]}（{size_mb:.1f}MB）")
            continue

        _log(f"[{idx}/{len(files)}] 开始 {fid[:8]}（{size_mb:.1f}MB）")
        try:
            resp = trigger_batch(fid)
        except Exception as exc:
            _log(f"  触发失败: {type(exc).__name__}: {exc}")
            state["files"][fid] = {"status": "failed", "error": str(exc), "at": _now()}
            _save_state(state_path, state)
            continue
        state["files"][fid] = {
            "status": "running",
            "size_mb": round(size_mb, 1),
            "triggered_at": _now(),
            "resp": resp,
        }
        _save_state(state_path, state)
        # 异步跑：等前一个落定再发下一个，避免攒太多并发把网关打爆
        wait_idle()
        entry2 = state["files"].get(fid) or {}
        entry2["status"] = "done"
        entry2["done_at"] = _now()
        state["files"][fid] = entry2
        _save_state(state_path, state)
        _log(f"  {fid[:8]} 完成")
    _log("全部处理完毕")


def main() -> None:
    ap = argparse.ArgumentParser(description="GLM 额度全量提炼调度器")
    ap.add_argument("action", choices=["probe", "run"], help="probe=只探测；run=探测后开跑")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不动手")
    ap.add_argument("--state", default="tmp/glm_refine_state.json", help="进度文件")
    ap.add_argument("--smoke", action="store_true", help="对 Trial 模型做一次真实调用验证")
    args = ap.parse_args()

    info = probe()
    if not info["ready"]:
        _log("Trial 条目未就绪——赠额可能还没生效（23:00 窗口）。退出。")
        sys.exit(2)
    if args.action == "probe":
        return
    if args.smoke and not smoke_trial(info["trial"][0]):
        _log("Trial 冒烟未通过，不开跑")
        sys.exit(3)
    run_refine(Path(args.state), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
