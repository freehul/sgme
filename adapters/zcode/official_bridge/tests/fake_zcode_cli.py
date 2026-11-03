# -*- coding: utf-8 -*-
"""测试用假 zcode CLI：模拟官方 CLI 的 -p/--attach 行为。"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path


def main() -> int:
    argv = sys.argv[1:]
    prompt_file = None
    for i, a in enumerate(argv):
        if a == "--attach" and i + 1 < len(argv):
            prompt_file = Path(argv[i + 1])
    mode = os.environ.get("FAKE_ZCODE_MODE", "json")
    prompt = ""
    if prompt_file and prompt_file.is_file():
        prompt = prompt_file.read_text(encoding="utf-8")
    if mode == "json":
        import json
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "FAKE-ANSWER " + prompt[:30]}]}}, ensure_ascii=False))
        return 0
    if mode == "plain":
        print("思考中...")
        print("FAKE-PLAIN " + prompt[:30])
        return 0
    if mode == "rate":
        print("ProviderBusinessError: [1113][Insufficient balance or no resource package. Please recharge.]", file=sys.stderr)
        return 1
    if mode == "risk":
        print('{"error":{"message":"ZCode: request has been blocked due to unusual activity. (405, code 3012)"}}')
        return 0
    if mode == "mcp":
        print("SdkError: Version negotiation probe failed: Connection closed during the version negotiation probe", file=sys.stderr)
        return 1
    if mode == "empty":
        print("")
        return 0
    if mode == "slow":
        time.sleep(5)
        print("SLOW-DONE")
        return 0
    print("unknown mode", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
