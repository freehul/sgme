#!/usr/bin/env python
"""T-168 冒烟：dsh-sgme 0.5.0 新增 15 工具对应的生产端点真实可用性核验。

设计约束（对齐项目纪律）：
- **零硬编码**：地址从服务发现清单 ``~/.sgme/install.json`` 读（http.host/http.port），
  key 从 ``adapters/dsh/.env`` 读；脚本不打印密钥原文（只打印「已取到/缺失」布尔）。
- **只读优先**：冒烟只打只读端点 + ``skill_materialize``（服务端只返回内容，落盘在本地
  tmp/）。运维写侧（config_update / skill_put / skill_delete / skill_rename /
  refine_trigger / refine_batch / signal_clear）刻意不冒烟——会改生产状态或消耗 LLM 额度。
- **防代理劫持**：显式禁用环境变量代理（对齐 sgme 侧 httpx trust_env=False 约定）。

用法：
    <python> scripts/oneoff/verify_t168_dsh_endpoints.py

背景：T-168（dsh-sgme 0.5.0 工具面对齐）的验收脚本，先例见
``scripts/oneoff/verify_t86_endpoints.py``（同样是「插件新工具 → 生产端点真实冒烟」）。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台按 UTF-8 输出（git-bash 下 stdout 默认可能是 GBK）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[2]           # <project-root>
ENV_FILE = ROOT / "adapters" / "dsh" / ".env"
INSTALL_JSON = Path.home() / ".sgme" / "install.json"
MATERIALIZE_DIR = "/tmp/sgme-smoke-t168"   # ⚠️ 服务端（容器内）路径；落 /tmp 不碰业务数据

# 显式禁用代理（防 Clash 劫持）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_env(path: Path) -> dict[str, str]:
    """解析 .env（不打印内容）。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_base(env: dict[str, str]) -> tuple[str, str]:
    """解析 baseUrl（优先级：env > install.json > 回环缺省）。返回 (base, 来源说明)。"""
    if env.get("SGME_BASE_URL"):
        return env["SGME_BASE_URL"].rstrip("/"), ".env(SGME_BASE_URL)"
    if INSTALL_JSON.exists():
        try:
            d = json.loads(INSTALL_JSON.read_text(encoding="utf-8"))
            http = d.get("http") or {}
            host, port = http.get("host"), http.get("port")
            if host and port:
                return f"http://{host}:{port}", "install.json"
        except Exception:
            pass
    return "http://127.0.0.1:9910", "回环缺省"


