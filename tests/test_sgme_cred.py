"""tests/test_sgme_cred.py：sgme-cred 客户端工具测试（T-274）。

覆盖：

① 单元——dotenv 转义矩阵（安全集/空格/引号/换行/反斜杠/$）、参数解析
   （--only/--prefix 互斥、run 缺 `--`）、地址与 key 解析（SGME_BASE_URL
   覆盖、SGME_INSTALL_JSON 测试缝、默认路径隔离真实 ~/.sgme）。
② 端到端——标准库 http.server 起假服务端（canned 数据：list/get/export/404），
   子进程真调 list / get / export / run；run 用 `python -c` 子进程断言注入
   成功，并断言**单次** export 请求（无逐键 N+1）；断言值未打印、未落盘；
   环境注入死代理验证显式绕过（Clash 系先例）。
③ 文件通道（D-37 v1.2）——file-get / file-put 子进程真调：二进制落 --out 且字节
   完整、stdout 只一行 JSON 摘要（二进制不进 stdout）；上传字节原样、--filename
   缺省/覆盖两条路径、返回 JSON 可解析、admin key 解析与缺失时的本地拒绝。

纪律：不碰真实 ~/.sgme（USERPROFILE/HOME 指向 tmp）；不出外网（仅 127.0.0.1）；
假数据一律低熵（B179 教训）。
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sgme_cred.py"


def _load_module():
    """scripts/ 非包：经 importlib 从脚本路径加载被测模块。"""
    spec = importlib.util.spec_from_file_location("sgme_cred", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sgme_cred = _load_module()

# ---------------------------------------------------------------------------
# 假数据与期望（一律低熵假值；canned 字面量不引用被测格式化器）
# ---------------------------------------------------------------------------

FAKE_AGENT_KEY = "agt_test_fake_0001"
AGENT_ENV_NAME = "SGME_TEST_AGENT_KEY"
# 文件上/下载是 admin 端点（install.json keys.admin → 该环境变量名；_write_install 同值）
FAKE_ADMIN_KEY = "adm_test_fake_0001"
ADMIN_ENV_NAME = "SGME_ADMIN_KEY"
DEAD_PROXY = "http://127.0.0.1:9"

# D-37 v1.2 文件通道 canned 数据：全字节谱 + 尾巴（非 UTF-8/NUL 全含 → 证明不是文本伪装）
FILE_KEY = "BLOB_KEY"
FILE_BYTES = bytes(range(256)) + b"binary-tail-\x00"
FILE_SHA256 = hashlib.sha256(FILE_BYTES).hexdigest()
FILE_NAME = "备份 数据.bin"

CANON_VALUES = {
    "K": "test-value-k",
    "ALPHA_KEY": "test-alpha-1",
    "BETA_KEY": "test beta value",  # 空格 → 双引号包裹
    "GAMMA_KEY": 'test-"quoted"-value',  # 双引号 → 转义
}
CANON_KEYS = list(CANON_VALUES)

EXPECTED_LINES = {
    "K": "K=test-value-k",
    "ALPHA_KEY": "ALPHA_KEY=test-alpha-1",
    "BETA_KEY": 'BETA_KEY="test beta value"',
    "GAMMA_KEY": 'GAMMA_KEY="test-\\"quoted\\"-value"',
}


def _expected_dotenv(keys):
    """期望的规范化 dotenv 正文（每行 \\n 结尾）。"""
    return "".join(EXPECTED_LINES[k] + "\n" for k in keys)


def _decode_dotenv(text: str) -> dict:
    """测试端参考解析器（format_dotenv 的逆），仅用于往返断言。"""
    plain = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
    result = {}
    for line in text.splitlines():
        key, _, rest = line.partition("=")
        if len(rest) >= 2 and rest.startswith('"') and rest.endswith('"'):
            inner = rest[1:-1]
            out = []
            i = 0
            while i < len(inner):
                if inner[i] == "\\" and i + 1 < len(inner) and inner[i + 1] in plain:
                    out.append(plain[inner[i + 1]])
                    i += 2
                else:
                    out.append(inner[i])
                    i += 1
            result[key] = "".join(out)
        else:
            result[key] = rest
    return result


def _closed_port() -> int:
    """取一个刚释放的本机端口（必然拒绝连接）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# 假服务端（标准库 http.server；仅监听 127.0.0.1）
