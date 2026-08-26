"""sgme/skills/writesync.py：进程内写锁单点串行（ST-36 M3，设计 §四）。

**NAS Server 进程是唯一合法写入方，进程内锁保证临界区原子**
（设计 v0.2.1 并发裁决：单点串行——多机 agent 并发请求在入口自然排队，
无需分布式锁；绕过 API 的直推由 pre-receive 钩子执法，不归本层管）。

所有落盘+git commit 的写侧操作必须包在 ``write_critical()`` 内执行，
保证「读状态→校验→写文件→commit」整个序列对同进程其他写请求不可见中间态。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

# 进程级唯一写锁：技能仓全部写路径共用（单点串行的物理载体）
write_lock = threading.Lock()


@contextmanager
def write_critical():
    """写临界区：with write_critical(): ... 内的落盘/commit 原子（进程内互斥）。"""
    with write_lock:
        yield
