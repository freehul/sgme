"""tests/test_skill_fetch.py：skill_fetch 客户端脚本测试（T-282，技能包跨机获取通道）。

覆盖：

① 单元——MANIFEST 解析（canonical 格式 / 路径安全 / 重复 / 空）、归档成员安全
   校验（`..`、绝对路径、盘符、链接、特殊文件）、key 解析优先级与形态、
   地址解析链（--base / SGME_BASE_URL / install.json）、名称校验；
② 端到端——标准库 http.server 起假服务端（仅 127.0.0.1）：下载 → 清单校验 →
   安全解包 → 原子落盘全链路；哈希不符 / 文件集不符即中止且**不留残件**；
   恶意 tar 成员样本被拒且不写盘（低版本手工校验分支单独直测）；404/405 →
   「服务端版本不足」文案；认证失败不泄露 key；死代理在场仍直连（显式绕过）；
   install.json 地址回退；目标已存在拒绝覆盖；
③ 制度——scripts/ 真源与 AI-INSTALL/ 分发副本 sha256 相等；--help 可用。

纪律：不出外网（仅 127.0.0.1 随机端口）；不碰真实 ~/.sgme（HOME/USERPROFILE 与
清单路径全指 tmp）；假数据一律低熵（B179 教训）。
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "skill_fetch.py"
DIST_COPY_PATH = ROOT / "AI-INSTALL" / "skill_fetch.py"
MANIFEST_NAME = "MANIFEST.sha256"

FAKE_AGENT_KEY = "agt_test_fake_0001"
FAKE_WRONG_KEY = "agt_test_wrong_0002"
ALT_ENV_NAME = "SGME_TEST_AGENT_KEY"
DEAD_PROXY = "http://127.0.0.1:9"
SKILL_NAME = "adapter-demo"

DEFAULT_FILES = {
    "SKILL.md": "# 演示技能\n\n正文示例。\n".encode(),
    "scripts/run.py": b"print('demo')\n",
    "corpus/金匮要略/notes.md": "# 金匮要略\n\n中文路径样本。\n".encode(),
}


def _load_module():
    """scripts/ 非包：经 importlib 从脚本路径加载被测模块。"""
    spec = importlib.util.spec_from_file_location("skill_fetch", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


skill_fetch = _load_module()


# ---------------------------------------------------------------------------
# 测试工具：canonical 清单 / 包构造（与设计 §4.3 同口径）
# ---------------------------------------------------------------------------


def _canonical_manifest(files: dict[str, bytes]) -> bytes:
    """生成 canonical 清单正文：<sha256><两个空格><路径>\\n，按路径字节序排序。"""
    lines = []
    for rel in sorted(files, key=lambda s: s.encode("utf-8")):
        digest = hashlib.sha256(files[rel]).hexdigest()
        lines.append(f"{digest}  {rel}\n")
    return "".join(lines).encode("utf-8")


def _file_member(path: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    ti = tarfile.TarInfo(path)
    ti.size = len(data)
    return ti, data


def _symlink_member(path: str, target: str) -> tuple[tarfile.TarInfo, None]:
    ti = tarfile.TarInfo(path)
    ti.type = tarfile.SYMTYPE
    ti.linkname = target
    return ti, None


def build_package(
    name: str,
    files: dict[str, bytes],
    *,
    manifest: bytes | None = None,
    extra_members: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
) -> tuple[bytes, str, int]:
    """构造 tar.gz 包 → (bytes, 聚合 sha256, 文件数)。

    ``manifest`` 缺省按 ``files`` 生成 canonical 正文；``extra_members`` 用于
    注入恶意样本（内容为 None 表示无 payload 的成员，如链接）。
    """
    manifest = _canonical_manifest(files) if manifest is None else manifest
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(MANIFEST_NAME)
        ti.size = len(manifest)
        tf.addfile(ti, io.BytesIO(manifest))
        for rel, data in files.items():
            ti = tarfile.TarInfo(f"{name}/{rel}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for member, payload in extra_members or []:
            if payload is None:
                tf.addfile(member)
            else:
                tf.addfile(member, io.BytesIO(payload))
    body = buf.getvalue()
    return body, hashlib.sha256(manifest).hexdigest(), len(files)


# ---------------------------------------------------------------------------
# 假服务端（标准库 http.server；仅监听 127.0.0.1；canned tar.gz 包）
# ---------------------------------------------------------------------------


class _FakeSkillHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默日志
        return

    def _send(self, status: int, body: bytes, ctype: str, headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
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
        srv.requests.append((parsed.path, urllib.parse.parse_qs(parsed.query)))
        srv.keys_seen.append(self.headers.get("X-API-Key"))

        if srv.status is not None:
            self._json(
                srv.status, {"error": {"code": srv.err_code, "message": "假服务端响应"}}
            )
            return
        if self.headers.get("X-API-Key") != srv.expected_key:
            self._json(
                401, {"error": {"code": "unauthorized", "message": "无效的 agent key"}}
            )
            return
        if (
            parsed.path
            == f"/v1/skills/{urllib.parse.quote(srv.skill_name, safe='')}/package"
        ):
            body, aggregate, count = srv.package
            # Content-Disposition 头值须 latin-1 可编码：非 ASCII 技能名先 URL 编码
            filename = urllib.parse.quote(srv.skill_name, safe="") + ".tar.gz"
            self._send(
                200,
                body,
                "application/gzip",
                {
                    "X-Skill-Sha256": srv.aggregate_override or aggregate,
                    "X-Skill-Files": str(
                        srv.files_override if srv.files_override is not None else count
                    ),
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
            return
        self._json(
            404, {"error": {"code": "not_found", "message": "未知技能或端点未部署"}}
        )


@pytest.fixture()
def fake_server():
    """起假服务端（端口 0 随机）并记录请求；测试结束关闭。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeSkillHandler)
    server.expected_key = FAKE_AGENT_KEY
    server.skill_name = SKILL_NAME
    server.package = build_package(SKILL_NAME, DEFAULT_FILES)
    server.status = None
    server.err_code = "not_found"
    server.aggregate_override = None
    server.files_override = None
    server.requests = []
    server.keys_seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# CLI 子进程夹具（隔离 home / 死代理）
