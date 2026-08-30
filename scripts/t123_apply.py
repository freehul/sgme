# -*- coding: utf-8 -*-
"""T-123 分类治理应用脚本：uncategorized → 建议分类（写侧正路）。

用法（审核 exports/t123-categorize-review.md 后执行）：
    # 预览（默认 dry-run，只打印将执行的替换，不写任何数据）
    python scripts/t123_apply.py

    # 应用全部建议
    python scripts/t123_apply.py --apply

    # 只应用指定技能 / 排除指定技能
    python scripts/t123_apply.py --apply --only apple-notes,arxiv
    python scripts/t123_apply.py --apply --exclude box,blogwatcher

设计要点：
- **写侧正路**：PUT /v1/admin/skills/{name} 覆盖源 SKILL.md（frontmatter category
  行替换）——skills.db 是源的索引投影，直改库会在下次 sync_index 被源 diff 打回；
  PUT 后 sha256 重算，索引经 sync_index 增量同步，单一真相源保持。
- 前置条件：NAS skills.db 已备份（~/sgme-backups/skills.db.bak-t123-20260830）。
- suggested=null 的条目（LLM 白名单校验未过）自动跳过，须人工改清单。
- 建议应用后跑 POST /v1/admin/skills/sync {"direction":"to_remote"} 回推 bare 仓。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUGGESTIONS_PATH = PROJECT_ROOT / "exports" / "t123-categorize-suggestions.json"

# 与 scripts/_sgme_net.py 同源：env → install.json → 默认
BASE = "http://192.168.10.10:9910"


def _load_admin_key() -> str:
    """从 config/.env 读 admin key（铁律：密钥不落码）。"""
    env_path = PROJECT_ROOT / "config" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SGME_ADMIN_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    print("未找到 SGME_ADMIN_KEY（config/.env）", file=sys.stderr)
    sys.exit(1)


def _replace_category(content: str, new_cat: str) -> tuple[str, bool]:
    """替换 frontmatter 中的 category 行。返回 (新内容, 是否命中)。"""
    pat = re.compile(r"(^category:\s*)uncategorized\s*$", re.MULTILINE)
    new_content, n = pat.subn(rf"\g<1>{new_cat}", content, count=1)
    return new_content, n == 1


def main() -> None:
    ap = argparse.ArgumentParser(description="T-123 分类治理应用（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真实应用（缺省 dry-run）")
    ap.add_argument("--only", help="逗号分隔的技能名白名单")
    ap.add_argument("--exclude", help="逗号分隔的技能名排除")
    args = ap.parse_args()

    data = json.loads(SUGGESTIONS_PATH.read_text(encoding="utf-8"))
    items = data["suggestions"]
    if args.only:
        allow = set(args.only.split(","))
        items = [x for x in items if x["name"] in allow]
    if args.exclude:
        deny = set(args.exclude.split(","))
        items = [x for x in items if x["name"] not in deny]

    pending = [x for x in items if x.get("suggested")]
    skipped = [x for x in items if not x.get("suggested")]
    print(f"待应用 {len(pending)} 条（suggested=null 自动跳过 {len(skipped)} 条）")

    s = requests.Session()
    s.trust_env = False  # 项目铁律：防 Clash 劫持
    headers = {"X-API-Key": _load_admin_key()}

    ok, fail = 0, 0
    for x in pending:
        name, cat = x["name"], x["suggested"]
        # GET 原文（带 429 重试：滑动窗口 120 req/min/Key，Retry-After 头为准）
        r = None
        for attempt in range(5):
            r = s.get(f"{BASE}/v1/admin/skills/{name}", headers=headers, timeout=15)
            if r.status_code != 429:
                break
            wait = int(r.headers.get("Retry-After", "8")) + 1
            print(f"  [429] {name}: GET 限流，等 {wait}s（第 {attempt + 1} 次）")
            time.sleep(wait)
        if r is None or r.status_code != 200:
            print(f"  [FAIL] {name}: GET {r.status_code if r is not None else 'n/a'}")
            fail += 1
            continue
        body = r.json()
        body = body.get("data", body) if isinstance(body, dict) else {}
        content = body.get("content") or ""
        new_content, hit = _replace_category(content, cat)
        if not hit:
            print(f"  [SKIP] {name}: frontmatter 未找到 category: uncategorized 行")
            continue
        if not args.apply:
            print(f"  [DRY] {name}: uncategorized -> {cat}")
            ok += 1
            continue
        # PUT 回写（同样 429 重试）
        pr = None
        for attempt in range(5):
            pr = s.put(
                f"{BASE}/v1/admin/skills/{name}", headers=headers,
                json={"content": new_content}, timeout=20,
            )
            if pr.status_code != 429:
                break
            wait = int(pr.headers.get("Retry-After", "8")) + 1
            print(f"  [429] {name}: PUT 限流，等 {wait}s（第 {attempt + 1} 次）")
            time.sleep(wait)
        if pr is not None and pr.status_code == 200:
            ok += 1
        else:
            print(f"  [FAIL] {name}: PUT {pr.status_code if pr is not None else 'n/a'} {pr.text[:80] if pr is not None else ''}")
            fail += 1
        time.sleep(0.6)  # 限流友好：102 条 × (GET+PUT) 留出窗口余量

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] 成功 {ok} / 失败 {fail} / 跳过 {len(skipped)}")
    if args.apply and ok:
        print("建议收尾：POST /v1/admin/skills/sync {\"direction\": \"to_remote\"} 回推 bare 仓")


if __name__ == "__main__":
    main()
