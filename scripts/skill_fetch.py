#!/usr/bin/env python3
r"""skill_fetch — SGME 技能包跨机获取客户端（T-282；技能包通道设计 v0.3 §4.6）。

单文件、纯标准库（urllib / tarfile / hashlib），零第三方依赖：可直接裸拷到
任意机器（NAS 容器 / 笔记本 / 任意宿主 shell）使用。

用途：跨机 agent（本机与 SGME 服务端不在同一文件系统）从服务端拉取技能的
**完整包**（SKILL.md + 随附文件，与 materialize 落盘同口径的文件集），
字节保真地落盘到本地 <dest>/<name>/，供执行脚本 / 安装适配器使用。

流程（本脚本内闭环）：
    GET /v1/skills/{name}/package
    → 解析 MANIFEST.sha256（归档根、与 <name>/ 平级）
    → 逐文件 sha256 校验 + 文件集完整性对照（多一个 / 少一个文件都算失败）
    → 安全解包（全量成员校验；py≥3.12 叠加 tarfile 官方 data 过滤器）
    → 校验后自删 MANIFEST → 原子改名落盘 <dest>/<name>/
    → stdout 输出 path 与聚合 sha256

失败不留残件：全部工作在 <dest> 下的临时目录内进行；任何失败（下载中断 /
清单校验失败 / 哈希不符 / 解包被拒 / 落点已存在）都在临时目录清理后中止，
<dest>/<name>/ 不会出现半成品，也不散落临时文件。

用法::

    skill_fetch --name <技能名> --dest <落点目录> [--base http://<host>:9910]
                [--key-env NAME | --key-file FILE]

认证与地址（零明文惯例，参考 scripts/sgme_cred.py）
    - 地址：--base 优先；未给读环境变量 SGME_BASE_URL；再读 ~/.sgme/install.json
      的 http.host / http.port（都未配置时 --base 必填报错）；
    - Key：--key-file 文件 → --key-env 环境变量 → 默认环境变量 SGME_AGENT_KEY；
      值只驻留内存，任何输出 / 错误都不打印 key（本地预检拒绝含空白 / 非
      ASCII 可见字符的 key，防请求层异常消息把值回显出来）；
    - 测试缝：环境变量 SGME_INSTALL_JSON 可覆盖 install.json 路径。

代理纪律
    HTTP 一律经 ProxyHandler({}) 显式绕过环境代理（局域网直连；PC 上 Clash 系
    会接管内网流量；对标 sgme_cred 与 SGME 自身 trust_env=False 先例）。

服务端契约（对应设计 §4.1 / §4.3）
    - 响应：tar.gz 流；归档结构 = MANIFEST.sha256 与 <name>/ 在归档根平级；
    - 响应头 X-Skill-Sha256（= canonical manifest 文本 sha256 的聚合哈希）与
      X-Skill-Files（= <name>/ 下文件数，不含 MANIFEST）在场时逐一对账；
    - MANIFEST 行格式：<sha256 十六进制><两个空格><相对路径>\n（UTF-8 / LF，
      相对路径基点 = 技能根 <name>/）；
    - 404 / 405：按「服务端版本不足」文案处置（package 端点未部署）。

退出码：0 成功；1 运行时 / 接口 / 完整性错误；2 用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROG = "skill_fetch"
USER_AGENT = PROG + "/1.0"
DOWNLOAD_TIMEOUT_S = 30.0
DEFAULT_PORT = 9910
DEFAULT_KEY_ENV = "SGME_AGENT_KEY"
ENV_BASE_URL = "SGME_BASE_URL"
ENV_INSTALL_JSON = "SGME_INSTALL_JSON"
MANIFEST_NAME = "MANIFEST.sha256"
_FORMAT_PARAM = "tar.gz"
_CHUNK = 262144  # 256 KiB：流式下载 / 逐文件哈希分块

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class FetchError(Exception):
    """运行时 / 接口 / 完整性错误（消息面向用户，绝不包含 key 值）。"""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# 哈希工具
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """分块计算文件 sha256（大文件不进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 参数值校验
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str:
    """校验技能名 = 单个路径组件（防经 --name 逃逸落点目录）。"""
    n = str(name or "").strip()
    if not n:
        raise FetchError("--name 不能为空", exit_code=2)
    if any(ch in n for ch in "/\\\r\n\0") or n in (".", ".."):
        raise FetchError(
            f"--name 非法（应为单个技能名、不含路径分隔符）：{n!r}", exit_code=2
        )
    return n