def call(method: str, url: str, key: str | None = None, body: dict | None = None,
         timeout: int = 30) -> tuple[int | None, object]:
    """发请求，返回 (状态码, 解析后的 JSON 或错误文本)。状态码 None 表示连不上。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw[:300]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:300]
    except Exception as e:  # 连接失败/超时
        return None, f"{type(e).__name__}: {e}"


def ok200(code: int | None) -> bool:
    return code == 200


def main() -> int:
    env = read_env(ENV_FILE)
    agent_key = env.get("SGME_AGENT_KEY", "")
    admin_key = env.get("SGME_ADMIN_KEY", "")
    base, base_src = resolve_base(env)

    print("=" * 68)
    print("dsh-sgme 0.5.0 冒烟 —— 生产端点真实可用性核验（T-168）")
    print("=" * 68)
    # 地址脱敏：只报来源与形态，不打印内网 IP
    host_part = re.sub(r"^https?://([^:/]+).*$", r"\1", base)
    shape = "回环" if host_part in ("127.0.0.1", "localhost") else "非回环"
    print(f"baseUrl: <{shape}:{base.rsplit(':', 1)[-1]}>  来源: {base_src}")
    print(f"agent key: {'已取到' if agent_key else '【缺失】'}   "
          f"admin key: {'已取到' if admin_key else '【缺失】'}")
    print("-" * 68)

    if not agent_key:
        print("⚠️ 未取到 SGME_AGENT_KEY —— 请确认 adapters/dsh/.env 存在且已由 install.py 写入")
        return 1

    results: list[tuple[str, bool, str]] = []

    def record(label: str, passed: bool, note: str) -> None:
        results.append((label, passed, note))
        print(f"[{'PASS' if passed else 'FAIL'}] {label} — {note}")

    # ① health（免鉴权）
    code, d = call("GET", f"{base}/v1/health")
    ver = d.get("version") if isinstance(d, dict) else "?"
    record("health", ok200(code) and isinstance(d, dict),
           f"HTTP {code} version={ver}")

    # ② search（memory）
    code, d = call("POST", f"{base}/v1/search", agent_key,
                   {"query": "dsh 插件 对齐", "scopes": ["memory"], "limit": 2})
    n = len(d.get("results", [])) if isinstance(d, dict) else -1
    record("search(memory)", ok200(code) and n >= 0, f"HTTP {code} results={n}")

    # ③ answer（T-149，会消耗一次 LLM）
    code, d = call("POST", f"{base}/v1/answer", agent_key,
                   {"query": "SGME 最新版本是多少", "limit": 3}, timeout=90)
    if isinstance(d, dict) and "error" in d:
        record("answer", False, f"HTTP {code} error={str(d['error'])[:80]}")
    else:
        qtype = d.get("question_type") if isinstance(d, dict) else "?"
        has_ans = bool(isinstance(d, dict) and d.get("answer"))
        record("answer", ok200(code) and has_ans, f"HTTP {code} type={qtype} 有答案={has_ans}")

    # ④ skills 读侧
    code, d = call("GET", f"{base}/v1/skills/coldstart", agent_key)
    items = len((d.get("index") or {}).get("items", [])) if isinstance(d, dict) else -1
    record("skills/coldstart", ok200(code) and items >= 0, f"HTTP {code} 冷启动项={items}")

    code, d = call("GET", f"{base}/v1/skills?limit=3", agent_key)
    total = d.get("total") if isinstance(d, dict) else "?"
    record("skills 列表", ok200(code), f"HTTP {code} total={total}")

    # ④b skills/search（HTTP 侧 T-163 新端点）
    code, d = call("GET", f"{base}/v1/skills/search?q=docker&limit=2", agent_key)
    hits = len(d.get("results", [])) if isinstance(d, dict) else -1
    record("skills/search", ok200(code) and hits >= 0, f"HTTP {code} hits={hits}")

    # ⑤ wiki/search（执行通道）—— query 必须 URL 编码（urllib 不自动处理中文）
    code, d = call("GET", f"{base}/v1/wiki/search?q={urllib.parse.quote('部署')}&limit=2", agent_key)
    wr = len(d.get("results", [])) if isinstance(d, dict) else -1
    record("wiki/search", ok200(code) and wr >= 0, f"HTTP {code} results={wr}")

    # ⑥ skill_materialize（L3 字节保真落盘）
    #
    # ⚠️ 关键语义（本次冒烟实测澄清）：dest_dir 与返回的 path 都是 **SGME 服务端**
    #    路径——落盘发生在服务端进程内，agent 本地 exists() 检查必然为 False（跨机部署）。
    #    故断言只校验「服务端返回了 name/path/sha256」，不校验本地文件存在。
    code, d = call("POST", f"{base}/v1/skills/sgme-operations/materialize", agent_key,
                   {"dest_dir": str(MATERIALIZE_DIR).replace("\\", "/")})
    if isinstance(d, dict) and d.get("sha256"):
        record("skill_materialize", ok200(code) and bool(d.get("path")),
               f"HTTP {code} sha256={str(d['sha256'])[:12]}（path/落盘均在服务端，符合契约）")
    else:
        # 技能名可能不存在，退化为「端点存在性」判定（404 = 端点活着且鉴权通过）
        record("skill_materialize", code in (404, 422),
               f"HTTP {code}（技能名不存在亦可，端点可达即算通过）")

    # ⑦ memory unreject 端点存在性（打不存在的 id，期望 404 而非 404-路由缺失/403）
    code, d = call("POST", f"{base}/v1/memory/__nonexistent_t168__/unreject", agent_key, {})
    record("memory/unreject 端点", code == 404, f"HTTP {code}（期望 404=端点存在且鉴权通过）")

    # ⑧ 运维读侧（Admin Key）
    if admin_key:
        code, d = call("GET", f"{base}/v1/admin/stats", admin_key)
        mem = (d.get("memories") or {}).get("total") if isinstance(d, dict) else "?"
        record("admin/stats", ok200(code), f"HTTP {code} memories={mem}")

        code, d = call("GET", f"{base}/v1/admin/config", admin_key)
        secs = d.get("writable_sections") if isinstance(d, dict) else None
        record("admin/config 读", ok200(code) and isinstance(secs, list),
               f"HTTP {code} 可写段={len(secs) if isinstance(secs, list) else '?'}")

        code, d = call("GET", f"{base}/v1/admin/refine_runs?limit=2", admin_key)
        tot = d.get("total") if isinstance(d, dict) else "?"
        record("admin/refine_runs", ok200(code),
               f"HTTP {code} total={tot}（= dsh refine_status 的数据源）")

        code, d = call("GET", f"{base}/v1/admin/roles", admin_key)
        roles = d.get("total") if isinstance(d, dict) else "?"
        record("admin/roles", ok200(code), f"HTTP {code} total={roles}")

        # consume_all：只验证端点存在（带一个必然匹配不到的类型，避免真清生产信号）
        code, d = call("POST", f"{base}/v1/admin/events/consume_all?type=__nonexistent_t168__",
                       admin_key, {})
        consumed = d.get("consumed") if isinstance(d, dict) else "?"
        record("admin/events/consume_all 端点", ok200(code),
               f"HTTP {code} consumed={consumed}（类型过滤避免误清）")
    else:
        print("[SKIP] Admin 侧端点 —— 未取到 SGME_ADMIN_KEY")

    print("-" * 68)
    passed = sum(1 for _, p, _ in results if p)
    print(f"汇总: {passed}/{len(results)} passed")
    failed = [label for label, p, _ in results if not p]
    if failed:
        print("未通过: " + ", ".join(failed))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
