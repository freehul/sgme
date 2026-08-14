#!/usr/bin/env python3
"""project_audit.py — 定期扫描项目目录 vs SGME 登记，发现未登记/改名项目。

第三层防遗忘保险：即使 SOUL.md 铁律和需求池可见性都失效，
此脚本也能发现"有项目目录但没登记"。

输出：发现异常 → 列出明细（exit 0）；全部正常 → 空输出（静默）。
"""
import json
import os
import urllib.request
from pathlib import Path

PROJECTS_ROOT = Path(os.environ.get("SGME_PROJECTS_ROOT") or os.getcwd()).resolve()
SGME_URL = "http://localhost:9910/v1/search"
SGME_KEY = os.environ.get("SGME_AGENT_KEY", "") or (
    dict(
        l.strip().split("=", 1)
        for l in (Path(__file__).resolve().parents[1] / "config" / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in l and not l.strip().startswith("#")
    ).get("SGME_AGENT_KEY", "dev-agent-key-change-me")
)
# 非项目目录（排除）
EXCLUDE = {
    "SGME", "aixm",  # SGME 自身 + AIXM（旧 Python hook 项目）
    "memory-system-research", "tackmark", "hermes",  # 研究/工具目录
}

# 登记标志：SGME 记忆里含 "项目立项：<name>" 或 "项目注册"
def check_registered(name: str) -> bool:
    try:
        body = json.dumps({
            "query": f"项目立项 {name} 项目注册",
            "scopes": ["memory"],
            "limit": 5,
        }).encode("utf-8")
        req = urllib.request.Request(
            SGME_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": SGME_KEY},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for r in data.get("results", []):
            content = str(r.get("content") or "")
            if f"项目立项：{name}" in content or f"项目立项：{name}" in content.replace(" ", ""):
                return True
            if name in content and ("项目注册" in content or "项目立项" in content):
                return True
        return False
    except Exception:
        return True  # SGME 不可用时静默（fail-open）


def main():
    if not PROJECTS_ROOT.exists():
        return
    issues = []
    for d in sorted(PROJECTS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith(".") or d.name in EXCLUDE:
            continue
        # 有 .git 或 docs/ 的才算项目目录
        has_git = (d / ".git").exists()
        has_docs = (d / "docs").exists()
        if not (has_git or has_docs):
            continue
        if not check_registered(d.name):
            issues.append(d.name)

    if issues:
        print("⚠️ 以下项目目录未在 SGME 登记（可能漏了立项流程）：")
        for n in issues:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