# ---------------------------------------------------------------------------
# 地址解析（--base → SGME_BASE_URL → install.json http 段；零明文）
# ---------------------------------------------------------------------------


def install_json_path() -> Path:
    """install.json 路径：``SGME_INSTALL_JSON`` 覆盖（测试缝）→ ~/.sgme/install.json。"""
    override = os.environ.get(ENV_INSTALL_JSON)
    if override:
        return Path(override)
    return Path.home() / ".sgme" / "install.json"


def load_install_json() -> dict:
    """读取 install.json（零明文：只含地址与 Key 的环境变量名引用）。"""
    path = install_json_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FetchError(
            f"未找到安装清单：{path}\n"
            f"提示：本机注册过 SGME 时该文件可由 scripts/install_client.py 生成；"
            f"或用环境变量 {ENV_INSTALL_JSON} 指定路径，或改用 --base 显式给出地址。"
        ) from None
    except (OSError, ValueError) as e:
        raise FetchError(f"安装清单不可读或格式非法：{path}（{e}）") from None
    if not isinstance(data, dict):
        raise FetchError(f"安装清单格式非法（应为 JSON 对象）：{path}")
    return data


def _normalize_base(url: str) -> str:
    u = url.strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        raise FetchError(f"地址形态非法（需以 http:// 或 https:// 开头）：{u}")
    return u


def resolve_base(cli_base: str | None) -> str:
    """地址解析：--base → SGME_BASE_URL → install.json http.host/port。"""
    if cli_base and cli_base.strip():
        return _normalize_base(cli_base)
    env_url = (os.environ.get(ENV_BASE_URL) or "").strip()
    if env_url:
        return _normalize_base(env_url)
    install = load_install_json()
    http = install.get("http")
    http = http if isinstance(http, dict) else {}
    host = str(http.get("host") or "").strip()
    if not host:
        raise FetchError(
            f"未找到 SGME 地址：--base 未给、{ENV_BASE_URL} 未设置，"
            f"install.json 也缺少 http.host（{install_json_path()}）——"
            f"请显式传入 --base http://<host>:<port>"
        )
    if host in ("0.0.0.0", "::"):
        # 服务端绑任意地址时按本机回环连接（同 sgme_cred 口径）
        host = "127.0.0.1"
    port = http.get("port") or DEFAULT_PORT
    return f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# Key 解析（--key-file → --key-env → 默认环境变量；值绝不回显）
