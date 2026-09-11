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

    2026-09-11 二次加固：
    - 主流程收进 main() 并加 __main__ 守卫——此前 import 本模块会**直接拉起一次
      评测**（实测误触发了默认臂全量评测），对库/显存都是事故源；
    - 强制清空代理环境变量并设 NO_PROXY=*（宿主机残留死代理会让批量向量与判分
      全部 10061 连接被拒，详见 eval/longmemeval_eval.py 的 _no_proxy_client）。

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

# 评测专用覆盖：提炼与向量都在 PC 的 LM Studio
# （2026-09-11 拓扑定案：笔记本 LM Studio 无法无头启动，故向量模型也搬来 PC，
#   与 9B 提炼模型并存仅多占 ~0.6GB 显存；地址如需换机改这里或用环境变量覆盖）
OVERRIDES = {
    "SGME_EMBED_BASE_URL": "http://192.168.10.130:8123/v1",
    "SGME_EMBED_MODEL": "text-embedding-bge-m3-legal-euro-r7",
    "SGME_REFINE_BASE_URL": "http://192.168.10.130:8123/v1",
    "SGME_REFINE_MODEL": "qwen3.8-9b-distill",
    "SGME_REFINE_CTX": "131072",  # 批预算 = ctx − 4096 − 8%ctx = 116490
}

# 运行时可覆盖（同名环境变量优先）：切换装载档/端点时用。
# ⚠️ SGME_REFINE_CTX 的批预算（ctx−4096−8%ctx）必须满足
#    「并发数 × 批预算 ≤ LM Studio 的 -c 池」，否则单批提示词会被超窗拒绝
#    （2026-09-11 实测：-c 65536 --parallel 4 下 4 条 55K 全部失败）。
#
# 注意：覆盖只在 main() 里做（import 本模块不产生副作用）。
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _effective_overrides(environ: dict) -> dict:
    """合并默认覆盖与调用方环境变量（同名环境变量优先）。"""
    merged = dict(OVERRIDES)
    for k in merged:
        if environ.get(k):
            merged[k] = environ[k]
    return merged


def main(argv: list[str]) -> int:
    overrides = _effective_overrides(dict(os.environ))
    log_path = Path(argv[0]) if argv else ROOT / "eval" / "results" / "run.log"
    eval_args = argv[1:]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    def say(msg: str) -> None:
        _log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
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

    env.update(overrides)
    # 评测台铁律：禁走系统代理（2026-09-11 实锤——宿主机残留的死代理 env 会让批量向量
    # 与判分全部报 10061「目标计算机积极拒绝」，而引擎侧 trust_env=False 的调用正常，
    # 故障表现为「同一进程一半通一半不通」）。这里清空代理变量并设 NO_PROXY=*。
    for _pk in PROXY_VARS:
        env.pop(_pk, None)
    env["NO_PROXY"] = env["no_proxy"] = "*"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    say(f"已从 config/.env 注入 {len(loaded)} 个键（名称不打印）")
    say(f"judge 密钥 AGNESAI_API_KEY：{'已就绪' if env.get('AGNESAI_API_KEY') else '缺失 ⚠'}")
    say("已禁用系统代理（NO_PROXY=*，防死代理劫持）")
    for k, v in sorted(overrides.items()):
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
    cmd = [str(ROOT / ".venv" / "Scripts" / "python.exe"),
           str(ROOT / "eval" / "longmemeval_eval.py"), *eval_args]
    rc = subprocess.call(cmd, env=env, cwd=str(ROOT), stdout=_log, stderr=_log)
    _stop.set()
    say(f"评测进程退出，返回码 {rc}，总耗时 {int(time.time() - t0)} 秒")
    say("=" * 60)
    _log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
