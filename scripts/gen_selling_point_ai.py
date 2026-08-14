#!/usr/bin/env python3
"""生成 AI 创意类卖点图（火山 seedream plan 通道）。"""
import os
import sys
import json
import urllib.request
import urllib.error

API_URL = "https://ark.cn-beijing.volces.com/api/plan/v3/images/generations"
MODEL = "doubao-seedream-5.0-lite"
KEY = os.environ.get("VOLC_API_KEY")
if not KEY:
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            if line.startswith("VOLC_API_KEY="):
                KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
if not KEY:
    print("ERROR: VOLC_API_KEY 未设置")
    sys.exit(1)

JOBS = [
    {
        "out": "assets/selling-point-06-chinese.png",
        "prompt": (
            "现代科技插画，深蓝紫色渐变背景，画面中央一个巨大的发光汉字「记」悬浮，"
            "书法笔触风格，汉字周围环绕漂浮的中文对话气泡和汉字偏旁部首元素（氵 讠 纟 心），"
            "发光的青色霓虹光效，玻璃拟态质感，半透明科技网格底纹，"
            "扁平 3D 混合风格，干净简洁，画面除「记」字外无其他文字，16:9 宽幅构图"
        ),
    },
    {
        "out": "assets/selling-point-08-zero-llm.png",
        "prompt": (
            "现代科技插画，深蓝紫色渐变背景，画面中央一个发光的数据簇图标，"
            "旁边一枚发光的硬币和向下弯曲的绿色成本曲线箭头（表示成本趋零），"
            "点缀零的符号 0 和闪烁星光元素，发光的青色霓虹光效，玻璃拟态质感，"
            "半透明科技网格底纹，扁平 3D 混合风格，干净简洁，画面无文字，16:9 宽幅构图"
        ),
    },
]


def gen(prompt: str, out_path: str, size: str = "2560x1440") -> None:
    body = json.dumps({
        "model": MODEL, "prompt": prompt, "size": size,
        "response_format": "url", "watermark": False,
        "sequential_image_generation": "disabled",
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    url = data["data"][0]["url"]
    with urllib.request.urlopen(url, timeout=180) as img:
        content = img.read()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(content)
    print("OK:", out_path, len(content), "bytes")


if __name__ == "__main__":
    for job in JOBS:
        try:
            gen(job["prompt"], job["out"])
        except urllib.error.HTTPError as e:
            print("FAIL:", job["out"], e.code, e.read()[:200])
