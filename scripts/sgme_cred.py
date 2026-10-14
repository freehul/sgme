#!/usr/bin/env python3
r"""sgme-cred — SGME 凭据模块客户端工具（T-274；凭据模块设计 v0.4 §五）。

单文件、纯标准库（urllib），零第三方依赖：可直接裸拷到任意机器
（NAS 容器 / 笔记本）使用。

用法::

    sgme-cred list                                        # 列键名（不含值）
    sgme-cred get KEY                                     # stdout 仅输出值本身
    sgme-cred export [--only A,B | --prefix P] [--out F]  # 导出 dotenv（默认 stdout）
    sgme-cred run [--only A,B | --prefix P] -- <cmd...>   # 拉取 → 注入环境变量 → 执行子进程

    ``--only`` 与 ``--prefix`` 互斥（同给报错）；``run`` 必须带 ``--`` 分隔符，
    其后的参数原样（不经 shell）交给子进程；``run`` 透传子进程退出码。

认证与地址（零明文）

    - 地址：环境变量 ``SGME_BASE_URL`` 优先（如 http://host:9910）；未设置时读
      ``~/.sgme/install.json`` 的 ``http.host`` / ``http.port``。
    - Key：读 install.json 的 ``keys.agent``（**环境变量名引用**），再取该
      环境变量的值；缺失时给出排障提示。本工具不硬编码任何 key、无默认 key。
    - 测试缝：环境变量 ``SGME_INSTALL_JSON`` 可覆盖 install.json 路径
      （测试与多清单场景）。

代理纪律

    HTTP 一律经 ``ProxyHandler({})`` 显式绕过环境代理（PC 上 Clash 系会接管
    内网流量；对标 SGME 自身 ``trust_env=False`` 先例）；请求超时 10s；
    连接失败时输出「地址/代理」排障提示。

dotenv 转义规则（本工具负责格式化；防 ``sgme-cred export > .env`` 被
shell/解析器误读）

    - 值匹配安全集（非空、无首尾空白，且仅含 ``A-Za-z0-9_./:@%+,=-``）
      → 裸写 ``KEY=value``；
    - 否则用双引号包裹，并转义五个字符：``\`` → ``\\``、``"`` → ``\"``、
      换行 → ``\n``、回车 → ``\r``、制表 → ``\t``；``$``、反引号等其余字符
      进入双引号但不追加转义（shell ``source`` 语义请使用方自行评估）。

接口（服务端契约）

    ``GET /v1/credentials`` → JSON 列表；``GET /v1/credentials/{key}`` → JSON
    单值；``GET /v1/credentials/export?only=|prefix=`` → JSON
    ``{"credentials": {"KEY": 原始值, ...}, "count": N}``（值原样、不转义；
    dotenv 转义与 ``KEY=VALUE`` 行格式化由本工具完成）。``export`` 与 ``run``
    共用**同一次** export 请求取原始值（不逐键 N+1 请求）。错误信封统一
    ``{"error": {"code", "message"}}``。

退出码：``0`` 成功；``1`` 运行时/接口/配置错误；``2`` 用法错误；
``run`` 透传子进程退出码。所有输出与错误信息均不打印凭据值。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROG = "sgme-cred"
TIMEOUT_S = 10.0
DEFAULT_PORT = 9910
INSTALL_JSON_ENV = "SGME_INSTALL_JSON"
BASE_URL_ENV = "SGME_BASE_URL"
USER_AGENT = PROG + "/1.0"

# dotenv 安全集：非空且仅含这些字符的值可裸写（正则同时天然排除一切空白，
# 满足「无首尾空白」要求）
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")

# 双引号转义表：\ " 换行 回车 制表（其余字符原样保留）
_ESCAPE_MAP = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class CredError(Exception):
    """运行时/接口/配置错误（消息面向用户，绝不包含凭据值）。"""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# dotenv 转义规则（§五：由客户端负责格式化）
# ---------------------------------------------------------------------------


def is_safe_value(value: str) -> bool:
    """判断值是否匹配安全集（非空、无首尾空白、仅 A-Za-z0-9_./:@%+,=-）。"""
    return bool(value) and _SAFE_VALUE_RE.match(value) is not None


def _escape_dquoted(value: str) -> str:
    r"""逐字符转义（避免 replace 顺序陷阱）：\ " 换行 回车 制表。"""
    return "".join(_ESCAPE_MAP.get(ch, ch) for ch in value)