# ---------------------------------------------------------------------------


def _base_url(fake_server) -> str:
    return f"http://127.0.0.1:{fake_server.server_address[1]}"


def _cli_env(
    home: Path,
    *,
    agent_key=FAKE_AGENT_KEY,
    env_base_url=None,
    install_json=None,
) -> dict:
    """构造子进程环境：隔离 home、清理真实 SGME_* 变量、注入死代理。

    死代理（HTTP(S)_PROXY / ALL_PROXY → 127.0.0.1:9）在所有端到端用例中都在场——
    客户端若不显式绕过环境代理，全部用例都会失败（等价 trust_env=False 纪律）。
    """
    env = os.environ.copy()
    for name in (
        "SGME_BASE_URL",
        "SGME_INSTALL_JSON",
        "SGME_AGENT_KEY",
        "SGME_ADMIN_KEY",
        ALT_ENV_NAME,
    ):
        env.pop(name, None)
    env.update(
        {
            "USERPROFILE": str(home),
            "HOME": str(home),
            "HTTP_PROXY": DEAD_PROXY,
            "HTTPS_PROXY": DEAD_PROXY,
            "ALL_PROXY": DEAD_PROXY,
            "http_proxy": DEAD_PROXY,
            "https_proxy": DEAD_PROXY,
            "all_proxy": DEAD_PROXY,
        }
    )
    if env_base_url is not None:
        env["SGME_BASE_URL"] = env_base_url
    if install_json is not None:
        env["SGME_INSTALL_JSON"] = str(install_json)
    if agent_key is not None:
        env["SGME_AGENT_KEY"] = agent_key
    return env


def _run_cli(args, env: dict, *, cwd: Path, timeout: int = 60):
    """子进程真调 skill_fetch（bytes 捕获）。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _closed_port() -> int:
    """取一个刚释放的本机端口（必然拒绝连接）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _stderr(proc) -> str:
    return proc.stderr.decode("utf-8", "replace")


def _stdout(proc) -> str:
    return proc.stdout.decode("utf-8", "replace")


def _out_lines(proc) -> dict:
    text = _stdout(proc).replace("\r\n", "\n")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _assert_no_residue(dest: Path, name: str) -> None:
    """失败后不留残件：dest 不存在或为空；技能目录与临时目录都不得残留。"""
    if dest.exists():
        entries = sorted(p.name for p in dest.iterdir())
        assert entries == [], f"失败后有残件：{entries}"
    assert not (dest / name).exists()


