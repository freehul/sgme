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

from sgme.server.app import create_app


def main() -> None:
    host = os.environ.get("SGME_HOST", "127.0.0.1")
    port = int(os.environ.get("SGME_PORT", "9910"))
    app = create_app(start_background_tasks=True)

    # 延迟导入 uvicorn，仅在启动时需要
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