# ---------------------------------------------------------------------------


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_key_file_text(text: str, path: Path) -> str:
    """解析密钥文件：裸值（单行无 '='）或 dotenv 形态（取 SGME_AGENT_KEY 行）。

    返回只含值本身；错误信息不含文件内容。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in normalized.split("\n")]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise FetchError(f"密钥文件为空：{path}")
    if len(lines) == 1 and "=" not in lines[0]:
        return _strip_quotes(lines[0])
    pairs: dict[str, str] = {}
    for ln in lines:
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        pairs[k.strip()] = _strip_quotes(v.strip())
    if not pairs:
        raise FetchError(f"密钥文件形态无法识别：{path}（单行应只含 key 值）")
    if DEFAULT_KEY_ENV in pairs:
        return pairs[DEFAULT_KEY_ENV]
    if len(pairs) == 1:
        return next(iter(pairs.values()))
    raise FetchError(
        f"密钥文件未包含 {DEFAULT_KEY_ENV} 赋值、且不是单一 KEY=VALUE：{path}"
        f"（找到键名：{', '.join(sorted(pairs)) or '无'}）"
    )


def read_key_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise FetchError(f"密钥文件不存在：{path}") from None
    except (OSError, ValueError) as e:
        raise FetchError(f"密钥文件不可读：{path}（{e}）") from None
    return parse_key_file_text(text, path)


def check_key_shape(value: str, source: str) -> str:
    """拒绝含空白 / 非 ASCII 可见字符的 key（防请求层异常消息回显值本身）。"""
    if not value or any((ord(ch) < 0x21 or ord(ch) > 0x7E) for ch in value):
        raise FetchError(
            f"agent key 含空白或非 ASCII 可见字符（来源：{source}）——"
            f"请检查变量 / 文件内容（取值不回显）"
        )
    return value


def resolve_key(key_env: str | None, key_file: str | None) -> str:
    """Key 解析优先级：--key-file → --key-env NAME → 默认 SGME_AGENT_KEY。"""
    if key_file:
        return check_key_shape(read_key_file(Path(key_file)), f"--key-file {key_file}")
    name = (key_env or "").strip() or DEFAULT_KEY_ENV
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise FetchError(
            f"未找到 agent key：环境变量 {name} 未设置或为空。\n"
            f"提示：用 --key-env 指定其它变量名，或用 --key-file 指定密钥文件；"
            f"key 值不进对话、不落盘。"
        )
    return check_key_shape(value, f"环境变量 {name}")


# ---------------------------------------------------------------------------
# HTTP（显式绕过环境代理；错误文案不含 key）
# ---------------------------------------------------------------------------


def _read_body(err: urllib.error.HTTPError) -> str:
    """读错误响应正文（尽力，不因读取失败二次报错）。"""
    try:
        return err.read().decode("utf-8", "replace")
    except (OSError, ValueError):
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
    if status in (404, 405):
        return (
            f"服务端版本不足：未找到技能包端点（HTTP {status}）。{detail}\n"
            f"技能包跨机获取通道（GET /v1/skills/{{name}}/package）需服务端支持；"
            f"请升级 SGME 服务端后重试；若服务端已支持，请确认技能名是否存在。"
        )
    if status in (401, 403):
        return (
            f"认证失败（HTTP {status}）：{detail}\n"
            f"排障提示：检查 --key-env / --key-file 指向的是否为有效 agent key"
            f"（值不进对话）。"
        )
    if status == 429:
        return f"请求被限流（HTTP 429）：{detail}"
    return f"请求失败（HTTP {status}）：{detail}"


def _download_package(base_url: str, api_key: str, name: str, dest_file: Path) -> dict:
    """GET 技能包 → 流式写入 dest_file；返回相关响应头字典（无头时为 None）。"""
    quoted = urllib.parse.quote(name, safe="")
    url = f"{base_url}/v1/skills/{quoted}/package?format={_FORMAT_PARAM}"
    req = urllib.request.Request(
        url,
        headers={
            "X-API-Key": api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/gzip, application/octet-stream",
        },
        method="GET",
    )
    # 显式绕过环境代理：PC 上 Clash 系会接管内网流量（对标 trust_env=False 先例）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            with open(dest_file, "wb") as fh:
                shutil.copyfileobj(resp, fh, _CHUNK)
            return {
                "sha256": resp.headers.get("X-Skill-Sha256"),
                "files": resp.headers.get("X-Skill-Files"),
            }
    except urllib.error.HTTPError as e:  # 注意：HTTPError ⊂ URLError，先捕
        raise FetchError(_http_error_message(e.code, _read_body(e))) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", "") or e
        raise FetchError(
            f"无法连接 SGME：{url}\n"
            f"原因：{reason}\n"
            f"排障提示：① 检查 --base / install.json 的地址与端口；② 确认服务在运行；"
            f"③ 本脚本已显式绕过环境代理（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY），"
            f"如需代理请确认其可达该地址。"
        ) from None
    except ValueError as e:
        # 头值 / URL 非法等：原始消息可能携带值，不透传原文（防 key 回显）
        raise FetchError(
            f"请求构造失败（参数含非法字符）：{type(e).__name__}"
        ) from None


# ---------------------------------------------------------------------------
# 归档安全（解包前后路径检查；成员预检两版本一致执行）
# ---------------------------------------------------------------------------


def _normalize_member_name(raw: str) -> str:
    """去前导 './'（容忍归档器差异）；只用于校验 / 对照，不改写解包行为。"""
    n = raw
    while n.startswith("./"):
        n = n[2:]
    return n


def member_path_problem(path: str) -> str | None:
    """成员 / 清单相对路径安全检查；返回问题描述（None = 安全）。"""
    if "\\" in path:
        return "含反斜杠"
    p = path.removesuffix("/")
    if not p:
        return "为空"
    if p.startswith("/"):
        return "绝对路径"
    if _DRIVE_RE.match(p):
        return "盘符路径"
    if any(seg == ".." for seg in p.split("/")):
        return "含 .. 段"
    return None


def check_tar_member(member: tarfile.TarInfo) -> str | None:
    """单个归档成员安全校验；返回问题描述（None = 安全）。

    链接成员一律拒绝：服务端包文件集明确「链接排除 + 不打包」（设计 §4.2），
    合法技能包不会携带链接（生产全量扫描 = 0）；低版本手工校验与高版本
    data 过滤器由此获得一致行为。
    """
    if not member.name:
        return "成员名为空"
    norm = _normalize_member_name(member.name)
    problem = member_path_problem(norm)
    if problem:
        return problem
    if member.issym() or member.islnk():
        return "链接成员（技能包不应携带链接）"
    if member.ischr() or member.isblk() or member.isfifo():
        return "特殊文件成员"
    if not (member.isfile() or member.isdir()):
        return "非普通文件 / 目录成员"
    return None


def extract_archive(tf: tarfile.TarFile, target: Path) -> None:
    """安全解包：先全量成员预检（所有解释器版本一致执行），再按版本解包。

    - Python ≥3.12：``extractall(filter="data")``（官方过滤器兜底）；
    - 低版本：手工校验通过后 ``extractall()``（危险形态已在预检中逐条拒绝）。
    """
    for member in tf.getmembers():
        problem = check_tar_member(member)
        if problem:
            raise FetchError(f"归档成员不安全（{problem}）：{member.name!r}")
    target.mkdir(parents=True, exist_ok=True)
    if sys.version_info >= (3, 12):
        tf.extractall(path=str(target), filter="data")
    else:
        tf.extractall(path=str(target))


# ---------------------------------------------------------------------------
# MANIFEST 校验（canonical 行格式钉死；宁严勿松）
# ---------------------------------------------------------------------------


def parse_manifest(raw: bytes) -> list[tuple[str, str]]:
    r"""解析 canonical MANIFEST.sha256：每行 ``<sha256><两个空格><相对路径>\n``。

    返回 [(hexdigest, relpath)]（保序）；格式 / 路径安全 / 重复任何一项不合法
    即抛 FetchError（清单是完整性对账的唯一依据）。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError(f"{MANIFEST_NAME} 不是合法 UTF-8") from None
    if not text:
        raise FetchError(f"{MANIFEST_NAME} 为空（至少应包含 SKILL.md）")
    if "\r" in text:
        raise FetchError(f"{MANIFEST_NAME} 含 CR 字符（规范要求 LF 换行）")
    if not text.endswith("\n"):
        raise FetchError(f"{MANIFEST_NAME} 行尾缺少 LF（应为每行以 \\n 结尾）")
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.split("\n")[:-1]:
        if len(line) < 67 or line[64:66] != "  " or not _HEX64_RE.match(line[:64]):
            raise FetchError(
                f"{MANIFEST_NAME} 行格式非法（应为 <sha256 十六进制><两个空格><相对路径>）："
                f"{line[:80]!r}"
            )
        rel = line[66:]
        if rel.endswith("/"):
            raise FetchError(f"{MANIFEST_NAME} 路径不应以 / 结尾（只列文件）：{rel!r}")
        problem = member_path_problem(rel)
        if problem:
            raise FetchError(f"{MANIFEST_NAME} 相对路径不安全（{problem}）：{rel!r}")
        if rel in seen:
            raise FetchError(f"{MANIFEST_NAME} 相对路径重复：{rel!r}")
        seen.add(rel)
        entries.append((line[:64], rel))
    if not entries:
        raise FetchError(f"{MANIFEST_NAME} 为空（至少应包含 SKILL.md）")
    return entries


