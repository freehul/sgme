#!/usr/bin/env python3
"""用火山方舟 doubao-seedream 生成卖点配图（单张，风格样图）。"""
import os
import sys
import time
import urllib.request
import json

API_URL = "https://ark.cn-beijing.volces.com/api/plan/v3/images/generations"
MODEL = "doubao-seedream-5.0-lite"
KEY = os.environ.get("VOLC_API_KEY")
if not KEY:
    # 回退：从 SGME config/.env 读取（不打印值）
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("VOLC_API_KEY="):
                KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
if not KEY:
    print("ERROR: VOLC_API_KEY 未设置")
    sys.exit(1)

def gen_image(prompt: str, out_path: str, size: str = "2K") -> str:
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": False,
        "sequential_image_generation": "disabled",
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    url = data["data"][0]["url"]
    with urllib.request.urlopen(url, timeout=120) as img:
        content = img.read()
    with open(out_path, "wb") as f:
        f.write(content)
    return out_path

PROMPT = (
    "现代科技插画，深蓝紫色渐变背景，一条发光的青色光线路径横贯画面，"
    "路径串联四个玻璃拟态卡片节点：最右是 AI 机器人头像卡片（代表 AI 画像），"
    "中间是场景卡片和记忆卡片，最左是一叠展开的文档（代表原始对话记录），"
    "路径上有从右往左延伸的虚线追溯箭头，箭头旁有一个发光的放大镜图标，"
    "表示从 AI 的画像一路追溯回原始对话。霓虹青蓝光效，玻璃拟态，扁平 3D 混合风格，"
    "科技感，干净简洁，画面无任何文字，16:9 宽幅构图"
)

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "assets/selling-point-01-trace.png"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    path = gen_image(PROMPT, out)
    print("OK:", path)
