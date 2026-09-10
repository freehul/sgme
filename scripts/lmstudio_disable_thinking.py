# -*- coding: utf-8 -*-
"""关闭 / 打开 LM Studio 模型的「思考」功能（模型级配置，幂等可重跑）

为什么需要本脚本
----------------
本地 qwen3.8-9b-distill 做 SGME 提炼时正文为空（JSON 解析失败）的根因是：
思考内容（reasoning_content）与正文共用 max_tokens 预算，思考把预算吃光 → content 为空。

三条路都试过，只有第三条有效：
  1. 请求侧 `extra_body={"enable_thinking": false}` —— LM Studio 忽略（官方 issue #1990）；
  2. `chat_template_kwargs` / 模型目录 model.yaml —— 同样无效；
  3. **模型级默认配置** `llm.prediction.reasoning.enableThinking = false` —— ✅ 有效。

机制：LM Studio 把该值作为 Jinja 模板变量 `enable_thinking` 传给模型的提示词模板，
     模板据此决定是否注入思考 token（例如 Gemma 模板里的 `{%- if enable_thinking -%}<|think|>`）。
     所以这是官方正路，不是绕过手段。

落点文件（Windows）
------------------
  %USERPROFILE%\\.lmstudio\\.internal\\user-concrete-model-default-config\\<发布者>\\<模型>\\<gguf>.json

注意：该文件在 LM Studio 程序目录内，不属于本项目版本管理；
      换机 / 重装 LM Studio 后需重跑本脚本，或按本脚本说明手工改。

用法
----
  # 关闭思考（默认动作，幂等）
  python scripts/lmstudio_disable_thinking.py --model-key qwen3.8-9b-distill

  # 先看有哪些候选配置文件匹配
  python scripts/lmstudio_disable_thinking.py --model-key qwen3.8-9b-distill --list

  # 反向：打开思考
  python scripts/lmstudio_disable_thinking.py --model-key qwen3.8-9b-distill --enable

  # 回滚到最近一次备份
  python scripts/lmstudio_disable_thinking.py --model-key qwen3.8-9b-distill --restore

改完必须重载模型才生效：
  lms unload --all && lms load <模型key> --gpu max -c 262144 --parallel 2 -y

验证（reasoning_tokens 应为 0）：
  python tmp/verify_no_think.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

FIELD_KEY = "llm.prediction.reasoning.enableThinking"
DEFAULT_HOME = Path(os.path.expanduser("~")) / ".lmstudio"
CONFIG_SUBDIR = Path(".internal") / "user-concrete-model-default-config"


def find_candidates(home: Path, model_key: str) -> list[Path]:
    """在模型级默认配置目录里找匹配 model_key 的 json（按路径模糊匹配，大小写不敏感）"""
    root = home / CONFIG_SUBDIR
    if not root.is_dir():
        return []
    key = model_key.lower()
    out = []
    for p in sorted(root.rglob("*.json")):
        if p.name.endswith(".bak") or ".bak-" in p.name:
            continue
        rel = str(p.relative_to(root)).lower()
        if key in rel:
            out.append(p)
    return out


def latest_backup(target: Path) -> Path | None:
    baks = sorted(target.parent.glob(target.name + ".bak-*"))
    return baks[-1] if baks else None


def set_thinking(target: Path, enable: bool, keep_backup: bool = True) -> int:
    """写 enableThinking = enable；返回 0 成功"""
    with open(target, encoding="utf-8") as f:
        data = json.load(f)

    fields = data.setdefault("operation", {}).setdefault("fields", [])
    existing = [f for f in fields if isinstance(f, dict) and f.get("key") == FIELD_KEY]

    if keep_backup:
        bak = target.with_name(target.name + ".bak-" + datetime.now().strftime("%Y%m%d"))
        if not bak.exists():
            shutil.copy2(target, bak)
            print(f"[备份] {bak.name}")
        else:
            print(f"[备份] 已存在，跳过：{bak.name}")

    if existing:
        old = existing[0].get("value")
        existing[0]["value"] = enable
        print(f"[修改] 字段已存在：{old} -> {enable}")
    else:
        fields.insert(0, {"key": FIELD_KEY, "value": enable})
        print(f"[修改] 已插入 {FIELD_KEY} = {enable}")

    with open(target, "w", encoding="utf-8", newline="\r\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(f"[写回] {target}")
    print("[结果] operation.fields：")
    for fld in fields:
        if isinstance(fld, dict):
            print(f"   - {fld.get('key')} = {fld.get('value')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="开关 LM Studio 模型的思考功能（模型级配置）")
    ap.add_argument("--model-key", required=True,
                    help="LM Studio 模型 key 或路径片段，如 qwen3.8-9b-distill")
    ap.add_argument("--lmstudio-home", default=str(DEFAULT_HOME),
                    help=f"LM Studio 数据目录（默认 {DEFAULT_HOME}）")
    ap.add_argument("--enable", action="store_true", help="反向：打开思考（默认为关闭）")
    ap.add_argument("--restore", action="store_true", help="从最近一次备份恢复")
    ap.add_argument("--list", action="store_true", help="只列出匹配的配置文件，不修改")
    args = ap.parse_args()

    home = Path(args.lmstudio_home)
    cands = find_candidates(home, args.model_key)
    if not cands:
        print(f"[错误] 未找到匹配 '{args.model_key}' 的模型级配置文件")
        print(f"       搜索目录：{home / CONFIG_SUBDIR}")
        print("       提示：先在 LM Studio 里加载过一次该模型，配置才会落盘")
        return 1

    if args.list:
        print(f"[列表] 匹配 '{args.model_key}' 的配置文件 {len(cands)} 个：")
        for p in cands:
            print("   -", p)
        return 0

    if len(cands) > 1:
        print(f"[错误] 匹配到 {len(cands)} 个文件，请用更精确的 --model-key：")
        for p in cands:
            print("   -", p)
        return 1

    target = cands[0]
    print(f"[目标] {target}")

    if args.restore:
        bak = latest_backup(target)
        if not bak:
            print("[错误] 没有可用备份")
            return 1
        shutil.copy2(bak, target)
        print(f"[恢复] 已从 {bak.name} 恢复（重载模型后生效）")
        return 0

    return set_thinking(target, enable=bool(args.enable), keep_backup=True)


if __name__ == "__main__":
    sys.exit(main())