def _check_archive_structure(members: list[tarfile.TarInfo], name: str) -> None:
    """归档根只允许 MANIFEST.sha256 与 <name>/ 两个条目。"""
    roots = {_normalize_member_name(m.name).split("/", 1)[0] for m in members}
    expected = {MANIFEST_NAME, name}
    if roots != expected:
        raise FetchError(
            f"归档根条目异常：期望 {sorted(expected)}，实际 {sorted(roots)}"
        )


def _read_manifest_member(tf: tarfile.TarFile, members: list[tarfile.TarInfo]) -> bytes:
    """取归档根 MANIFEST.sha256 原始字节（canonical 文本 = 聚合哈希的原料）。"""
    found = [m for m in members if _normalize_member_name(m.name) == MANIFEST_NAME]
    if not found:
        raise FetchError(f"归档缺少 {MANIFEST_NAME}")
    if len(found) > 1:
        raise FetchError(f"归档含多个 {MANIFEST_NAME}")
    member = found[0]
    if not member.isfile():
        raise FetchError(f"{MANIFEST_NAME} 不是普通文件")
    fh = tf.extractfile(member)
    if fh is None:
        raise FetchError(f"{MANIFEST_NAME} 不可读")
    return fh.read()


def _check_headers(headers: dict, aggregate: str, files: int) -> None:
    """X-Skill-Sha256 / X-Skill-Files 在场时逐一对账（不在场不阻塞）。"""
    got = str(headers.get("sha256") or "").strip()
    if got and got.lower() != aggregate:
        raise FetchError(
            f"响应头 X-Skill-Sha256 与清单不一致：{got[:16]}… ≠ {aggregate[:16]}…"
        )
    got_n = str(headers.get("files") or "").strip()
    if got_n:
        try:
            n = int(got_n)
        except ValueError:
            raise FetchError(f"响应头 X-Skill-Files 非法：{got_n!r}") from None
        if n != files:
            raise FetchError(f"响应头 X-Skill-Files（{n}）与清单行数（{files}）不一致")