def _run_ok_case(
    fake_server, tmp_path, *, name=SKILL_NAME, dest=None, extra_args=(), env=None
):
    """端到端公共形状：以 --base 指向假服务端跑一次 CLI。"""
    dest = dest or (tmp_path / "out")
    env = env or _cli_env(tmp_path / "home")
    args = [
        "--name",
        name,
        "--dest",
        str(dest),
        "--base",
        _base_url(fake_server),
        *extra_args,
    ]
    return _run_cli(args, env, cwd=tmp_path), dest


# ---------------------------------------------------------------------------
# ① 单元：MANIFEST 解析
# ---------------------------------------------------------------------------


def test_parse_manifest_canonical_roundtrip():
    """canonical 清单解析：保序 + 摘要正确。"""
    raw = _canonical_manifest({"SKILL.md": b"a", "scripts/x.py": b"bb"})
    entries = skill_fetch.parse_manifest(raw)
    assert [rel for _, rel in entries] == ["SKILL.md", "scripts/x.py"]
    assert entries[0][0] == hashlib.sha256(b"a").hexdigest()
    assert entries[1][0] == hashlib.sha256(b"bb").hexdigest()


BAD_MANIFESTS = {
    "empty": (b"", "为空"),
    "no-trailing-newline": ((("0" * 64) + "  SKILL.md").encode(), "LF"),
    "cr-newline": ((("0" * 64) + "  SKILL.md\r\n").encode(), "CR"),
    "one-space-separator": ((("0" * 64) + " SKILL.md\n").encode(), "行格式非法"),
    "short-digest": (((("0" * 32)) + "  SKILL.md\n").encode(), "行格式非法"),
    "traversal-path": ((("0" * 64) + "  ../SKILL.md\n").encode(), "不安全"),
    "absolute-path": ((("0" * 64) + "  /etc/passwd\n").encode(), "不安全"),
    "backslash-path": ((("0" * 64) + "  scripts\\x.py\n").encode(), "不安全"),
    "dir-suffix": ((("0" * 64) + "  scripts/\n").encode(), "不应以 / 结尾"),
    "duplicate-path": (
        ((("0" * 64) + "  SKILL.md\n") + (("1" * 64) + "  SKILL.md\n")).encode(),
        "重复",
    ),
}


@pytest.mark.parametrize("case", sorted(BAD_MANIFESTS))
def test_parse_manifest_rejects_malformed(case):
    raw, expect = BAD_MANIFESTS[case]
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.parse_manifest(raw)
    assert expect in str(ei.value)


# ---------------------------------------------------------------------------
# ① 单元：归档成员安全校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["../x", "a/../b", "/abs/path", "C:/evil", "a\\b", "..", "a/..", "./../x"],
)
def test_member_path_problem_rejects(bad_name):
    assert skill_fetch.member_path_problem(bad_name) is not None


@pytest.mark.parametrize(
    "ok_name", ["SKILL.md", "pkg/SKILL.md", "corpus/金匮要略/notes.md", "scripts/"]
)
def test_member_path_problem_accepts(ok_name):
    assert skill_fetch.member_path_problem(ok_name) is None


def test_check_tar_member_types():
    """链接（软/硬）与特殊文件成员一律拒绝；普通文件 / 目录放行。"""
    ok_file = tarfile.TarInfo("pkg/SKILL.md")
    ok_file.size = 3
    assert skill_fetch.check_tar_member(ok_file) is None

    ok_dir = tarfile.TarInfo("pkg/scripts/")
    ok_dir.type = tarfile.DIRTYPE
    assert skill_fetch.check_tar_member(ok_dir) is None

    sym = tarfile.TarInfo("pkg/link")
    sym.type = tarfile.SYMTYPE
    sym.linkname = "/etc/passwd"
    assert "链接" in skill_fetch.check_tar_member(sym)

    hard = tarfile.TarInfo("pkg/hard")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "pkg/SKILL.md"
    assert "链接" in skill_fetch.check_tar_member(hard)

    fifo = tarfile.TarInfo("pkg/fifo")
    fifo.type = tarfile.FIFOTYPE
    assert skill_fetch.check_tar_member(fifo) is not None


def test_check_tar_member_path_component():
    """成员名经 './' 归一再判路径安全（'.' 前缀不构成豁免）。"""
    sneaky = tarfile.TarInfo("./../evil.txt")
    assert ".." in skill_fetch.check_tar_member(sneaky)


# ---------------------------------------------------------------------------
# ① 单元：key 解析优先级 / 形态
# ---------------------------------------------------------------------------


