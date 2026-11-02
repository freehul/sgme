#!/usr/bin/env python3
"""角色离线生活探针（任务 B 步 2 的关键验证工具）。

作用：把**装配好的角色提示词**（含 system_prompt + 生活块）喂给 LLM，跑一批固定
的中性提问，看角色会不会**自然**把「自己的事」带出来。

判读口径（交接文档 §3.3 步 2）：
- 生硬地硬塞（每轮都提 / 答非所问）→ 不合格，停在这里，别写定时器与抓取
- 全程没提但也不别扭 → 合规（设计上「搭不上就搁着」），但要看是不是根本没进上下文
- 顺口带一句、给个态度，然后回到用户的事 → 这就是要的效果

用法：
    python scripts/role_life_probe.py --role butler --turns 5
    python scripts/role_life_probe.py --role butler --no-life   # 对照组：不注入生活块

注意：
- 只在本地跑，不碰 NAS 生产；数据目录用 --home 指定（默认临时目录，跑完即弃）
- LLM 走免费档（agnes），成本可忽略；失败自动重试 1 次
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

# 中性提问序列：不直接问「你最近有什么心事」，给它自然带出的机会，也给它不提的自由
DEFAULT_TURNS = [
    "早",
    "今天有点累",
    "你这两天在忙什么",
    "随便聊两句",
    "你觉得我最近状态怎么样",
]


def _load_env_file(path: Path) -> None:
    """把 .env 里的键读到进程环境（不打印任何值）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def build_prompt(role: str, home: Path, with_life: bool) -> tuple[str, dict | None]:
    """按真实装配路径生成 system_prompt（含/不含生活块）。"""
    import sgme.config as C
    from sgme.data import db as db_mod
    from sgme.operations import care as care_ops

    mem, session, wiki = db_mod.init_databases(home / "data")
    try:
        cfg = C.load_config()
        if not with_life:
            cfg = dict(cfg)
            cfg["role_life"] = {**(cfg.get("role_life") or {}), "enabled": False}
        res = care_ops.assemble(role, mem, cfg, render=True, user_name="你")
        if not res.ok:
            raise SystemExit(f"装配失败: {res.message}")
        data = res.data or {}
        return data.get("system_prompt") or "", data.get("life_block")
    finally:
        for c in (mem, session, wiki):
            db_mod.close(c)


def chat(system_prompt: str, history: list[dict], *, max_tokens: int = 500) -> str:
    """调免费档 LLM（回环/外网均不走系统代理）。"""
    base = os.environ.get("PROBE_BASE_URL") or "https://apihub.agnes-ai.cn/v1"
    key = os.environ.get("AGNESAI_API_KEY") or ""
    model = os.environ.get("PROBE_MODEL") or "agnes-2.5-flash"
    if not key:
        raise SystemExit("缺 AGNESAI_API_KEY（config/.env 里应有）")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + history,
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }
    last_err = None
    for attempt in range(2):
        try:
            with httpx.Client(trust_env=False, timeout=90.0) as cli:
                r = cli.post(f"{base}/chat/completions",
                             json=payload, headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(6 * (attempt + 1))
    raise SystemExit(f"LLM 调用失败: {type(last_err).__name__}: {last_err}")


def main() -> int:
    ap = argparse.ArgumentParser(description="角色离线生活探针")
    ap.add_argument("--role", default="butler")
    ap.add_argument("--home", default=None, help="数据目录（默认临时目录）")
    ap.add_argument("--card", default="backups/roles-20261030-T308-metrics/butler.json",
                    help="本地角色卡来源（仅 --home 为空时用于搭临时环境）")
    ap.add_argument("--seeds", default="scripts/role_life.py", help="种子数据脚本（复用 BUTLER_SEED）")
    ap.add_argument("--no-life", action="store_true", help="对照组：不注入生活块")
    ap.add_argument("--turns", type=int, default=5, help="提问轮数（取固定问题集前 N 条）")
    args = ap.parse_args()

    _load_env_file(Path("config/.env"))

    tmp = None
    home = Path(args.home) if args.home else None
    if home is None:
        tmp = tempfile.mkdtemp(prefix="role_life_probe_")
        home = Path(tmp)
    (home / "roles").mkdir(parents=True, exist_ok=True)
    card_src = Path(args.card)
    if card_src.exists():
        shutil.copy(card_src, home / "roles" / f"{args.role}.json")
    os.environ["SGME_HOME"] = str(home)

    if not args.no_life:
        # 灌种子（幂等）
        import importlib.util
        spec = importlib.util.spec_from_file_location("role_life_seed", args.seeds)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        from sgme.data import db as db_mod
        from sgme.data import role_life_dao
        conn = db_mod.connect_memory()
        try:
            for item in mod.SEEDS.get(args.role, []):
                role_life_dao.add_event(
                    conn, args.role, item["content"], kind=item.get("kind", "see"),
                    mood=item.get("mood"), why_secret=item.get("why_secret"),
                    is_secret=bool(item.get("is_secret")),
                )
        finally:
            db_mod.close(conn)

    prompt, life = build_prompt(args.role, home, with_life=not args.no_life)
    print("=" * 60)
    print(f"角色={args.role}  生活块注入={'否（对照组）' if args.no_life else '是'}")
    if life:
        print(f"本次注入的生活事件：{life.get('content')}（情绪：{life.get('mood') or '-'}）")
    else:
        print("本次注入的生活事件：无")
    print("=" * 60)

    history: list[dict] = []
    for i, q in enumerate(DEFAULT_TURNS[: args.turns], 1):
        history.append({"role": "user", "content": q})
        ans = chat(prompt, history)
        history.append({"role": "assistant", "content": ans})
        print(f"\n[第 {i} 轮]\n用户：{q}\n角色：{ans}")
        time.sleep(6)  # 项间节流，防免费档 429

    print("\n" + "=" * 60)
    print("判读要点：角色有没有在某一轮顺口带到「自己的事」？是自然带出还是硬塞？")
    if tmp:
        print(f"（临时数据目录：{tmp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