def _member_files_under(members: list[tarfile.TarInfo], name: str) -> set[str]:
    """归档成员中 <name>/ 下的文件相对路径集合。"""
    rels: set[str] = set()
    prefix = name + "/"
    for m in members:
        norm = _normalize_member_name(m.name)
        if norm == MANIFEST_NAME:
            continue
        if norm.startswith(prefix) and m.isfile():
            rels.add(norm[len(prefix) :])
    return rels


def _compare_file_sets(expected: set[str], actual: set[str], where: str) -> None:
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        raise FetchError(
            f"包内文件集与清单不一致（对照 {where}）："
            f"多出 {extra or '无'}；缺少 {missing or '无'}"
        )


def verify_staged_tree(stage: Path, name: str, entries: list[tuple[str, str]]) -> None:
    """解包后逐文件哈希校验 + 文件集 / 路径安全后置检查。"""
    root = stage / name
    if not root.is_dir():
        raise FetchError(f"归档缺少技能目录：{name}/")
    base = root.resolve()
    for digest, rel in entries:
        p = root / rel
        # 解包后复检：realpath 不得越出技能根（防经路径逃逸）
        try:
            p.resolve().relative_to(base)
        except ValueError:
            raise FetchError(f"包内路径越出技能根：{rel}") from None
        if p.is_symlink() or not p.is_file():
            raise FetchError(f"包内文件缺失或类型异常：{rel}")
        got = sha256_file(p)
        if got != digest:
            raise FetchError(f"文件哈希不匹配（清单与内容不一致）：{rel}")
    # 完整性：磁盘文件集 == 清单（多一个也算失败）；同时复核无链接残留
    on_disk: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for entry_name in dirnames + filenames:
            p = Path(dirpath) / entry_name
            if p.is_symlink():
                raise FetchError(f"解包产物含链接：{p.relative_to(root).as_posix()}")
        for fn in filenames:
            on_disk.add((Path(dirpath) / fn).relative_to(root).as_posix())
    _compare_file_sets({rel for _, rel in entries}, on_disk, "解包产物")


# ---------------------------------------------------------------------------
# 主流程：下载 → 校验 → 解包 → 原子落盘（失败不留残件）
# ---------------------------------------------------------------------------