# ---------------------------------------------------------------------------


class _FakeHandler(http.server.BaseHTTPRequestHandler):
    """canned 假服务端：list / get(200|404) / export(JSON) / 认证 401。"""

    def log_message(self, *args):
        return

    def _send(
        self, status: int, body: bytes, ctype: str, headers: dict | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self):
        srv = self.server
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        srv.requests.append((path, query))

        if self.headers.get("X-API-Key") != srv.expected_key:
            self._json(
                401,
                {"error": {"code": "unauthorized", "message": "无效的 agent key"}},
            )
            return

        if path == "/v1/credentials":
            creds = [
                {
                    "key_name": key,
                    "description": "测试凭据",
                    "created_at": "2026-10-15T00:00:00Z",
                    "updated_at": "2026-10-15T00:00:00Z",
                    "updated_by": "default(admin,http)",
                }
                for key in CANON_KEYS
            ]
            self._json(200, {"credentials": creds, "count": len(creds)})
            return

        if path == "/v1/credentials/export":
            only = (query.get("only") or [None])[0]
            prefix = (query.get("prefix") or [None])[0]
            keys = list(CANON_KEYS)
            if only:
                wanted = {x for x in only.split(",") if x}
                keys = [k for k in keys if k in wanted]
            elif prefix:
                keys = [k for k in keys if k.startswith(prefix)]
            self._json(
                200,
                {"credentials": {k: CANON_VALUES[k] for k in keys}, "count": len(keys)},
            )
            return

        # D-37 v1.2：文件下载（二进制 + 完整性响应头；必须在通用 {key} 分支之前）
        if path == f"/v1/credentials/{FILE_KEY}/file":
            self._send(
                200,
                FILE_BYTES,
                "application/octet-stream",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + urllib.parse.quote(FILE_NAME),
                    "X-Cred-Sha256": FILE_SHA256,
                    "X-Cred-Size": str(len(FILE_BYTES)),
                },
            )
            return

        if path.startswith("/v1/credentials/"):
            key = urllib.parse.unquote(path[len("/v1/credentials/") :])
            if key in CANON_VALUES:
                self._json(
                    200,
                    {
                        "key_name": key,
                        "value": CANON_VALUES[key],
                        "description": "测试凭据",
                        "updated_at": "2026-10-15T00:00:00Z",
                        "updated_by": "default(admin,http)",
                    },
                )
            else:
                self._json(
                    404,
                    {"error": {"code": "not_found", "message": f"凭据不存在：{key}"}},
                )
            return

        self._json(404, {"error": {"code": "not_found", "message": "未知路径"}})

    def do_POST(self):
        """文件上传端点（D-37 v1.2）：记录原始请求体，返回元数据 JSON。"""
        srv = self.server
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        srv.requests.append((path, query))

        if path != f"/v1/credentials/{FILE_KEY}/file":
            self._json(404, {"error": {"code": "not_found", "message": "未知路径"}})
            return
        # 上传是 admin 端点：假服务端只认 admin key（也让测试能断言 CLI 用的是哪把钥匙）
        if self.headers.get("X-API-Key") != srv.expected_admin_key:
            self._json(
                403,
                {"error": {"code": "forbidden", "message": "缺失或无权限的 X-API-Key"}},
            )
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        filename = (query.get("filename") or [""])[0]
        srv.uploads.append(
            {
                "body": body,
                "filename": filename,
                "api_key": self.headers.get("X-API-Key"),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self._json(
            200,
            {
                "ok": True,
                "key_name": FILE_KEY,
                "filename": filename,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "updated_at": "2026-10-15T00:00:00Z",
                "scope": None,
                "cred_type": "file",
                "expires_at": None,
            },
        )


@pytest.fixture()
def fake_server():
    """起假服务端（端口 0 随机）并记录请求；测试结束关闭。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.expected_key = FAKE_AGENT_KEY
    server.expected_admin_key = FAKE_ADMIN_KEY
    server.requests = []
    server.uploads = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# CLI 子进程夹具（隔离 home / 死代理 / install.json）
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_paths(tmp_path):
    """隔离目录：home（含 .sgme/install.json）+ work（子进程 cwd）。"""
    home = tmp_path / "home"
    work = tmp_path / "work"
    (home / ".sgme").mkdir(parents=True)
    work.mkdir()
    paths = SimpleNamespace(
        home=home, work=work, install=home / ".sgme" / "install.json"
    )
    _write_install(paths, port=1)
    return paths


def _write_install(paths, port: int) -> None:
    paths.install.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sgme_version": "test",
                "http": {"host": "127.0.0.1", "port": port},
                "mcp": {"port": 9913},
                "data_dir": None,
                "raw_dir": None,
                "keys": {
                    "admin": "SGME_ADMIN_KEY",
                    "agent": AGENT_ENV_NAME,
                    "bearer": "SGME_BEARER_TOKEN",
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _cli_env(
    paths, *, base_url=None, agent_key=FAKE_AGENT_KEY, admin_key=None, install=True
):
    """构造子进程环境：隔离 home、清理真实 SGME_* 变量、注入死代理。

    死代理（HTTP_PROXY 等 → 127.0.0.1:9）在所有端到端用例中都在场——
    客户端若不显式绕过环境代理，全部用例都会失败（等价 trust_env=False 纪律）。
    """
    env = os.environ.copy()
    for name in (
        "SGME_BASE_URL",
        "SGME_INSTALL_JSON",
        AGENT_ENV_NAME,
        "SGME_AGENT_KEY",
        "SGME_ADMIN_KEY",
    ):
        env.pop(name, None)
    env.update(
        {
            "USERPROFILE": str(paths.home),
            "HOME": str(paths.home),
            "HTTP_PROXY": DEAD_PROXY,
            "HTTPS_PROXY": DEAD_PROXY,
            "ALL_PROXY": DEAD_PROXY,
            "http_proxy": DEAD_PROXY,
            "https_proxy": DEAD_PROXY,
            "all_proxy": DEAD_PROXY,
        }
    )
    if base_url is not None:
        env["SGME_BASE_URL"] = base_url
    if install:
        env["SGME_INSTALL_JSON"] = str(paths.install)
    if agent_key is not None:
        env[AGENT_ENV_NAME] = agent_key
    if admin_key is not None:
        # D-37 v1.2：install.json keys.admin 指向该变量名（文件上传走 admin key）
        env[ADMIN_ENV_NAME] = admin_key
    return env


def _run_cli(paths, args, env, *, timeout=60):
    """子进程真调 sgme-cred（cwd=work）：返回 CompletedProcess（bytes 捕获）。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        env=env,
        cwd=str(paths.work),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _assert_no_values_on_disk(root: Path) -> None:
    """断言目录树中不存在任何 canned 凭据值（值未落盘）。"""
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for value in CANON_VALUES.values():
                assert value.encode("utf-8") not in data, f"凭据值落盘：{path}"


# ---------------------------------------------------------------------------
# ① 单元：dotenv 转义矩阵
# ---------------------------------------------------------------------------

SAFE_VALUES = ["abc123", "A-Z.a/b:c@d%e+f,g=h", "a=b=c", "_x_", "0"]

UNSAFE_TO_LINE = {
    "with space": '"with space"',
    "has'apostrophe": '"has\'apostrophe"',
    'has"dquote': '"has\\"dquote"',
    "has\nnewline": '"has\\nnewline"',
    "has\ttab": '"has\\ttab"',
    "has\rcr": '"has\\rcr"',
    "has\\backslash": '"has\\\\backslash"',
    "has$dollar": '"has$dollar"',
    "has`tick": '"has`tick"',
    "": '""',
    " leading": '" leading"',
    "trailing ": '"trailing "',
    "中文值": '"中文值"',
    "multi\nline\tand\\mix": '"multi\\nline\\tand\\\\mix"',
}


@pytest.mark.parametrize("value", SAFE_VALUES)
def test_format_dotenv_safe_bare(value):
    """安全集值 → 裸写 KEY=value。"""
    assert sgme_cred.format_dotenv([("KEY", value)]) == f"KEY={value}\n"


@pytest.mark.parametrize("value,expected", list(UNSAFE_TO_LINE.items()))
def test_format_dotenv_matrix(value, expected):
    """非安全集值 → 双引号包裹 + 五字符转义（含 $/反引号原样）。"""
    assert sgme_cred.format_dotenv([("KEY", value)]) == f"KEY={expected}\n"


@pytest.mark.parametrize("value", SAFE_VALUES + list(UNSAFE_TO_LINE))
def test_format_dotenv_roundtrip(value):
    """格式化 → 参考解析器还原 == 原值（注入/落盘语义一致）。"""
    text = sgme_cred.format_dotenv([("KEY", value)])
    assert _decode_dotenv(text) == {"KEY": value}


def test_format_dotenv_multi_order_and_lf():
    """多键：保持输入顺序；行尾一律 \\n，无 \\r。"""
    text = sgme_cred.format_dotenv([("B", "x y"), ("A", "plain"), ("C", "")])
    assert text == 'B="x y"\nA=plain\nC=""\n'
    assert "\r" not in text and text.endswith("\n")


# ---------------------------------------------------------------------------
# ① 单元：参数解析
# ---------------------------------------------------------------------------


def test_only_prefix_mutually_exclusive(capsys):
    """--only 与 --prefix 同给 → argparse 报错（退出码 2）。"""
    with pytest.raises(SystemExit) as ei:
        sgme_cred.main(["export", "--only", "A", "--prefix", "B"])
    assert ei.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_run_missing_dashdash(capsys):
    """run 缺 `--` 分隔符 → 报错（退出码 2），不发任何请求。"""
    assert sgme_cred.main(["run", "echo", "hi"]) == 2
    assert "--" in capsys.readouterr().err


def test_run_empty_command_after_dashdash(capsys):
    """`--` 之后未提供命令 → 报错（退出码 2）。"""
    assert sgme_cred.main(["run", "--"]) == 2
    assert "未提供命令" in capsys.readouterr().err


def test_split_only_normalization():
    """--only 规范化：去空白、丢空项；全空报错。"""
    assert sgme_cred._split_only("K, ALPHA_KEY ,,BETA_KEY ") == [
        "K",
        "ALPHA_KEY",
        "BETA_KEY",
    ]
    assert sgme_cred._split_only(None) is None
    with pytest.raises(sgme_cred.CredError):
        sgme_cred._split_only(" , ")


def test_clean_prefix_normalization():
    assert sgme_cred._clean_prefix(" BETA ") == "BETA"
    assert sgme_cred._clean_prefix(None) is None
    with pytest.raises(sgme_cred.CredError):
        sgme_cred._clean_prefix("   ")


# ---------------------------------------------------------------------------
# ① 单元：地址与 key 解析
# ---------------------------------------------------------------------------


def test_base_url_env_priority(monkeypatch):
    """SGME_BASE_URL 优先（去尾斜杠）。"""
    monkeypatch.setenv("SGME_BASE_URL", "http://host.example:9999/")
    assert (
        sgme_cred.resolve_base_url({"http": {"host": "ignored", "port": 1}})
        == "http://host.example:9999"
    )


def test_base_url_from_install_json(monkeypatch):
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    assert (
        sgme_cred.resolve_base_url({"http": {"host": "10.1.2.3", "port": 9910}})
        == "http://10.1.2.3:9910"
    )


def test_base_url_any_bind_normalized(monkeypatch):
    """install.json 记 0.0.0.0（服务端绑任意地址）→ 按本机回环连接。"""
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    assert (
        sgme_cred.resolve_base_url({"http": {"host": "0.0.0.0", "port": 9910}})
        == "http://127.0.0.1:9910"
    )


def test_base_url_missing(monkeypatch):
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    with pytest.raises(sgme_cred.CredError):
        sgme_cred.resolve_base_url({})


def test_install_json_path_override(monkeypatch, tmp_path):
    """SGME_INSTALL_JSON 覆盖清单路径（测试缝）。"""
    target = tmp_path / "custom" / "install.json"
    monkeypatch.setenv("SGME_INSTALL_JSON", str(target))
    assert sgme_cred.install_json_path() == target


def test_install_json_path_default_isolated(monkeypatch, tmp_path):
    """默认路径 = <HOME>/.sgme/install.json；HOME 指向 tmp，不碰真实 ~/.sgme。"""
    monkeypatch.delenv("SGME_INSTALL_JSON", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sgme_cred.install_json_path() == tmp_path / ".sgme" / "install.json"


def test_load_install_json_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("SGME_INSTALL_JSON", str(tmp_path / "nope.json"))
    with pytest.raises(sgme_cred.CredError) as ei:
        sgme_cred.load_install_json()
    assert "SGME_INSTALL_JSON" in str(ei.value)


def test_load_install_json_invalid(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("SGME_INSTALL_JSON", str(bad))
    with pytest.raises(sgme_cred.CredError):
        sgme_cred.load_install_json()


def test_resolve_api_key_from_referenced_env(monkeypatch):
    """keys.agent 是环境变量名引用 → 读该变量的值。"""
    monkeypatch.setenv(AGENT_ENV_NAME, FAKE_AGENT_KEY)
    assert (
        sgme_cred.resolve_api_key({"keys": {"agent": AGENT_ENV_NAME}}) == FAKE_AGENT_KEY
    )


def test_resolve_api_key_env_unset(monkeypatch):
    """被引用环境变量缺失 → 排障提示含变量名、不含任何值。"""
    monkeypatch.delenv(AGENT_ENV_NAME, raising=False)
    with pytest.raises(sgme_cred.CredError) as ei:
        sgme_cred.resolve_api_key({"keys": {"agent": AGENT_ENV_NAME}})
    message = str(ei.value)
    assert AGENT_ENV_NAME in message
    assert FAKE_AGENT_KEY not in message


def test_resolve_api_key_missing_reference():
    with pytest.raises(sgme_cred.CredError) as ei:
        sgme_cred.resolve_api_key({})
    assert "keys.agent" in str(ei.value)


# ---------------------------------------------------------------------------
# ② 端到端：子进程真调 CLI × 假服务端
# ---------------------------------------------------------------------------


def _base_url(fake_server) -> str:
    return f"http://127.0.0.1:{fake_server.server_address[1]}"


def test_e2e_list(fake_server, cli_paths):
    """list：逐行键名、无值、无 stderr。"""
    proc = _run_cli(
        cli_paths, ["list"], _cli_env(cli_paths, base_url=_base_url(fake_server))
    )
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").splitlines() == CANON_KEYS
    assert proc.stderr == b""
    for value in CANON_VALUES.values():
        assert value.encode("utf-8") not in proc.stdout
    assert fake_server.requests == [("/v1/credentials", {})]


def test_e2e_get_single_value(fake_server, cli_paths):
    """get：stdout 只输出值本身（含换行），无其它输出。"""
    proc = _run_cli(
        cli_paths,
        ["get", "ALPHA_KEY"],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 0
    assert proc.stdout == b"test-alpha-1\n"
    assert proc.stderr == b""
    _assert_no_values_on_disk(cli_paths.home)
    _assert_no_values_on_disk(cli_paths.work)


def test_e2e_get_unknown_key_404(fake_server, cli_paths):
    """坏键名：404 信封 → 中文报错、退出码 1、stdout 空。"""
    proc = _run_cli(
        cli_paths,
        ["get", "UNKNOWN_KEY"],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    err = proc.stderr.decode("utf-8")
    assert "404" in err and "UNKNOWN_KEY" in err


def test_e2e_bad_agent_key_401(fake_server, cli_paths):
    """坏 key：401 → 认证排障提示、退出码 1。"""
    proc = _run_cli(
        cli_paths,
        ["list"],
        _cli_env(
            cli_paths, base_url=_base_url(fake_server), agent_key="agt_test_wrong_0002"
        ),
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    err = proc.stderr.decode("utf-8")
    assert "401" in err and "认证失败" in err


def test_e2e_export_stdout(fake_server, cli_paths):
    """export：客户端格式化（裸写/引号/转义）后写 stdout；单次 export 请求。"""
    proc = _run_cli(
        cli_paths, ["export"], _cli_env(cli_paths, base_url=_base_url(fake_server))
    )
    assert proc.returncode == 0
    assert proc.stdout == _expected_dotenv(CANON_KEYS).encode("utf-8")
    assert proc.stderr == b""
    assert fake_server.requests == [("/v1/credentials/export", {})]


def test_e2e_export_only_filter(fake_server, cli_paths):
    """export --only：子集 + 查询参数规范化（K, ALPHA_KEY → K,ALPHA_KEY）。"""
    proc = _run_cli(
        cli_paths,
        ["export", "--only", "K, ALPHA_KEY"],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 0
    assert proc.stdout == _expected_dotenv(["K", "ALPHA_KEY"]).encode("utf-8")
    assert fake_server.requests == [
        ("/v1/credentials/export", {"only": ["K,ALPHA_KEY"]})
    ]


def test_e2e_export_prefix_filter(fake_server, cli_paths):
    proc = _run_cli(
        cli_paths,
        ["export", "--prefix", "BETA"],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 0
    assert proc.stdout == _expected_dotenv(["BETA_KEY"]).encode("utf-8")
    assert fake_server.requests == [("/v1/credentials/export", {"prefix": ["BETA"]})]


def test_e2e_export_out_file(fake_server, cli_paths):
    """export --out：utf-8、LF 行尾；stdout 静默；值只落在用户显式指定的文件。"""
    out = cli_paths.work / "cached.env"
    proc = _run_cli(
        cli_paths,
        ["export", "--out", str(out)],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 0
    assert proc.stdout == b"" and proc.stderr == b""
    data = out.read_bytes()
    assert data == _expected_dotenv(CANON_KEYS).encode("utf-8")
    assert b"\r" not in data
    for path in cli_paths.home.rglob("*"):
        if path.is_file():  # install.json 等不应含值
            for value in CANON_VALUES.values():
                assert value.encode("utf-8") not in path.read_bytes()


def test_e2e_run_injects_env(fake_server, cli_paths):
    """run：单次 export 请求取原始值 → 注入子进程环境（值为服务端原值）。

    注：嵌套子进程的 print() 在 Windows 上输出 CRLF（其自身文本模式所致），
    断言按 LF 归一后比较。
    """
    child = ["-c", "import os; print(os.environ['K']); print('BETA_KEY' in os.environ)"]
    proc = _run_cli(
        cli_paths,
        ["run", "--only", "K", "--", sys.executable, *child],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").replace("\r\n", "\n") == "test-value-k\nFalse\n"
    assert proc.stderr == b""
    # 单次 export 请求（无逐键 N+1）
    assert fake_server.requests == [("/v1/credentials/export", {"only": ["K"]})]
    _assert_no_values_on_disk(cli_paths.home)
    _assert_no_values_on_disk(cli_paths.work)


def test_e2e_run_overrides_existing_env(fake_server, cli_paths):
    """run：服务端值覆盖同名本地环境变量。"""
    env = _cli_env(cli_paths, base_url=_base_url(fake_server))
    env["K"] = "stale-local-value"
    proc = _run_cli(
        cli_paths,
        [
            "run",
            "--only",
            "K",
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['K'])",
        ],
        env,
    )
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").replace("\r\n", "\n") == "test-value-k\n"
    assert b"stale-local-value" not in proc.stdout


def test_e2e_run_exit_code_passthrough(fake_server, cli_paths):
    """run：透传子进程退出码。"""
    proc = _run_cli(
        cli_paths,
        ["run", "--", sys.executable, "-c", "import sys; sys.exit(7)"],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 7


def test_e2e_run_missing_dashdash_cli(fake_server, cli_paths):
    """CLI 层 run 缺 `--`：退出码 2、无请求发出。"""
    proc = _run_cli(
        cli_paths,
        ["run", "echo", "hi"],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert "分隔符" in proc.stderr.decode("utf-8")
    assert fake_server.requests == []


def test_e2e_base_url_from_install_json(fake_server, cli_paths):
    """地址回退链：无 SGME_BASE_URL 时读 install.json http.host/port。"""
    _write_install(cli_paths, port=fake_server.server_address[1])
    proc = _run_cli(
        cli_paths,
        ["export", "--only", "K"],
        _cli_env(cli_paths, base_url=None),
    )
    assert proc.returncode == 0
    assert proc.stdout == _expected_dotenv(["K"]).encode("utf-8")


def test_e2e_explicit_proxy_bypass(fake_server, cli_paths):
    """死代理在场（HTTP(S)_PROXY/ALL_PROXY）仍直连成功——显式绕过环境代理。"""
    env = _cli_env(cli_paths, base_url=_base_url(fake_server))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert env[name] == DEAD_PROXY
    proc = _run_cli(cli_paths, ["list"], env)
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8").splitlines() == CANON_KEYS


def test_e2e_connection_failure_hint(cli_paths):
    """服务不可达：中文报错 + 排障提示（代理/地址）、退出码 1。"""
    env = _cli_env(cli_paths, base_url=f"http://127.0.0.1:{_closed_port()}")
    proc = _run_cli(cli_paths, ["list"], env)
    assert proc.returncode == 1
    assert proc.stdout == b""
    err = proc.stderr.decode("utf-8")
    assert "无法连接" in err and "排障提示" in err


def test_e2e_missing_agent_key_env(fake_server, cli_paths):
    """被引用环境变量缺失：本地即报错（无请求发出）、提示变量名。"""
    proc = _run_cli(
        cli_paths,
        ["list"],
        _cli_env(cli_paths, base_url=_base_url(fake_server), agent_key=None),
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert AGENT_ENV_NAME in proc.stderr.decode("utf-8")
    assert fake_server.requests == []


def test_e2e_missing_install_json(cli_paths):
    """install.json 缺失：排障提示指向 SGME_INSTALL_JSON；不碰真实 ~/.sgme。"""
    env = _cli_env(cli_paths, base_url="http://127.0.0.1:1", install=False)
    env["USERPROFILE"] = str(cli_paths.work)
    env["HOME"] = str(cli_paths.work)
    proc = _run_cli(cli_paths, ["list"], env)
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert "SGME_INSTALL_JSON" in proc.stderr.decode("utf-8")

# ---------------------------------------------------------------------------
# ③ 端到端：文件 / 二进制「重要数据」通道（D-37 v1.2）
# ---------------------------------------------------------------------------


def test_e2e_file_get(fake_server, cli_paths):
    """file-get：二进制只落 --out；stdout 只有一行 JSON 摘要（sha256/size 对得上）。"""
    out = cli_paths.work / "restored.bin"
    proc = _run_cli(
        cli_paths,
        ["file-get", FILE_KEY, "--out", str(out)],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    # 字节完整（含 NUL 与非 UTF-8 字节，证明不是文本通道）
    assert out.read_bytes() == FILE_BYTES
    # stdout 只有一行 JSON 摘要
    lines = proc.stdout.decode("utf-8").splitlines()
    assert len(lines) == 1, f"stdout 必须只有一行摘要，实际 {lines!r}"
    summary = json.loads(lines[0])
    assert summary["ok"] is True and summary["key_name"] == FILE_KEY
    assert summary["size"] == len(FILE_BYTES) and summary["sha256"] == FILE_SHA256
    assert Path(summary["out"]) == out
    assert "warning" not in summary  # 服务端 X-Cred-Sha256 与本地重算一致
    assert proc.stderr == b""
    # 二进制绝不进 stdout / stderr
    assert FILE_BYTES[:64] not in proc.stdout
    assert FILE_BYTES[:64] not in proc.stderr
    # 只发一次 GET，路径正确
    assert fake_server.requests == [(f"/v1/credentials/{FILE_KEY}/file", {})]
    # 值未泄漏到其它文件（--out 之外不留副本）
    for path in cli_paths.home.rglob("*"):
        if path.is_file():
            assert FILE_BYTES[:64] not in path.read_bytes(), f"文件泄漏：{path}"


def test_e2e_file_put(fake_server, cli_paths):
    """file-put：本地字节原样上传（admin key）+ --filename 缺省/覆盖 + 返回 JSON 可解析。"""
    local = cli_paths.work / "to-upload.bin"
    local.write_bytes(FILE_BYTES)
    env = _cli_env(cli_paths, base_url=_base_url(fake_server), admin_key=FAKE_ADMIN_KEY)

    proc = _run_cli(cli_paths, ["file-put", FILE_KEY, "--file", str(local)], env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["ok"] is True and payload["cred_type"] == "file"
    assert payload["size"] == len(FILE_BYTES) and payload["sha256"] == FILE_SHA256
    assert proc.stderr == b""
    assert len(fake_server.uploads) == 1
    upload = fake_server.uploads[0]
    assert upload["body"] == FILE_BYTES  # 原样上传（无 base64 / 换行改写）
    assert upload["filename"] == "to-upload.bin"  # 缺省 = 本地文件名
    assert upload["api_key"] == FAKE_ADMIN_KEY  # 上传端点是 admin
    assert upload["content_type"] == "application/octet-stream"

    # --filename 覆盖（含中文与空格 → query 百分号编码后服务端解码回原名）
    proc = _run_cli(
        cli_paths,
        ["file-put", FILE_KEY, "--file", str(local), "--filename", FILE_NAME],
        env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert fake_server.uploads[-1]["filename"] == FILE_NAME
    assert fake_server.uploads[-1]["body"] == FILE_BYTES
    assert json.loads(proc.stdout.decode("utf-8"))["filename"] == FILE_NAME
    assert proc.stderr == b""
    assert fake_server.requests[-1] == (
        f"/v1/credentials/{FILE_KEY}/file", {"filename": [FILE_NAME]}
    )

    # admin key 缺失：本地即拒绝（不发请求、提示变量名、不打印任何 key 值）
    before = len(fake_server.uploads)
    proc = _run_cli(
        cli_paths,
        ["file-put", FILE_KEY, "--file", str(local)],
        _cli_env(cli_paths, base_url=_base_url(fake_server)),
    )
    assert proc.returncode == 1
    assert len(fake_server.uploads) == before  # 无请求发出
    err = proc.stderr.decode("utf-8")
    assert ADMIN_ENV_NAME in err and FAKE_ADMIN_KEY not in err