def format_dotenv(pairs: list[tuple[str, str]]) -> str:
    r"""把 (KEY, 值) 序列渲染为 dotenv 正文（每行以 ``\n`` 结尾）。

    值匹配安全集 → 裸写 ``KEY=value``；否则双引号包裹并转义
    ``\`` ``"`` 换行 回车 制表。键序保持输入顺序。
    """
    lines = []
    for key, value in pairs:
        if is_safe_value(value):
            lines.append(f"{key}={value}")
        else:
            lines.append(f'{key}="{_escape_dquoted(value)}"')
    return "".join(line + "\n" for line in lines)


# ---------------------------------------------------------------------------
# 配置解析：地址 + Key（零明文）
# ---------------------------------------------------------------------------


def install_json_path() -> Path:
    """install.json 路径：``SGME_INSTALL_JSON`` 覆盖（测试缝）→ ~/.sgme/install.json。"""
    override = os.environ.get(INSTALL_JSON_ENV)
    if override:
        return Path(override)
    return Path.home() / ".sgme" / "install.json"


def load_install_json() -> dict:
    """读取 install.json（零明文：只含地址与 Key 的环境变量名引用）。"""
    path = install_json_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CredError(
            f"未找到安装清单：{path}\n"
            f"提示：请先在本机注册 SGME（scripts/install_client.py 可生成清单），"
            f"或用环境变量 {INSTALL_JSON_ENV} 指定 install.json 路径。"
        ) from None
    except (OSError, ValueError) as e:
        raise CredError(f"安装清单不可读或格式非法：{path}（{e}）") from None
    if not isinstance(data, dict):
        raise CredError(f"安装清单格式非法（应为 JSON 对象）：{path}")
    return data


def resolve_base_url(install: dict) -> str:
    """地址解析：``SGME_BASE_URL`` 优先，否则 install.json ``http.host/port``。"""
    env_url = (os.environ.get(BASE_URL_ENV) or "").strip()
    if env_url:
        return env_url.rstrip("/")
    http = install.get("http")
    http = http if isinstance(http, dict) else {}
    host = str(http.get("host") or "").strip()
    if not host:
        raise CredError(
            f"未找到 SGME 地址：环境变量 {BASE_URL_ENV} 未设置，"
            f"install.json 也缺少 http.host（{install_json_path()}）"
        )
    if host in ("0.0.0.0", "::"):
        # 服务端绑任意地址时按本机回环连接
        host = "127.0.0.1"
    port = http.get("port") or DEFAULT_PORT
    return f"http://{host}:{port}"


def resolve_api_key(install: dict) -> str:
    """Key 解析：install.json ``keys.agent``（环境变量名引用）→ 读该环境变量值。

    不硬编码任何 key、无默认 key；值只驻留内存，绝不落盘/打印。
    """
    keys = install.get("keys")
    keys = keys if isinstance(keys, dict) else {}
    env_name = str(keys.get("agent") or "").strip()
    if not env_name:
        raise CredError(
            f"install.json 缺少 keys.agent（agent key 的环境变量名引用）："
            f"{install_json_path()}"
        )
    value = (os.environ.get(env_name) or "").strip()
    if not value:
        raise CredError(
            f"install.json 指向的环境变量 {env_name} 未设置或为空——"
            f"请先在本机导出该变量（agent key 值不进对话、不落盘）"
        )
    return value


def _resolve_client() -> tuple[str, str]:
    """解析 (base_url, api_key)；任一缺失抛 CredError（排障提示见消息）。"""
    install = load_install_json()
    return resolve_base_url(install), resolve_api_key(install)


# ---------------------------------------------------------------------------
# HTTP（显式绕过环境代理）
# ---------------------------------------------------------------------------


def _http_get(
    base_url: str, api_key: str, path: str, params: dict | None = None
) -> str:
    """GET 请求，返回 UTF-8 正文；非 200 抛 CredError（错误信封友好化）。"""
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"X-API-Key": api_key, "User-Agent": USER_AGENT},
        method="GET",
    )
    # 显式绕过环境代理：PC 上 Clash 系会接管内网流量（对标 trust_env=False 先例）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:  # 注意：HTTPError ⊂ URLError，先捕
        raise CredError(_http_error_message(e.code, _read_body(e))) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", "") or e
        raise CredError(
            f"无法连接 SGME：{url}\n"
            f"原因：{reason}\n"
            f"排障提示：① 检查 {BASE_URL_ENV} / install.json 的地址与端口；"
            f"② 确认服务在运行；③ 本工具已显式绕过环境代理"
            f"（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY），如需代理请确认其可达该地址。"
        ) from None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CredError(f"SGME 响应不是合法 UTF-8：{url}") from None


