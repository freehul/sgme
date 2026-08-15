# -*- coding: utf-8 -*-
"""scripts/describe_image.py：看图能力固化——本地优先，火山托底。

把「text-only 模型读图」能力固化成一个可复用脚本：
- 优先调本地 LM Studio 视觉模型（离线、图不出本机）
- 本地不可达/失败 → 托底调火山方舟云端视觉模型

用法：
    python scripts/describe_image.py <图片路径> [提问]
    python scripts/describe_image.py 图.png "这是什么？"

返回 JSON：
    {"ok": true, "provider": "lmstudio"|"volc", "model": "...", "description": "..."}

配置（环境变量，密钥不落盘铁律 #10）：
- 本地 LM Studio：LMSTUDIO_BASE_URL（默认 http://127.0.0.1:1014/v1）
                  LMSTUDIO_VISION_MODEL（默认 qwythos-9b-v2）
- 火山托底：VISION_BASE_URL / VISION_MODEL / VOLC_API_KEY（User 级已配）
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import httpx

# ---------- 配置 ----------

LMSTUDIO_BASE_URL = "http://127.0.0.1:1014/v1"
LMSTUDIO_VISION_MODEL = "qwythos-9b-v2"

VOLC_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
VOLC_VISION_MODEL = "doubao-seed-2.0-lite"

DEFAULT_PROMPT = "请用中文简洁描述这张图：标题是什么？有哪些主要元素？"


def _env(name: str, default: str = "") -> str:
    """读环境变量：先 process 级，再 User 级（注册表），最后默认值。

    关键：长驻进程（agent harness）是在环境变量写入之前启动的，其进程
    树里看不到后写入的 User 级变量（如 VOLC_API_KEY / NPM_KEY）。故此处
    兜底用 PowerShell 查 User 级注册表，保证脚本独立运行时也能读到。
    """
    import os
    v = os.environ.get(name)
    if v:
        return v
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"[System.Environment]::GetEnvironmentVariable('{name}','User')"],
            capture_output=True, text=True, timeout=10,
        )
        v = r.stdout.strip()
        if v:
            return v
    except Exception:
        pass
    return default


def _read_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _chat(base_url: str, model: str, b64: str, prompt: str,
          api_key: str | None = None, timeout: int = 180) -> str:
    """调 OpenAI 兼容 chat/completions，返回 assistant 文本。

    用 httpx（trust_env=False 防 Clash 代理劫持，且对带图大 payload
    稳定——urllib 对火山带图请求会 10054 连接重置，实测 httpx 正常）。
    """
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = httpx.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": messages, "max_tokens": 2000},
        headers=headers,
        timeout=timeout,
        trust_env=False,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def describe_image(image_path: str, prompt: str = DEFAULT_PROMPT) -> dict:
    """本地优先，火山托底。返回 {ok, provider, model, description}。"""
    import os

    b64 = _read_image_b64(image_path)

    # 1. 本地 LM Studio
    local_base = _env("LMSTUDIO_BASE_URL", LMSTUDIO_BASE_URL)
    local_model = _env("LMSTUDIO_VISION_MODEL", LMSTUDIO_VISION_MODEL)
    try:
        text = _chat(local_base, local_model, b64, prompt)
        if text and text.strip():
            return {"ok": True, "provider": "lmstudio", "model": local_model,
                    "description": text.strip()}
    except Exception as e:
        # 本地失败 → 记入 error，继续火山托底
        local_err = str(e)
    else:
        local_err = "本地返回空文本"

    # 2. 火山托底
    volc_base = _env("VISION_BASE_URL", VOLC_BASE_URL)
    volc_model = _env("VISION_MODEL", VOLC_VISION_MODEL)
    volc_key = _env("VOLC_API_KEY", "")
    try:
        text = _chat(volc_base, volc_model, b64, prompt, api_key=volc_key)
        if text and text.strip():
            return {"ok": True, "provider": "volc", "model": volc_model,
                    "description": text.strip(),
                    "local_error": local_err}
    except Exception as e:
        volc_err = str(e)
    else:
        volc_err = "火山返回空文本"

    return {"ok": False, "provider": None, "model": None,
            "description": None,
            "error": f"本地失败({local_err})；火山失败({volc_err})"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="看图：本地 LM Studio 优先，火山托底")
    parser.add_argument("image", help="图片路径（png/jpg 等）")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT, help="提问（可选）")
    args = parser.parse_args(argv)

    if not Path(args.image).exists():
        print(json.dumps({"ok": False, "error": f"图片不存在: {args.image}"},
                         ensure_ascii=False))
        return 1

    result = describe_image(args.image, args.prompt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
