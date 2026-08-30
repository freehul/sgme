#!/usr/bin/env python3
"""install_client.py — 客户端模式安装清单生成（T-121：纯远程接入端服务发现）。

用途：本机**不跑 SGME**、只作为远程接入端（Agent 远程调用 NAS 等机器上的
SGME）时，用本脚本生成 ~/.sgme/install.json 服务发现清单——http 记录远程
host/port，data_dir/raw_dir 置 null（表示本地无数据目录，防止接入机上残留
的本机测试安装清单误导服务发现）。

与服务端启动自动生成的区别：
  - 服务端：本机跑 SGME，生产启动时 app.py lifespan 自动调 write_install_json
    生成全量清单（含本机 data_dir/raw_dir），无需手动运行本脚本；
  - 客户端：本脚本手动生成（重跑即覆盖更新），只记录远程地址 + Key 环境变量
    名引用，data_dir/raw_dir 为 null。

服务发现顺序（接入纪律 SGME-ONBOARDING-v1）：
  1) 探测 http://<host>:<port>/v1/health；
  2) 失败读 ~/.sgme/install.json 兜底（本脚本生成的即第二步数据源）；
  3) 仍失败 → 向主人报告「SGME 未发现」。

用法：
  python scripts/install_client.py --host 192.168.10.10 [--port 9910] [--mcp-port 9913]

Key 引用：清单只写环境变量名（SGME_ADMIN_KEY/SGME_AGENT_KEY/SGME_BEARER_TOKEN），
不落任何明文密钥（铁律 #10：密钥不落盘）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 sgme 包可导入（scripts/ 下脚本统一做法，与 e2e_smoke.py 同模式）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sgme.config import write_client_install_json  # noqa: E402


def main() -> int:
    """解析参数并生成客户端模式 install.json，打印写入路径与 http 地址摘要。"""
    parser = argparse.ArgumentParser(
        description="客户端模式：生成 install.json 服务发现清单（本机不跑 SGME，纯远程接入端）",
    )
    parser.add_argument("--host", required=True, help="远程 SGME 主机地址（如 192.168.10.10）")
    parser.add_argument("--port", type=int, default=9910, help="远程 HTTP 端口（默认 9910）")
    parser.add_argument(
        "--mcp-port", type=int, default=None,
        help="远程 MCP 端口（缺省转交 config 层：取 SGME_MCP_PORT env，默认 9913）",
    )
    args = parser.parse_args()

    path = write_client_install_json(host=args.host, port=args.port, mcp_port=args.mcp_port)
    print(f"安装清单已写入: {path}（http://{args.host}:{args.port}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
