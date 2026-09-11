# -*- coding: utf-8 -*-
"""T-150 LongMemEval 评测启动器：注入 config/.env → 拉起评测脚本 → 全程写日志 + 心跳。

Why（背景，2026-09-10 教训）：
    eval/longmemeval_eval.py 自身不加载 .env（不含 dotenv），judge 密钥
    （AGNESAI_API_KEY）与 SGME_* 配置只能靠进程环境传入；把密钥写进 .bat
    违反「密钥不落盘」铁律，故由本启动器在运行时从 config/.env 注入。

    另：用 cmd 的 `>` 重定向时日志恒为 0 字节（句柄在脱离式启动下失效），
    导致评测卡死时无从诊断。故改为 Python 自己打开日志文件写完即刷，
    并起心跳线程每 20 秒打一行——即使某个 LLM 调用长跑，也能看出进程还活着。

    本文件原为现场脚本 tmp/run_eval_env.py（.gitignore 忽略 tmp/，会被清理导致
    评测不可复现），2026-09-11 迁入 eval/ 入库；ROOT 改为按脚本自身位置推导，
    使克隆到任意目录均可运行（原硬编码 D:\\Projects\\SGME）。

用法：
    python eval/run_eval_env.py <日志文件> [longmemeval_eval.py 的全部参数]
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 仓库根 = 本文件所在目录（eval/）的上一级；不写死绝对路径，便于克隆后直接运行
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "config" / ".env"

# 评测专用覆盖：提炼=PC LM Studio，向量=笔记本 LM Studio
# （地址为 T-150 双机评测环境，如需换机直接改这里或改用环境变量覆盖）
OVERRIDES = {
    "SGME_EMBED_BASE_URL": "http://192.168.10.141:1014/v1",
    "SGME_EMBED_MODEL": "text-embedding-bge-m3-legal-euro-r7",
    "SGME_REFINE_BASE_URL": "http://192.168.10.130:8123/v1",
    "SGME_REFINE_MODEL": "qwen3.8-9b-distill",
    "SGME_REFINE_CTX": "131072",  # 128K：单批预算 116490 token
}

# 允许调用方用同名环境变量覆盖上表（切换装载档/端点时用，如冒烟试 4×64K）。
# 不设则用上表默认（提炼=PC、向量=笔记本）。注意 SGME_REFINE_CTX 必须与
# LM Studio 每路上下文一致或更小，否则单批提示词会超窗被截断。
for _k in list(OVERRIDES):
    if os.environ.get(_k):
        OVERRIDES[_k] = os.environ[_k]

log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "eval" / "results" / "run.log"
eval_args = sys.argv[2:]
log_path.parent.mkdir(parents=True, exist_ok=True)
_log = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115


def say(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    _log.write(f"[{stamp}] {msg}\n")
    _log.flush()


say("=" * 60)
say(f"启动器启动  日志={log_path}")

# 1) 注入 config/.env
env = dict(os.environ)
loaded = []
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            env[k] = v
            loaded.append(k)
else:
    say(f"⚠ 未找到 {ENV_FILE}")

env.update(OVERRIDES)
env["PYTHONUNBUFFERED"] = "1"
env["PYTHONIOENCODING"] = "utf-8"

say(f"已从 config/.env 注入 {len(loaded)} 个键（名称不打印）")
say(f"judge 密钥 AGNESAI_API_KEY：{'已就绪' if env.get('AGNESAI_API_KEY') else '缺失 ⚠'}")
for k, v in sorted(OVERRIDES.items()):
    say(f"  生效 {k}={v}")
say(f"评测参数：{' '.join(eval_args)}")

# 2) 心跳线程：LLM 长跑时也能确认进程活着
t0 = time.time()
_stop = threading.Event()


def heartbeat() -> None:
    while not _stop.wait(20):
        say(f"[心跳] 已运行 {int(time.time() - t0)} 秒")


threading.Thread(target=heartbeat, daemon=True).start()

# 3) 拉起评测子进程，stdout/stderr 直灌日志
cmd = [str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "eval" / "longmemeval_eval.py"), *eval_args]
rc = subprocess.call(cmd, env=env, cwd=str(ROOT), stdout=_log, stderr=_log)
_stop.set()
say(f"评测进程退出，返回码 {rc}，总耗时 {int(time.time() - t0)} 秒")
say("=" * 60)
_log.close()
sys.exit(rc)