def run_fetch(
    name: str, dest_root: Path, base_url: str, api_key: str
) -> tuple[Path, str]:
    """完整获取流程；返回 (落盘路径, 聚合 sha256)。失败抛 FetchError 且不留残件。"""
    final = dest_root / name
    if final.exists():
        raise FetchError(f"目标已存在：{final}（如需重新获取请先移除该目录，防误覆盖）")
    created_root = not dest_root.exists()
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FetchError(f"落点目录不可用：{dest_root}（{e}）") from None
    if not dest_root.is_dir():
        raise FetchError(f"落点不是目录：{dest_root}")

    try:
        work = Path(tempfile.mkdtemp(prefix=f".{name}.fetch-tmp-", dir=str(dest_root)))
    except OSError as e:
        raise FetchError(f"无法在落点目录创建临时目录：{dest_root}（{e}）") from None
    try:
        archive = work / "package.tar.gz"
        headers = _download_package(base_url, api_key, name, archive)
        stage = work / "stage"

        try:
            with tarfile.open(str(archive), "r:gz") as tf:
                members = tf.getmembers()
                for member in members:  # 成员安全预检（解包前）
                    problem = check_tar_member(member)
                    if problem:
                        raise FetchError(
                            f"归档成员不安全（{problem}）：{member.name!r}"
                        )
                _check_archive_structure(members, name)
                raw_manifest = _read_manifest_member(tf, members)
                entries = parse_manifest(raw_manifest)
                aggregate = hashlib.sha256(raw_manifest).hexdigest()
                _check_headers(headers, aggregate, len(entries))
                _compare_file_sets(
                    {rel for _, rel in entries},
                    _member_files_under(members, name),
                    "归档成员",
                )
                try:
                    extract_archive(tf, stage)
                except FetchError:
                    raise
                except (tarfile.TarError, OSError, ValueError) as e:
                    raise FetchError(f"解包失败：{e}") from None
        except FetchError:
            raise
        except (tarfile.TarError, OSError, EOFError) as e:
            raise FetchError(f"归档不可读（不是合法 tar.gz？）：{e}") from None

        verify_staged_tree(stage, name, entries)
        try:
            (stage / MANIFEST_NAME).unlink()  # 校验后自删 MANIFEST（设计 §4.3）
        except OSError as e:
            raise FetchError(f"清理 {MANIFEST_NAME} 失败：{e}") from None
        leftover = sorted(p.name for p in stage.iterdir() if p.name != name)
        if leftover:
            raise FetchError(f"归档根含预期之外的条目：{leftover}")

        try:
            os.rename(str(stage / name), str(final))  # 同卷原子改名
        except OSError as e:
            raise FetchError(f"落盘改名失败：{final}（{e}）") from None
        return final, aggregate
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
        if created_root:
            # 起始时由本脚本新建的 --dest：失败时若仍为空则一并清理（不留残件）
            try:
                if dest_root.exists() and not any(dest_root.iterdir()):
                    dest_root.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI
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


def _write_line(text: str) -> None:
    """文本写 stdout（带换行）；编码不支持时退化为 backslashreplace。"""
    line = text + "\n"
    try:
        sys.stdout.write(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(line.encode(enc, "backslashreplace").decode(enc, "replace"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "SGME 技能包跨机获取：下载 → 清单校验 → 安全解包 → 落盘" "（单文件零依赖）"
        ),
        epilog=(
            "示例：skill_fetch --name adapter-dsh --dest ./skills"
            "（地址与 key 走 install.json / SGME_AGENT_KEY）"
        ),
    )
    parser.add_argument(
        "--name",
        "-n",
        required=True,
        metavar="NAME",
        help="技能名（与 SGME 入库名一致）",
    )
    parser.add_argument(
        "--dest",
        "-d",
        required=True,
        metavar="DIR",
        help="落点目录；产物 = DIR/<name>/（已存在则拒绝，不覆盖）",
    )
    parser.add_argument(
        "--base",
        "-b",
        metavar="URL",
        help=(
            "SGME HTTP 地址（如 http://<host>:9910）；"
            "缺省读 SGME_BASE_URL / ~/.sgme/install.json"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--key-env",
        metavar="NAME",
        help=f"key 所在环境变量名（默认 {DEFAULT_KEY_ENV}）",
    )
    group.add_argument(
        "--key-file",
        metavar="FILE",
        help="从文件读 key（裸值或 dotenv 形态；与 --key-env 互斥）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：返回进程退出码（argparse 用法错误时以其自身退出码 2 退出）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    _prepare_stdio()
    args = _build_parser().parse_args(argv)
    try:
        name = validate_name(args.name)
        base_url = resolve_base(args.base)
        api_key = resolve_key(args.key_env, args.key_file)
        dest_root = Path(os.path.abspath(os.path.expanduser(args.dest)))
        final, aggregate = run_fetch(name, dest_root, base_url, api_key)
        _write_line(f"path={final}")
        _write_line(f"sha256={aggregate}")
        return 0
    except FetchError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:  # pragma: no cover - 交互中断场景
        print(f"{PROG}: 已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
