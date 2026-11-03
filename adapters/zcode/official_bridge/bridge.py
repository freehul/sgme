# -*- coding: utf-8 -*-
"""ZCode 官方 CLI ↔ OpenAI 兼容桥（本地垫片）。

用途：SGME 的提炼/重建只支持 openai_compat 供应商；本桥把请求转成
「官方 CLI 单次 headless 调用」，由官方二进制自己完成 OAuth/JWT 与 V4 签名，
从而在账号有额度时使用 ZCode 官方通道（Start Plan / Coding Plan）。

设计要点：
- 每个请求：提示词写入临时文件 → 官方 CLI --attach 读取 → 解析输出正文；
- 官方 CLI 的调用形态：node zcode.cjs -p "<短指令>" --attach <prompt.txt>
  --cwd <隔离目录> --mode plan --surface terminal --no-color
- 并发用信号量限制（官方 CLI 是重量级进程，默认 2）；
- 不写任何账号凭据；凭据由官方 CLI 自己管理。

环境变量（全部可选）：
  ZCODE_NODE           node 可执行文件（默认 node）
  ZCODE_CJS            zcode.cjs 路径（默认按常见安装位置探测）
  ZCODE_TIMEOUT        单次调用超时秒（默认 600）
  ZCODE_CONCURRENCY    并发上限（默认 2）
  ZCODE_MODEL_ID       对外的模型 id（默认 zcode/GLM-5.3-Flash-Trial）
  ZCODE_WORK_ROOT      临时工作目录根（默认系统临时目录）
  ZCODE_EXTRA_ARGS     追加给 CLI 的 JSON 数组（默认 []）
  ZCODE_PROMPT_PREFIX  附加在每次调用前的固定指令（默认内置一句）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DEFAULT_PROMPT_PREFIX = (
    "阅读附件中的完整指令，严格按其要求完成并只输出最终结果；"
    "不要解释、不要复述指令、不要调用任何工具。"
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def _iter_json_objects(text: str):
    """从文本里找出所有可解析的 JSON 对象（浅层扫描，够用即可）。"""
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        start = text.find("{", idx)
        if start < 0:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except Exception:
            idx = start + 1
            continue
        yield obj
        idx = end


def extract_text(raw: str) -> str:
    """从 CLI 原始输出中提取助手正文。

    兼容多种可能形态（官方 CLI 版本间可能变化）：
    1) OpenAI 风格：{"choices":[{"message":{"content":"..."}}]}
    2) 消息数组：{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
    3) 简单对象：{"text": "..."} / {"content": "..."} / {"result": "..."}
    4) 纯文本：取最后一段非日志文本
    """
    clean = strip_ansi(raw)
    if "3012" in clean or "risk control" in clean.lower():
        return ""
    candidates: list[str] = []
    for obj in _iter_json_objects(clean):
        try:
            if isinstance(obj, dict):
                choices = obj.get("choices")
                if isinstance(choices, list) and choices:
                    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, str) and content.strip():
                            candidates.append(content)
                for key in ("text", "content", "result", "output"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        candidates.append(val)
                msg = obj.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        candidates.append(content)
                    elif isinstance(content, list):
                        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                        joined = "".join(parts).strip()
                        if joined:
                            candidates.append(joined)
        except Exception:
            continue
    if candidates:
        return max(candidates, key=len).strip()

    # 纯文本兜底：剔除明显日志行后取尾部
    lines = []
    for ln in clean.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("[", "Error:", "Traceback", "Warning:")):
            continue
        if "traceId" in s or "ProviderBusinessError" in s or "无法定位" in s or "Turn execution failed" in s:
            continue
        lines.append(s)
    return "\n".join(lines).strip()


def default_cjs_path() -> str:
    env = os.environ.get("ZCODE_CJS", "").strip()
    if env:
        return env
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/ZCode/resources/glm/zcode.cjs",
        Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"),
        Path.home() / ".zcode/cli/zcode.cjs",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return ""


def ensure_provider_config(cjs_path: str) -> str | None:
    """官方 CLI 需要 zcode-builtin.json；缺失时从安装目录复制到用户配置位置。

    返回复制目标路径（已存在或复制成功时）；无法处理时返回 None。
    """
    if os.environ.get("ZCODE_NO_ENSURE_PROVIDER_CONFIG", "").strip() in ("1", "true", "yes"):
        return None
    src = Path(cjs_path).resolve().parent.parent / "config" / "provider" / "zcode-builtin.json"
    if not src.is_file():
        return None
    if os.name == "nt":
        target_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "config" / "provider"
    else:
        target_dir = Path.home() / ".config" / "provider"
    target = target_dir / "zcode-builtin.json"
    try:
        if target.is_file() and target.stat().st_size == src.stat().st_size:
            return str(target)
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        return str(target)
    except Exception:
        return None


class CliRunner:
    """官方 CLI 调用器：一次调用 = 一个隔离临时目录 + 一个附件提示词。"""

    def __init__(
        self,
        node: str | None = None,
        cjs: str | None = None,
        timeout_s: float = 600.0,
        concurrency: int = 2,
        work_root: str | None = None,
        extra_args: list[str] | None = None,
        prompt_prefix: str | None = None,
    ) -> None:
        self.node = node or os.environ.get("ZCODE_NODE", "node")
        self.cjs = cjs or default_cjs_path()
        if not self.cjs or not Path(self.cjs).is_file():
            raise RuntimeError("找不到 zcode.cjs，请设置 ZCODE_CJS")
        self.timeout_s = float(timeout_s)
        self.concurrency = max(1, int(concurrency))
        ensure_provider_config(self.cjs)
        self.work_root = Path(work_root or tempfile.gettempdir())
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.extra_args = list(extra_args or [])
        self.prompt_prefix = prompt_prefix or DEFAULT_PROMPT_PREFIX
        self._sem = threading.Semaphore(self.concurrency)
        self._lock = threading.Lock()
        self._stats = {"calls": 0, "errors": 0, "total_s": 0.0}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def run(self, prompt: str) -> dict[str, Any]:
        started = time.time()
        with self._sem:
            with tempfile.TemporaryDirectory(prefix="zcode-bridge-", dir=str(self.work_root)) as td:
                prompt_path = Path(td) / "prompt.txt"
                prompt_path.write_text(prompt, encoding="utf-8")
                cmd = [
                    self.node,
                    self.cjs,
                    "-p",
                    self.prompt_prefix,
                    "--attach",
                    str(prompt_path),
                    "--cwd",
                    td,
                    "--mode",
                    "plan",
                    "--surface",
                    "terminal",
                    "--no-color",
                ] + self.extra_args
                env = os.environ.copy()
                env.setdefault("NO_COLOR", "1")
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.timeout_s,
                        cwd=td,
                        env=env,
                    )
                except subprocess.TimeoutExpired as exc:
                    elapsed = time.time() - started
                    with self._lock:
                        self._stats["calls"] += 1
                        self._stats["errors"] += 1
                        self._stats["total_s"] += elapsed
                    return {
                        "ok": False,
                        "error_type": "timeout",
                        "message": "官方 CLI 调用超时（%.0fs）" % self.timeout_s,
                        "elapsed_s": round(elapsed, 2),
                        "raw": (exc.stdout or "") + (exc.stderr or ""),
                    }
                elapsed = time.time() - started
                raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
                text = extract_text(raw)
                err = None
                low = raw.lower()
                if "3012" in raw or "risk control" in low:
                    err = "risk_control"
                elif " 429" in raw or "1113" in raw or "rate_limit" in low or "insufficient balance" in low:
                    err = "rate_limit"
                elif "ERA_NEGOTIATION_FAILED" in raw or "version negotiation probe failed" in low:
                    err = "mcp_error"
                elif proc.returncode != 0:
                    err = "cli_error"
                elif not text:
                    err = "empty"
                elif any(k in raw for k in ("无法定位", "Turn execution failed", "ProviderBusinessError", "Traceback (most recent call last)")):
                    err = "cli_error"
                with self._lock:
                    self._stats["calls"] += 1
                    self._stats["total_s"] += elapsed
                    if err:
                        self._stats["errors"] += 1
                message = None
                if err:
                    tail = strip_ansi(raw).strip().splitlines()
                    message = (tail[-1][:300] if tail else err)
                return {
                    "ok": bool(text) and err is None,
                    "text": text,
                    "error_type": err,
                    "message": message,
                    "returncode": proc.returncode,
                    "elapsed_s": round(elapsed, 2),
                    "raw": raw,
                }


def build_prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        content = "" if content is None else str(content)
        if not content:
            continue
        if role == "system":
            parts.append("【系统指令】\n" + content)
        elif role == "assistant":
            parts.append("【历史助手回复】\n" + content)
        else:
            parts.append("【用户输入】\n" + content)
    return "\n\n".join(parts)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ZCodeOfficialBridge/0.1"
    runner: CliRunner | None = None
    model_id = "zcode/GLM-5.3-Flash-Trial"

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "model": self.model_id, "stats": self.runner.stats() if self.runner else {}})
        elif self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [{"id": self.model_id, "object": "model"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8"))
        except Exception:
            self._json(400, {"error": {"message": "bad json"}})
            return
        prompt = build_prompt(payload.get("messages") or [])
        if not prompt:
            self._json(400, {"error": {"message": "empty prompt"}})
            return
        started = time.time()
        try:
            result = self.runner.run(prompt)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": {"message": "bridge 内部错误: %r" % exc}})
            return
        if not result.get("ok"):
            code = 429 if result.get("error_type") == "rate_limit" else 502
            self._json(code, {"error": {"message": result.get("message") or "官方 CLI 调用失败", "type": result.get("error_type"), "detail": strip_ansi(result.get("raw", ""))[-800:]}})
            return
        text = result.get("text", "")
        usage = {"prompt_tokens": max(1, len(prompt) // 4), "completion_tokens": max(1, len(text) // 4), "total_tokens": max(1, (len(prompt) + len(text)) // 4)}
        self._json(200, {
            "id": "zcode-bridge-%d" % int(started * 1000),
            "object": "chat.completion",
            "created": int(started),
            "model": payload.get("model") or self.model_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
            "x_zcode_elapsed_s": result.get("elapsed_s"),
        })

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，避免污染 stdout
        return


def serve(host: str, port: int, runner: CliRunner, model_id: str) -> None:
    BridgeHandler.runner = runner
    BridgeHandler.model_id = model_id
    httpd = ThreadingHTTPServer((host, port), BridgeHandler)
    print("zcode official bridge listening on http://%s:%d/v1 model=%s" % (host, port, model_id), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ZCode 官方 CLI ↔ OpenAI 兼容桥")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ZCODE_BRIDGE_PORT", "8899")))
    ap.add_argument("--node", default=os.environ.get("ZCODE_NODE", "node"))
    ap.add_argument("--cjs", default=os.environ.get("ZCODE_CJS", ""))
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("ZCODE_TIMEOUT", "600")))
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("ZCODE_CONCURRENCY", "2")))
    ap.add_argument("--model", default=os.environ.get("ZCODE_MODEL_ID", "zcode/GLM-5.3-Flash-Trial"))
    ap.add_argument("--selftest", action="store_true", help="只做一次真实调用并打印结果（需要可用账号）")
    ap.add_argument("--check", action="store_true", help="只做只读诊断（CLI/登录/模型选择），不发起调用")
    ap.add_argument("--login", choices=["zai", "bigmodel"], help="调用官方 CLI 登录（浏览器授权），用于借用账号身份")
    args = ap.parse_args(argv)

    extra: list[str] = []
    raw_extra = os.environ.get("ZCODE_EXTRA_ARGS", "").strip()
    if raw_extra:
        try:
            extra = [str(x) for x in json.loads(raw_extra)]
        except Exception:
            print("ZCODE_EXTRA_ARGS 不是 JSON 数组，忽略", file=sys.stderr)

    try:
        runner = CliRunner(node=args.node, cjs=args.cjs, timeout_s=args.timeout, concurrency=args.concurrency, extra_args=extra)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.login:
        print("将调用官方 CLI 登录（%s）。按提示在浏览器完成授权；Ctrl+C 可取消。" % args.login, flush=True)
        try:
            return subprocess.call([args.node, runner.cjs, "login", args.login, "--no-browser"])
        except KeyboardInterrupt:
            return 130

    if args.check:
        import datetime
        cfg_path = Path.home() / ".zcode" / "v2" / "provider_config.json"
        cred_path = Path.home() / ".zcode" / "v2" / "credentials.json"
        info: dict[str, Any] = {
            "node": args.node,
            "cjs": runner.cjs,
            "cjs_exists": Path(runner.cjs).is_file(),
            "provider_config": str(cfg_path) if cfg_path.is_file() else None,
            "provider_config_mtime": datetime.datetime.fromtimestamp(cfg_path.stat().st_mtime).isoformat() if cfg_path.is_file() else None,
            "credentials_mtime": datetime.datetime.fromtimestamp(cred_path.stat().st_mtime).isoformat() if cred_path.is_file() else None,
        }
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
            info["default_model_selection"] = ((cfg.get("config") or {}).get("defaultModelSelection") or {})
        except Exception as exc:  # noqa: BLE001
            info["default_model_selection_error"] = repr(exc)
        info["provider_config_copy"] = ensure_provider_config(runner.cjs)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.selftest:
        res = runner.run("请只回复两个字：可用")
        print(json.dumps({k: v for k, v in res.items() if k != "raw"}, ensure_ascii=False, indent=2))
        print("---- raw tail ----")
        print(strip_ansi(res.get("raw", ""))[-1200:])
        return 0 if res.get("ok") else 1

    serve(args.host, args.port, runner, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