def test_resolve_key_default_env(monkeypatch):
    monkeypatch.setenv("SGME_AGENT_KEY", FAKE_AGENT_KEY)
    assert skill_fetch.resolve_key(None, None) == FAKE_AGENT_KEY


def test_resolve_key_env_name(monkeypatch):
    monkeypatch.delenv("SGME_AGENT_KEY", raising=False)
    monkeypatch.setenv(ALT_ENV_NAME, FAKE_AGENT_KEY)
    assert skill_fetch.resolve_key(ALT_ENV_NAME, None) == FAKE_AGENT_KEY


def test_resolve_key_file_beats_env(monkeypatch, tmp_path):
    """--key-file 优先于环境变量（显式来源覆盖默认来源）。"""
    monkeypatch.setenv("SGME_AGENT_KEY", FAKE_WRONG_KEY)
    key_file = tmp_path / "agent.key"
    key_file.write_text(FAKE_AGENT_KEY + "\n", encoding="utf-8")
    assert skill_fetch.resolve_key(None, str(key_file)) == FAKE_AGENT_KEY


def test_resolve_key_missing_env_message(monkeypatch):
    """缺失：提示变量名、绝不包含值。"""
    monkeypatch.delenv("SGME_AGENT_KEY", raising=False)
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.resolve_key(None, None)
    message = str(ei.value)
    assert "SGME_AGENT_KEY" in message
    assert FAKE_AGENT_KEY not in message


def test_resolve_key_rejects_whitespace_without_leak(monkeypatch):
    """含空白的 key 本地预检拒绝（防请求层异常回显值）。"""
    bad = "bad key with space"
    monkeypatch.setenv("SGME_AGENT_KEY", bad)
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.resolve_key(None, None)
    assert bad not in str(ei.value)


def test_key_file_missing(tmp_path):
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.resolve_key(None, str(tmp_path / "nope.key"))
    assert "不存在" in str(ei.value)


KEY_FILE_CASES = {
    "raw": (FAKE_AGENT_KEY + "\n", FAKE_AGENT_KEY),
    "raw-padded": ("  " + FAKE_AGENT_KEY + "  \n", FAKE_AGENT_KEY),
    "raw-quoted": ('"' + FAKE_AGENT_KEY + '"\n', FAKE_AGENT_KEY),
    "dotenv-named": (f"{ALT_ENV_NAME}={FAKE_AGENT_KEY}\n", FAKE_AGENT_KEY),
    "dotenv-comment-and-extra": (
        f"# 注释行\nOTHER={FAKE_WRONG_KEY}\nSGME_AGENT_KEY={FAKE_AGENT_KEY}\n",
        FAKE_AGENT_KEY,
    ),
    "dotenv-single-other-name": (f"OTHER={FAKE_AGENT_KEY}\n", FAKE_AGENT_KEY),
    "dotenv-quoted": (f"SGME_AGENT_KEY='{FAKE_AGENT_KEY}'\n", FAKE_AGENT_KEY),
}


@pytest.mark.parametrize("case", sorted(KEY_FILE_CASES))
def test_parse_key_file_text(case, tmp_path):
    content, expect = KEY_FILE_CASES[case]
    assert skill_fetch.parse_key_file_text(content, tmp_path / "k") == expect


def test_parse_key_file_text_empty(tmp_path):
    with pytest.raises(skill_fetch.FetchError):
        skill_fetch.parse_key_file_text("\n# 只有注释\n", tmp_path / "k")


def test_parse_key_file_text_ambiguous(tmp_path):
    """dotenv 多键且无 SGME_AGENT_KEY：拒绝（宁缺毋滥，不猜）。"""
    content = f"A={FAKE_AGENT_KEY}\nB={FAKE_WRONG_KEY}\n"
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.parse_key_file_text(content, tmp_path / "k")
    assert "A" in str(ei.value) and "B" in str(ei.value)  # 只列键名，不含值
    assert FAKE_AGENT_KEY not in str(ei.value)


# ---------------------------------------------------------------------------
# ① 单元：地址解析链
# ---------------------------------------------------------------------------


