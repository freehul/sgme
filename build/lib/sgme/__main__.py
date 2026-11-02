"""SGME Server 启动入口：`python -m sgme` 启动 uvicorn:9910。

环境变量：
- SGME_ADMIN_KEY  管理员 API Key（不设则用 dev 默认 + 告警）
- SGME_AGENT_KEY  Agent API Key（不设则用 dev 默认 + 告警）
- SGME_BEARER_TOKEN  Bearer 令牌（不设则旁路关闭，仅 localhost）
- SGME_HOST / SGME_PORT  绑定地址（默认 127.0.0.1:9910）

启动时经 lifespan 附加：每日 Tier0 摘要生成 cron（UTC 00:00）+ 每 10 分钟心跳检查
+ Batch 兜底扫描定时器（refine.batch_scan.enabled=true 时，线程常驻）。
"""
from __future__ import annotations

import os
import sys

from sgme.server.app import create_app

# T-146②：端口占用的常见 errno（Windows 10048 = 仅一次套接字地址使用；
# Linux 98 = Address already in use）
_PORT_IN_USE_ERRNOS = {10048, 98}


def _port_in_use_hint(port: int) -> str:
    """端口占用时的中文排障提示（T-146②）。"""
    return (
        f"\n[启动失败] 端口 {port} 已被占用（通常为 SGME 已在运行）。\n"
        f"  处置：① 先停旧进程（Windows: netstat -ano | findstr :{port} → taskkill /PID <pid> /F；"
        f"NAS: docker restart sgme）\n"
        f"        ② 或换端口重启：SGME_PORT=<其他端口> python -m sgme\n"
    )


def main() -> None:
    host = os.environ.get("SGME_HOST", "127.0.0.1")
    port = int(os.environ.get("SGME_PORT", "9910"))
    app = create_app(start_background_tasks=True)

    # 延迟导入 uvicorn，仅在启动时需要
    import uvicorn
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except OSError as e:
        # T-146②：端口占用报 Errno 10048/98 时给可行动提示，不再裸抛 winerror
        if getattr(e, "errno", None) in _PORT_IN_USE_ERRNOS:
            print(_port_in_use_hint(port), file=sys.stderr)
            raise SystemExit(1) from e
        raise


if __name__ == "__main__":
    main()