def _read_body(err: urllib.error.HTTPError) -> str:
    """读错误响应正文（尽力，不因读取失败二次报错）。"""
    try:
        return err.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        # 读错误正文失败不掩盖原错误
        return ""


def _http_error_message(status: int, body: str) -> str:
    """把错误信封 ``{"error": {"code", "message"}}`` 渲染为中文提示（无值）。"""
    try:
        err = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    code = str(err.get("code") or "").strip()
    message = str(err.get("message") or "").strip()
    if code and message:
        detail = f"{code}: {message}"
    else:
        detail = message or code or (body or "").strip()[:300] or "服务端未返回错误详情"
    if status in (401, 403):
        return (
            f"认证失败（HTTP {status}）：{detail}\n"
            f"排障提示：检查 install.json keys.agent 指向的环境变量是否为"
            f"有效 agent key（值不进对话）。"
        )
    if status == 404:
        return f"不存在（HTTP 404）：{detail}"
    if status == 409:
        return f"冲突（HTTP 409）：{detail}"
    return f"请求失败（HTTP {status}）：{detail}"


# ---------------------------------------------------------------------------
# 输出（Windows 控制台编码兜底；字节级精确输出）
# ---------------------------------------------------------------------------


def _prepare_stdio() -> None:
    """尽力把 stdout/stderr 切到 utf-8 / LF 输出（Windows 控制台与重定向场景）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (TypeError, ValueError, OSError):
            pass


def _write_out(text: str) -> None:
    """文本写 stdout；编码不支持时退化为 backslashreplace（不因编码失败丢输出）。"""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, "backslashreplace").decode(enc, "replace"))


def _write_raw(text: str) -> None:
    """stdout 输出精确 utf-8 字节（LF 不转换）；无 buffer 时退回文本写。"""
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        try:
            buf.write(text.encode("utf-8"))
            buf.flush()
            return
        except (OSError, ValueError):
            pass
    _write_out(text)


# ---------------------------------------------------------------------------
# 参数解析辅助
# ---------------------------------------------------------------------------


def _split_only(value: str | None) -> list[str] | None:
    """``--only A,B`` → 规范化键名列表（去空白/空项）；空给报错。"""
    if value is None:
        return None
    items = [x.strip() for x in value.split(",") if x.strip()]
    if not items:
        raise CredError("--only 未包含有效键名（形如 A,B）", exit_code=2)
    return items


def _clean_prefix(value: str | None) -> str | None:
    """``--prefix P`` → 去空白；空串给报错。"""
    if value is None:
        return None
    prefix = value.strip()
    if not prefix:
        raise CredError("--prefix 不能为空", exit_code=2)
    return prefix


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_list() -> int:
    """列键名（每行一个，不含值）。"""
    base_url, api_key = _resolve_client()
    body = _http_get(base_url, api_key, "/v1/credentials")
    try:
        creds = json.loads(body).get("credentials") or []
    except (ValueError, AttributeError):
        raise CredError("列表响应格式非法（期望 JSON 对象）") from None
    for item in creds:
        name = str((item or {}).get("key_name") or "")
        if name:
            _write_raw(name + "\n")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """取单值：stdout 只输出值本身（便于 ``v=$(sgme-cred get KEY)``）。"""
    base_url, api_key = _resolve_client()
    path = "/v1/credentials/" + urllib.parse.quote(args.key, safe="")
    body = _http_get(base_url, api_key, path)
    try:
        value = json.loads(body)["value"]
    except (ValueError, TypeError, KeyError, IndexError):
        raise CredError(f"取值响应缺少 value 字段（键 {args.key}）") from None
    if not isinstance(value, str):
        raise CredError(f"取值响应的 value 类型非法（键 {args.key}）")
    _write_raw(value + "\n")
    return 0


def _fetch_export_pairs(
    base_url: str,
    api_key: str,
    only: list[str] | None,
    prefix: str | None,
) -> list[tuple[str, str]]:
    """单次 export 请求取原始值（export/run 共用；不逐键 N+1 请求）。"""
    params: dict[str, str] = {}
    if only:
        params["only"] = ",".join(only)
    elif prefix:
        params["prefix"] = prefix
    body = _http_get(base_url, api_key, "/v1/credentials/export", params)
    try:
        creds = json.loads(body)["credentials"]
    except (ValueError, TypeError, KeyError, IndexError):
        raise CredError(
            '导出响应格式非法（期望 JSON：{"credentials": {"KEY": 值}}）'
        ) from None
    if not isinstance(creds, dict):
        raise CredError("导出响应 credentials 字段应为对象（KEY→值）")
    pairs: list[tuple[str, str]] = []
    for key, value in creds.items():
        if not isinstance(value, str):
            raise CredError(f"导出响应包含非字符串值（键 {key}）")
        pairs.append((str(key), value))
    return pairs


def cmd_export(args: argparse.Namespace) -> int:
    """导出 dotenv：单次 export 请求取原始值 → 本工具按转义规则格式化输出。

    默认写 stdout；``--out FILE`` 写文件（utf-8、``\n`` 行尾）。
    """
    base_url, api_key = _resolve_client()
    pairs = _fetch_export_pairs(
        base_url, api_key, _split_only(args.only), _clean_prefix(args.prefix)
    )
    text = format_dotenv(pairs)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as e:
            raise CredError(f"无法写入文件：{args.out}（{e}）") from None
    else:
        _write_raw(text)
    return 0


def cmd_run(args: argparse.Namespace, child: list[str]) -> int:
    """拉取（单次 export 请求）→ 注入环境变量 → 执行子进程；透传退出码。

    值只在内存与环境变量中流转：不落盘、不打印；子进程继承当前环境变量，
    服务端值覆盖同名项。
    """
    base_url, api_key = _resolve_client()
    pairs = _fetch_export_pairs(
        base_url, api_key, _split_only(args.only), _clean_prefix(args.prefix)
    )
    env = os.environ.copy()
    for key, value in pairs:
        env[key] = value
    try:
        proc = subprocess.run(child, env=env, check=False)
    except OSError as e:
        raise CredError(f"无法执行命令：{child[0]}（{e}）") from None
    return proc.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器（--only/--prefix 互斥；run/export 同一套过滤参数）。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="SGME 凭据客户端：list / get / export / run（单文件零依赖）",
        epilog="示例：sgme-cred run -- python app.py ｜ sgme-cred export --out .env",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<命令>", required=True)
    sub.add_parser("list", help="列键名（不含值）")

    p_get = sub.add_parser("get", help="取单值（stdout 仅输出值本身）")
    p_get.add_argument("key", metavar="KEY", help="凭据键名")

    for name, help_text in (
        ("export", "导出 dotenv（默认 stdout；--out 写文件）"),
        ("run", "拉取→注入环境变量→执行子进程（命令以 -- 分隔）"),
    ):
        p = sub.add_parser(name, help=help_text)
        group = p.add_mutually_exclusive_group()
        group.add_argument("--only", metavar="A,B", help="仅包含指定键名（逗号分隔）")
        group.add_argument("--prefix", metavar="P", help="仅包含此前缀的键名")
        if name == "export":
            p.add_argument(
                "--out", metavar="FILE", help="写入文件（utf-8、LF）；缺省写 stdout"
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：返回进程退出码（argparse 用法错误时以其自身退出码 2 退出）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    _prepare_stdio()
    parser = _build_parser()

    child: list[str] | None = None
    if argv[:1] == ["run"]:
        # run 需要 `--` 分隔符：其后的参数原样交给子进程（不参与解析）
        if "--" not in argv[1:]:
            print(
                f"{PROG}: run 缺少 `--` 分隔符——用法："
                f"{PROG} run [--only A,B | --prefix P] -- <cmd...>",
                file=sys.stderr,
            )
            return 2
        cut = argv.index("--")
        child = argv[cut + 1 :]
        if not child:
            print(f"{PROG}: run 的 `--` 之后未提供命令", file=sys.stderr)
            return 2
        argv = argv[:cut]

    args = parser.parse_args(argv)
    try:
        if args.cmd == "list":
            return cmd_list()
        if args.cmd == "get":
            return cmd_get(args)
        if args.cmd == "export":
            return cmd_export(args)
        return cmd_run(args, child or [])
    except CredError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:  # pragma: no cover - 交互中断场景
        print(f"{PROG}: 已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