def test_resolve_base_cli_wins(monkeypatch):
    monkeypatch.setenv("SGME_BASE_URL", "http://env.example:1")
    assert skill_fetch.resolve_base("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"


def test_resolve_base_env(monkeypatch):
    monkeypatch.delenv("SGME_INSTALL_JSON", raising=False)
    monkeypatch.setenv("SGME_BASE_URL", "http://env.example:9910/")
    assert skill_fetch.resolve_base(None) == "http://env.example:9910"


def test_resolve_base_install_json(monkeypatch, tmp_path):
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    inst = tmp_path / "install.json"
    inst.write_text(
        json.dumps({"http": {"host": "10.1.2.3", "port": 9910}}), encoding="utf-8"
    )
    monkeypatch.setenv("SGME_INSTALL_JSON", str(inst))
    assert skill_fetch.resolve_base(None) == "http://10.1.2.3:9910"


def test_resolve_base_install_json_any_bind(monkeypatch, tmp_path):
    """install.json 记 0.0.0.0（服务端绑任意地址）→ 按本机回环连接。"""
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    inst = tmp_path / "install.json"
    inst.write_text(
        json.dumps({"http": {"host": "0.0.0.0", "port": 9910}}), encoding="utf-8"
    )
    monkeypatch.setenv("SGME_INSTALL_JSON", str(inst))
    assert skill_fetch.resolve_base(None) == "http://127.0.0.1:9910"


def test_resolve_base_install_json_without_http(monkeypatch, tmp_path):
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    inst = tmp_path / "install.json"
    inst.write_text(json.dumps({"keys": {"agent": "SGME_AGENT_KEY"}}), encoding="utf-8")
    monkeypatch.setenv("SGME_INSTALL_JSON", str(inst))
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.resolve_base(None)
    assert "--base" in str(ei.value)


def test_resolve_base_install_json_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("SGME_BASE_URL", raising=False)
    monkeypatch.setenv("SGME_INSTALL_JSON", str(tmp_path / "nope.json"))
    with pytest.raises(skill_fetch.FetchError) as ei:
        skill_fetch.resolve_base(None)
    assert "SGME_INSTALL_JSON" in str(ei.value)


def test_resolve_base_rejects_bad_scheme(monkeypatch):
    with pytest.raises(skill_fetch.FetchError):
        skill_fetch.resolve_base("127.0.0.1:9910")


# ---------------------------------------------------------------------------
# ① 单元：名称校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "  ", "a/b", "a\\b", "..", "."])
def test_validate_name_rejects(bad):
    with pytest.raises(skill_fetch.FetchError):
        skill_fetch.validate_name(bad)


def test_validate_name_strips():
    assert skill_fetch.validate_name(" adapter-demo ") == "adapter-demo"


# ---------------------------------------------------------------------------
# ② 端到端：成功路径
# ---------------------------------------------------------------------------


def test_e2e_success_full_chain(fake_server, tmp_path):
    """下载 → 清单校验 → 安全解包 → 落盘：文件逐字节一致、MANIFEST 自删、无残件。"""
    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 0, _stderr(proc)
    assert proc.stderr == b""

    lines = _out_lines(proc)
    expected_path = dest / SKILL_NAME
    assert os.path.normcase(lines["path"]) == os.path.normcase(str(expected_path))
    _, aggregate, _count = fake_server.package
    assert lines["sha256"] == aggregate

    for rel, data in DEFAULT_FILES.items():
        assert (expected_path / rel).read_bytes() == data
    # 校验后自删 MANIFEST；dest 下只含技能目录
    assert not (expected_path / MANIFEST_NAME).exists()
    assert sorted(p.name for p in dest.iterdir()) == [SKILL_NAME]

    # 单次请求、路径与 format 参数、携带 key
    assert fake_server.requests == [
        (f"/v1/skills/{SKILL_NAME}/package", {"format": ["tar.gz"]})
    ]
    assert fake_server.keys_seen == [FAKE_AGENT_KEY]
    # 成功路径同样不打印 key
    assert FAKE_AGENT_KEY not in _stdout(proc)
    assert FAKE_AGENT_KEY not in _stderr(proc)


def test_e2e_non_ascii_skill_name(fake_server, tmp_path):
    """非 ASCII 技能名：URL 编码请求 + 本地落盘目录名原样。"""
    name = "技能-中文"
    fake_server.skill_name = name
    fake_server.package = build_package(name, DEFAULT_FILES)
    proc, dest = _run_ok_case(fake_server, tmp_path, name=name)
    assert proc.returncode == 0, _stderr(proc)
    assert (dest / name / "SKILL.md").exists()
    quoted = urllib.parse.quote(name, safe="")
    assert fake_server.requests[0][0] == f"/v1/skills/{quoted}/package"


def test_e2e_base_env_fallback(fake_server, tmp_path):
    """无 --base：SGME_BASE_URL 环境变量回退。"""
    env = _cli_env(tmp_path / "home", env_base_url=_base_url(fake_server))
    proc = _run_cli(
        ["--name", SKILL_NAME, "--dest", str(tmp_path / "out")], env, cwd=tmp_path
    )
    assert proc.returncode == 0, _stderr(proc)


def test_e2e_base_install_json_fallback(fake_server, tmp_path):
    """无 --base / 无环境变量：~/.sgme/install.json 的 http 段回退（零明文）。"""
    home = tmp_path / "home"
    (home / ".sgme").mkdir(parents=True)
    (home / ".sgme" / "install.json").write_text(
        json.dumps(
            {
                "http": {"host": "127.0.0.1", "port": fake_server.server_address[1]},
                "keys": {"agent": "SGME_AGENT_KEY"},
            }
        ),
        encoding="utf-8",
    )
    env = _cli_env(home)  # 无 SGME_BASE_URL / SGME_INSTALL_JSON；HOME 指隔离目录
    proc = _run_cli(
        ["--name", SKILL_NAME, "--dest", str(tmp_path / "out")], env, cwd=tmp_path
    )
    assert proc.returncode == 0, _stderr(proc)


def test_e2e_key_env_name_and_key_file(fake_server, tmp_path):
    """--key-env 指定变量名；--key-file 从文件读 key（均不打印值）。"""
    env = _cli_env(tmp_path / "home", agent_key=None)
    env[ALT_ENV_NAME] = FAKE_AGENT_KEY
    proc, _ = _run_ok_case(
        fake_server, tmp_path, extra_args=["--key-env", ALT_ENV_NAME], env=env
    )
    assert proc.returncode == 0, _stderr(proc)
    assert fake_server.keys_seen == [FAKE_AGENT_KEY]

    key_file = tmp_path / "agent.key"
    key_file.write_text(FAKE_AGENT_KEY + "\n", encoding="utf-8")
    env2 = _cli_env(tmp_path / "home", agent_key=None)
    proc2, _ = _run_ok_case(
        fake_server,
        tmp_path,
        dest=tmp_path / "out2",
        extra_args=["--key-file", str(key_file)],
        env=env2,
    )
    assert proc2.returncode == 0, _stderr(proc2)
    assert FAKE_AGENT_KEY not in _stdout(proc2)
    assert FAKE_AGENT_KEY not in _stderr(proc2)


def test_e2e_proxy_bypass_with_dead_proxy(fake_server, tmp_path):
    """死代理在场（HTTP(S)_PROXY / ALL_PROXY）仍直连成功——显式绕过环境代理。"""
    env = _cli_env(tmp_path / "home")
    assert env["HTTP_PROXY"] == DEAD_PROXY
    assert env["ALL_PROXY"] == DEAD_PROXY
    proc, _ = _run_ok_case(fake_server, tmp_path, env=env)
    assert proc.returncode == 0, _stderr(proc)


# ---------------------------------------------------------------------------
# ② 端到端：失败 → 中止且不留残件
# ---------------------------------------------------------------------------


def test_e2e_hash_mismatch_aborts(fake_server, tmp_path):
    """清单校验失败（哈希不符）→ 退出码 1、不留残件、stdout 为空。"""
    files = dict(DEFAULT_FILES)
    tampered = bytearray(_canonical_manifest(files))
    tampered[0] = ord("1") if tampered[0] != ord("1") else ord("2")
    fake_server.package = build_package(SKILL_NAME, files, manifest=bytes(tampered))

    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    assert "哈希不匹配" in _stderr(proc)
    assert proc.stdout == b""
    _assert_no_residue(dest, SKILL_NAME)


def test_e2e_extra_file_not_in_manifest_aborts(fake_server, tmp_path):
    """归档多出清单外文件 → 文件集对照失败、中止。"""
    files = dict(DEFAULT_FILES)
    manifest = _canonical_manifest(files)  # 不含 extra
    files["scripts/extra.py"] = b"x = 1\n"
    fake_server.package = build_package(SKILL_NAME, files, manifest=manifest)
    fake_server.files_override = 3  # 头与清单行数一致，隔离出「文件集对照」路径

    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    assert "文件集与清单不一致" in _stderr(proc)
    _assert_no_residue(dest, SKILL_NAME)


def test_e2e_missing_file_aborts(fake_server, tmp_path):
    """清单列出但归档缺失文件 → 文件集对照失败、中止。"""
    files = {"SKILL.md": DEFAULT_FILES["SKILL.md"]}
    manifest = _canonical_manifest({**files, "ghost.md": b"ghost"})
    fake_server.package = build_package(SKILL_NAME, files, manifest=manifest)
    fake_server.files_override = 2  # 头与清单行数一致，隔离出「文件集对照」路径

    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    err = _stderr(proc)
    assert "文件集与清单不一致" in err and "ghost.md" in err
    _assert_no_residue(dest, SKILL_NAME)


def test_e2e_header_aggregate_mismatch_aborts(fake_server, tmp_path):
    """响应头 X-Skill-Sha256 与清单聚合不一致 → 中止。"""
    fake_server.aggregate_override = "f" * 64
    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    assert "X-Skill-Sha256" in _stderr(proc)
    _assert_no_residue(dest, SKILL_NAME)


def test_e2e_header_files_mismatch_aborts(fake_server, tmp_path):
    """响应头 X-Skill-Files 与清单行数不一致 → 中止。"""
    fake_server.files_override = 99
    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    assert "X-Skill-Files" in _stderr(proc)
    _assert_no_residue(dest, SKILL_NAME)


MALICIOUS_MEMBER_CASES = {
    "traversal": (_file_member("../evil.txt", b"evil"), "../evil.txt"),
    "absolute": (_file_member("/tmp/sgme_evil.txt", b"evil"), "/tmp/sgme_evil.txt"),
    "symlink-escape": (
        _symlink_member(f"{SKILL_NAME}/evil-link", "/etc/passwd"),
        f"{SKILL_NAME}/evil-link",
    ),
    "symlink-parent": (
        _symlink_member(f"{SKILL_NAME}/evil-up", "../../outside"),
        f"{SKILL_NAME}/evil-up",
    ),
}


@pytest.mark.parametrize("case", sorted(MALICIOUS_MEMBER_CASES))
def test_e2e_malicious_tar_member_rejected(fake_server, tmp_path, case):
    """恶意 tar 成员样本（../ / 绝对路径 / 出目录链接）→ 中止且不写盘。"""
    member, member_path = MALICIOUS_MEMBER_CASES[case]
    fake_server.package = build_package(
        SKILL_NAME, DEFAULT_FILES, extra_members=[member]
    )
    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    assert "不安全" in _stderr(proc)
    _assert_no_residue(dest, SKILL_NAME)
    # 越权文件绝不落盘（按样本路径与 tmp_path 全树递归双重核对）
    assert not (dest / member_path).exists()
    assert list(tmp_path.rglob("*evil*")) == []


def test_e2e_not_a_targz_aborts(fake_server, tmp_path):
    fake_server.package = (b"not a tarball", "0" * 64, 0)
    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    assert "归档不可读" in _stderr(proc)
    _assert_no_residue(dest, SKILL_NAME)


# ---------------------------------------------------------------------------
# ② 端到端：接口错误分支（版本不足 / 认证 / 连接）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 405])
def test_e2e_version_insufficient_message(fake_server, tmp_path, status):
    """404 / 405 → 「服务端版本不足」文案；不留残件、不泄露 key。"""
    fake_server.status = status
    proc, dest = _run_ok_case(fake_server, tmp_path)
    assert proc.returncode == 1
    err = _stderr(proc)
    assert "服务端版本不足" in err
    assert str(status) in err
    assert FAKE_AGENT_KEY not in err
    _assert_no_residue(dest, SKILL_NAME)


def test_e2e_auth_failure_no_key_leak(fake_server, tmp_path):
    """坏 key → 401 认证提示；两个 key 值都不出现在输出里。"""
    env = _cli_env(tmp_path / "home", agent_key=FAKE_WRONG_KEY)
    proc, dest = _run_ok_case(fake_server, tmp_path, env=env)
    assert proc.returncode == 1
    err = _stderr(proc)
    assert "认证失败" in err and "401" in err
    assert FAKE_WRONG_KEY not in err and FAKE_WRONG_KEY not in _stdout(proc)
    assert FAKE_AGENT_KEY not in err
    _assert_no_residue(dest, SKILL_NAME)


def test_e2e_connection_failure_hint(fake_server, tmp_path):
    """服务不可达：中文报错 + 排障提示（地址 / 代理）、退出码 1。"""
    env = _cli_env(tmp_path / "home")
    proc = _run_cli(
        [
            "--name",
            SKILL_NAME,
            "--dest",
            str(tmp_path / "out"),
            "--base",
            f"http://127.0.0.1:{_closed_port()}",
        ],
        env,
        cwd=tmp_path,
    )
    assert proc.returncode == 1
    err = _stderr(proc)
    assert "无法连接" in err and "排障提示" in err


def test_e2e_missing_key_no_request(fake_server, tmp_path):
    """key 缺失：本地即报错（无请求发出）、提示变量名。"""
    env = _cli_env(tmp_path / "home", agent_key=None)
    proc, dest = _run_ok_case(fake_server, tmp_path, env=env)
    assert proc.returncode == 1
    assert "SGME_AGENT_KEY" in _stderr(proc)
    assert fake_server.requests == []
    assert not (dest / SKILL_NAME).exists()


def test_e2e_dest_target_exists_refused(fake_server, tmp_path):
    """目标已存在：拒绝覆盖（无请求、旧内容原样）。"""
    dest = tmp_path / "out"
    (dest / SKILL_NAME).mkdir(parents=True)
    (dest / SKILL_NAME / "keep.txt").write_text("old", encoding="utf-8")
    proc, _ = _run_ok_case(fake_server, tmp_path, dest=dest)
    assert proc.returncode == 1
    assert "目标已存在" in _stderr(proc)
    assert fake_server.requests == []
    assert (dest / SKILL_NAME / "keep.txt").read_text(encoding="utf-8") == "old"


def test_e2e_usage_error_exit2(tmp_path):
    """--key-env 与 --key-file 互斥（argparse 退出码 2）。"""
    env = _cli_env(tmp_path / "home")
    proc = _run_cli(
        ["--name", "x", "--dest", "y", "--key-env", "A", "--key-file", "B"],
        env,
        cwd=tmp_path,
    )
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# ② 单元：低版本解释器分支（手工成员校验，不依赖 data 过滤器）
# ---------------------------------------------------------------------------


def test_extract_rejects_malicious_on_older_python(monkeypatch, tmp_path):
    """py<3.12 分支：恶意成员同样被拒，且未写出任何文件（含越权路径）。"""
    body, _, _ = build_package(
        SKILL_NAME, DEFAULT_FILES, extra_members=[_file_member("../evil.txt", b"x")]
    )
    src = tmp_path / "pkg.tar.gz"
    src.write_bytes(body)
    monkeypatch.setattr(skill_fetch.sys, "version_info", (3, 11, 0))
    with tarfile.open(src, "r:gz") as tf, pytest.raises(skill_fetch.FetchError):
        skill_fetch.extract_archive(tf, tmp_path / "stage")
    assert list(tmp_path.rglob("*evil*")) == []
    assert not (tmp_path / "stage" / "evil.txt").exists()


def test_extract_valid_package_on_older_python(monkeypatch, tmp_path):
    """py<3.12 分支：合法包正常解包（手工校验不误伤）。"""
    body, _, _ = build_package(SKILL_NAME, DEFAULT_FILES)
    src = tmp_path / "pkg.tar.gz"
    src.write_bytes(body)
    monkeypatch.setattr(skill_fetch.sys, "version_info", (3, 11, 0))
    stage = tmp_path / "stage"
    with tarfile.open(src, "r:gz") as tf:
        skill_fetch.extract_archive(tf, stage)
    assert (stage / SKILL_NAME / "SKILL.md").read_bytes() == DEFAULT_FILES["SKILL.md"]
    assert (stage / MANIFEST_NAME).is_file()


# ---------------------------------------------------------------------------
# ③ 制度：真源 / 分发副本字节一致；--help 可用
# ---------------------------------------------------------------------------


def test_two_copies_byte_identical():
    """scripts/skill_fetch.py（真源）与 AI-INSTALL/skill_fetch.py（分发副本）sha256 相等。"""
    assert SCRIPT_PATH.is_file(), f"真源缺失：{SCRIPT_PATH}"
    assert DIST_COPY_PATH.is_file(), f"分发副本缺失：{DIST_COPY_PATH}"
    src = hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest()
    dist = hashlib.sha256(DIST_COPY_PATH.read_bytes()).hexdigest()
    assert src == dist, f"两侧副本漂移：{src} != {dist}"


def test_cli_help_available(tmp_path):
    """--help 可用：退出码 0，列出全部 CLI 参数。"""
    proc = _run_cli(["--help"], _cli_env(tmp_path / "home"), cwd=tmp_path)
    assert proc.returncode == 0
    out = _stdout(proc)
    for flag in ("--name", "--dest", "--base", "--key-env", "--key-file"):
        assert flag in out
