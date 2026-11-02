# -*- coding: utf-8 -*-
"""技能落盘缓存层（设计 5.2.1 / D-59，任务 T-406）。

定位
----
八适配器的「技能获取层」统一走库接口 + 本地落盘缓存，取代「从 git / 随适配器
分发」的旧路径。**纯标准库单文件**：各适配器以源码副本随自己的分发包携带
（本目录是唯一真源，``scripts/sync_shared_skill_cache.py`` 同步；副本一致性由
``tests/test_skill_cache_sync.py`` 守住），因为适配器安装后是独立目录、不能
反向依赖仓库里的 ``adapters/_shared/``。

两级失效（服务端契约见 ``sgme/operations/skill_cache.py``）
----------------------------------------------------------
1. 目录级短路：``GET /v1/skills/catalog-version`` 与本地
   ``<state_dir>/.sgme-catalog.json`` 比对；相同说明整库内容没变，
   本次会话不再逐个比对（进程内只拉一次）。
2. 单技能比对：``GET /v1/skills/{name}/manifest`` 的 ``package_sha256`` 与本地
   ``<dest>/<name>/.sgme-manifest.json`` 的 ``content_hash`` 比对；相同直接用本地
   副本，**零包传输**。
3. 未命中才 ``GET /v1/skills/{name}/package``（tar.gz）：包内 ``MANIFEST.sha256``
   逐文件校验 + 文件集双向对照 + 响应头聚合哈希对账，全部通过后在
   ``<dest>/.<name>.fetch-*`` 临时目录里组装，最后 ``os.replace`` **原子替换**
   （失败回滚、不留残件）。

本地工作副本清单（``.sgme-manifest.json``）
-------------------------------------------
``{name, version, content_hash, files: [{path, sha256}], fetched_at, catalog_version}``
- ``content_hash`` = 包内 canonical ``MANIFEST.sha256`` 文本的 sha256（= 服务端
  ``package_sha256`` = 响应头 ``X-Skill-Sha256``），既用于二级比对，也用于落盘后
  的完整性自证；
- ``files`` 记录每个文件的 sha256：本地文件被外部改动/删掉时，下次 ``ensure_skill``
  会检出并重新拉取（失效触发项之一）。

降级纪律
--------
缓存是**优化**不是正确性依赖：catalog/manifest 端点不可用（旧服务端 404/405、
网络抖动）一律降级为「直接拉包」，行为与 1.x 的 fetch 通道一致；只有包本体
下载/校验失败才抛错。适配器据此可以在旧服务端上继续工作。

不做什么
--------
不做全局技能缓存目录、不做预热（设计 5.2.1 明确「不建」）——落点永远由调用方
给的 ``dest_dir`` 决定。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------- 协议常量（与 dsh TS 移植版、服务端 skill_cache.py 对齐） ----------

#: 缓存协议版本：Python 真源与 TS 移植版必须同值（tests/test_skill_cache_sync.py 校验）
CACHE_PROTOCOL_VERSION = 1

#: 本地工作副本清单文件名（放在技能目录内，随技能目录一起原子替换）
MANIFEST_FILE = ".sgme-manifest.json"

#: 目录级缓存状态文件名（放在 state_dir，不放进技能目录）
CATALOG_FILE = ".sgme-catalog.json"

#: 包内清单文件名（服务端归档根，与 <name>/ 平级）
PACKAGE_MANIFEST = "MANIFEST.sha256"

DEFAULT_TIMEOUT_S = 300.0
_CHUNK = 262144  # 256 KiB：逐文件哈希分块

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

__all__ = [
    "CACHE_PROTOCOL_VERSION",
    "MANIFEST_FILE",
    "CATALOG_FILE",
    "PACKAGE_MANIFEST",
    "SkillCacheError",
    "HTTPStatusError",
    "SkillCache",
    "UrllibTransport",
    "ensure_skill",
    "refresh_catalog",
    "sha256_bytes",
    "sha256_file",
    "validate_name",
]


# ---------- 错误 ----------


class SkillCacheError(Exception):
    """技能缓存运行时错误（消息面向用户，绝不包含密钥值）。"""


class HTTPStatusError(SkillCacheError):
    """HTTP 非 2xx：带状态码，调用方据此决定是否降级（404/405 = 端点未部署）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)


# ---------- 通用工具 ----------


def sha256_bytes(data: bytes) -> str:
    """字节串 sha256（十六进制小写）。"""
    return hashlib.sha256(data).hexdigest()


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


def _now_iso() -> str:
    """当前 UTC 时间（ISO 8601，秒级；清单里的 fetched_at）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_name(name: Any) -> str:
    """技能名校验：必须是单个路径组件（防经技能名逃逸落点目录）。"""
    n = str(name or "").strip()
    if not n:
        raise SkillCacheError("技能名不能为空")
    if any(ch in n for ch in "/\\\r\n\0") or n in (".", ".."):
        raise SkillCacheError(f"技能名非法（应为单个技能名、不含路径分隔符）：{n!r}")
    return n


def _normalize_rel(rel: str) -> str:
    """包内相对路径归一化 + 安全校验（拒绝绝对路径 / 盘符 / ``..`` / 空段）。"""
    p = str(rel or "").replace("\\", "/")
    if not p or p.startswith("/") or _DRIVE_RE.match(p):
        raise SkillCacheError(f"包内路径非法（拒绝）: {rel!r}")
    if any(seg in ("", ".", "..") for seg in p.split("/")):
        raise SkillCacheError(f"包内路径非法（拒绝）: {rel!r}")
    return p


# ---------- 传输层 ----------


class UrllibTransport:
    """标准库 HTTP 传输（默认实现；适配器可换成自己的 HTTP 客户端适配器）。

    与项目铁律一致：``ProxyHandler({})`` 显式绕过环境代理（PC 上 Clash 会劫持
    内网请求），超时由调用方控制。``get_json`` / ``get_bytes`` 失败一律抛
    ``SkillCacheError``（4xx/5xx 抛 ``HTTPStatusError`` 并带状态码）。
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 timeout: float = 30.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        if not self.base_url:
            raise SkillCacheError("SGME 地址为空（需 base_url）")
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        # 显式空 ProxyHandler：局域网直连，不吃 HTTP_PROXY / HTTPS_PROXY
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, path: str, timeout: Optional[float] = None) -> Tuple[bytes, Dict[str, str]]:
        url = self.base_url + path
        req = urllib.request.Request(url, method="GET")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.read(), {str(k).lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise HTTPStatusError(e.code, f"HTTP {e.code} {e.reason}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise SkillCacheError(f"连接失败 {url}: {e.reason}") from e
        except OSError as e:  # 预检失败 / 超时等
            raise SkillCacheError(f"请求失败 {url}: {e}") from e

    def get_json(self, path: str) -> Any:
        raw, _headers = self._request(path)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SkillCacheError(f"响应不是合法 JSON（{path}）: {e}") from e

    def get_bytes(self, path: str) -> Tuple[bytes, Dict[str, str]]:
        return self._request(path, timeout=max(self.timeout, DEFAULT_TIMEOUT_S))

    def close(self) -> None:  # 与 httpx 客户端接口对齐（无资源可释放）
        return None


# ---------- 包校验 / 解包 ----------


def _parse_package_manifest(raw: bytes) -> List[Tuple[str, str]]:
    """解析包内 ``MANIFEST.sha256``：行格式 ``<sha256><两个空格><相对路径>``。"""
    entries: List[Tuple[str, str]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or not rel:
            raise SkillCacheError(f"包内 MANIFEST 行格式非法: {line[:80]!r}")
        entries.append((digest.strip().lower(), _normalize_rel(rel)))
    if not entries:
        raise SkillCacheError("包内 MANIFEST 为空——拒绝落盘")
    return entries


def _check_member(member: tarfile.TarInfo) -> str:
    """归档成员安全校验：拒绝对路径 / ``..`` / 链接 / 特殊文件。"""
    if member.issym() or member.islnk():
        raise SkillCacheError(f"包内出现链接成员（不符合归档契约，拒绝）: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise SkillCacheError(f"包内出现特殊成员（不符合归档契约，拒绝）: {member.name}")
    name = member.name.rstrip("/")
    if not name:
        raise SkillCacheError("包内出现空路径成员（拒绝）")
    return _normalize_rel(name)


def _extract_package(name: str, data: bytes, stage: Path) -> Tuple[List[Tuple[str, str]], str, int]:
    """安全解包到 ``stage``：返回 (MANIFEST 条目, 聚合 sha256, 文件数)。

    归档契约（服务端 ``_stream_package_archive``）：根层 ``MANIFEST.sha256`` +
    ``<name>/`` 下逐成员，不含链接。任何不合契约的成员整包拒收。
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise SkillCacheError(f"包解压失败（tar.gz 无效或截断）: {e}") from e
    with tar:
        members = tar.getmembers()
        top: set = set()
        for m in members:
            rel = _check_member(m)
            top.add(rel.split("/", 1)[0])
        # 顶层布局：只允许 <name>/ 与 MANIFEST.sha256
        if top != {name, PACKAGE_MANIFEST}:
            raise SkillCacheError(
                f"包顶层布局异常 {sorted(top)}（期望 [{name!r}, {PACKAGE_MANIFEST!r}]）——拒绝落盘")
        manifest_member = next(m for m in members if m.name.rstrip("/") == PACKAGE_MANIFEST)
        manifest_bytes = tar.extractfile(manifest_member).read()  # type: ignore[union-attr]
        entries = _parse_package_manifest(manifest_bytes)

        stage.mkdir(parents=True, exist_ok=True)
        for m in members:
            rel = _normalize_rel(m.name.rstrip("/"))
            if rel == PACKAGE_MANIFEST or m.isdir():
                continue
            if not rel.startswith(name + "/"):
                raise SkillCacheError(f"包内成员不在技能根下（拒绝）: {rel}")
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:  # pragma: no cover - 已限定 isfile
                raise SkillCacheError(f"包内成员不可读: {rel}")
            target.write_bytes(src.read())

    # 文件集双向对照：MANIFEST 列的必须都在、多一个也算失败
    skill_dir = stage / name
    expected = {rel for _digest, rel in entries}
    actual = ({p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*") if p.is_file()}
              if skill_dir.is_dir() else set())
    if expected != actual:
        raise SkillCacheError(
            f"包内文件集与 MANIFEST 不一致（缺 {sorted(expected - actual)[:3]}"
            f" / 多 {sorted(actual - expected)[:3]}）——拒绝落盘")
    for digest, rel in entries:
        if sha256_file(skill_dir / rel) != digest:
            raise SkillCacheError(f"包内文件 sha256 校验失败: {rel}——拒绝落盘")
    return entries, sha256_bytes(manifest_bytes), len(entries)


def _check_response_headers(headers: Dict[str, str], aggregate: str, files: int) -> None:
    """响应头对账：``X-Skill-Sha256`` / ``X-Skill-Files`` 在场时必须一致。"""
    header_hash = str(headers.get("x-skill-sha256") or "").strip().lower()
    if header_hash and header_hash != aggregate:
        raise SkillCacheError("包聚合哈希与响应头 X-Skill-Sha256 不一致——拒绝落盘")
    header_files = str(headers.get("x-skill-files") or "").strip()
    if header_files and header_files != str(files):
        raise SkillCacheError("包文件数与响应头 X-Skill-Files 不一致——拒绝落盘")


# ---------- 缓存主体 ----------


class SkillCache:
    """两级失效的技能落盘缓存。

    参数
    ----
    transport : ``UrllibTransport`` 或适配器自带的同形对象
        （只需实现 ``get_json(path)`` / ``get_bytes(path)``）。
    state_dir : 目录级缓存状态（``.sgme-catalog.json``）落点；缺省时
        ``ensure_skill`` 用 ``dest_dir`` 兜底。

    返回值
    ------
    ``ensure_skill(name, dest_dir) -> Path``：本地技能目录。命中/未命中都返回
    同一个路径；是否发生下载看实例属性 ``last_hit``（True = 零包传输）。
    """

    def __init__(self, transport: Any, state_dir: Optional[str | Path] = None) -> None:
        self.transport = transport
        self.state_dir = Path(state_dir).expanduser() if state_dir else None
        # 最近一次 ensure_skill 的结果（供适配器组装回执，不改函数签名）
        self.last_hit: bool = False
        self.last_manifest: Optional[Dict[str, Any]] = None
        # 目录级状态：进程内只拉一次（设计「会话开始」语义）；值为 (changed, version)
        self._catalog_checked: Dict[str, Tuple[bool, str]] = {}

    # ---------- 目录级（一级） ----------

    def _catalog_path(self, state_dir: Optional[Path]) -> Optional[Path]:
        d = state_dir or self.state_dir
        return Path(d).expanduser() / CATALOG_FILE if d else None

    def refresh_catalog(self, *, force: bool = False) -> bool:
        """拉目录版本并与本地记录比对；返回 True = 有变化（需要做二级比对）。

        相同则跳过（返回 False）；``.sgme-catalog.json`` 只在版本变化时重写。
        端点不可用抛 ``SkillCacheError``（调用方可降级）。
        """
        path = self._catalog_path(None)
        if path is None:
            raise SkillCacheError("未指定 state_dir，无法做目录级缓存比对")
        local = self._read_catalog(path)
        data = self.transport.get_json("/v1/skills/catalog-version")
        if not isinstance(data, dict):
            raise SkillCacheError("目录版本响应异常（期望对象）")
        version = str(data.get("catalog_version") or "").strip()
        if not version:
            raise SkillCacheError("目录版本响应缺少 catalog_version")
        if not force and local and local.get("catalog_version") == version:
            return False
        self._write_catalog(path, {"catalog_version": version, "checked_at": _now_iso()})
        return True

    def _catalog_state(self, state_dir: Path) -> Tuple[bool, str]:
        """目录级短路判定（同进程同 stateDir 只真的拉一次）。

        返回 ``(changed, version)``：changed=True 表示版本变了/未知（继续二级比对），
        version 是本次见到的服务端目录版本（拉不到时为空串）。

        端点不可用按「未知」处理（changed=True），实现降级而不误判命中。
        """
        key = str(Path(state_dir).expanduser().resolve())
        cached = self._catalog_checked.get(key)
        if cached is not None:
            return cached
        path = self._catalog_path(Path(state_dir))
        local = self._read_catalog(path) if path else None
        wrote = False
        change_seen = True
        version = ""
        try:
            data = self.transport.get_json("/v1/skills/catalog-version")
            version = str((data or {}).get("catalog_version") or "").strip() if isinstance(data, dict) else ""
            if not version:
                change_seen = True
            elif local and local.get("catalog_version") == version:
                change_seen = False
            else:
                change_seen = True
                if path is not None:
                    self._write_catalog(path, {"catalog_version": version, "checked_at": _now_iso()})
                    wrote = True
        except SkillCacheError:
            change_seen = True
        # 本次调用必须按 change_seen 走二级比对（刚写的状态还没被任何副本消费）；
        # 写盘后本进程**后续**调用已同步，可短路。
        self._catalog_checked[key] = (False, version) if wrote else (change_seen, version)
        return change_seen, version

    @staticmethod
    def _read_catalog(path: Optional[Path]) -> Optional[Dict[str, Any]]:
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_catalog(path: Path, payload: Dict[str, Any]) -> None:
        """目录状态原子写（临时文件 → os.replace）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))

    # ---------- 单技能（二级） ----------

    def read_manifest(self, name: str, dest_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        """读本地 ``.sgme-manifest.json``；缺失/损坏/名字不符返回 None。"""
        try:
            n = validate_name(name)
        except SkillCacheError:
            return None
        path = Path(dest_dir).expanduser() / n / MANIFEST_FILE if dest_dir else None
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("name") != n:
            return None
        if not isinstance(data.get("files"), list):
            return None
        return data

    def _remote_manifest(self, name: str) -> Optional[Dict[str, Any]]:
        """服务端单技能清单；端点不可用/响异常返回 None（降级为直接拉包）。"""
        try:
            data = self.transport.get_json(f"/v1/skills/{urllib.parse.quote(name)}/manifest")
        except SkillCacheError:
            return None
        if not isinstance(data, dict) or not data.get("package_sha256"):
            return None
        return data

    def ensure_skill(self, name: str, dest_dir: str | Path) -> Path:
        """保证 ``<dest_dir>/<name>`` 是服务端最新副本；返回该目录路径。

        命中（目录级短路或单技能 ``package_sha256`` 相同且本地文件完好）时零包
        传输；未命中才下载 + 校验 + 原子替换。``last_hit`` 记录本次是否命中。
        """
        name = validate_name(name)
        dest = Path(dest_dir).expanduser()
        target = dest / name
        self.last_hit = False
        self.last_manifest = None

        local = self.read_manifest(name, dest)
        intact = bool(local) and self._files_intact(target, local)
        # 目录级（一级）：无论本地有没有副本都先建立/刷新目录状态——
        # 首次落盘后同一进程的后续 ensure 才能真正短路（否则永远走二级比对）。
        catalog_changed, catalog_version = self._catalog_state(dest)
        # 只在本副本就是该目录版本下取的（manifest 记了 catalog_version）时才短路：
        # 防「上次下载失败但目录版本已记下」导致旧副本被误判为最新。
        same_catalog = bool(catalog_version) and str((local or {}).get("catalog_version") or "") == catalog_version
        if intact and not catalog_changed and same_catalog:
            self.last_hit = True
            self.last_manifest = local
            return target
        # 二级：单技能 package_sha256 比对
        remote = self._remote_manifest(name)
        version = str((remote or {}).get("version") or "")
        if intact and remote is not None and remote.get("package_sha256") == (local or {}).get("content_hash"):
            self.last_hit = True
            self.last_manifest = local
            return target

        return self._download_and_install(name, dest, version, remote, catalog_version)

    @staticmethod
    def _files_intact(target: Path, manifest: Dict[str, Any]) -> bool:
        """本地文件自检：清单内的文件都在且 sha256 未变（外部改动即失效）。"""
        if not target.is_dir():
            return False
        for item in manifest.get("files") or []:
            if not isinstance(item, dict):
                return False
            rel = item.get("path")
            digest = item.get("sha256")
            if not isinstance(rel, str) or not isinstance(digest, str):
                return False
            try:
                normalized = _normalize_rel(rel)
            except SkillCacheError:
                return False
            f = target / normalized
            if not f.is_file() or sha256_file(f) != digest.lower():
                return False
        return bool(manifest.get("files"))

    def _download_and_install(
        self, name: str, dest: Path, version: str, remote: Optional[Dict[str, Any]],
        catalog_version: str = "",
    ) -> Path:
        """拉包 → 校验 → 临时目录组装 → 原子替换（失败回滚、不留残件）。"""
        data, headers = self.transport.get_bytes(
            f"/v1/skills/{urllib.parse.quote(name)}/package")
        try:
            dest.mkdir(parents=True, exist_ok=True)
            tmp_root = Path(tempfile.mkdtemp(prefix=f".{name}.fetch-", dir=str(dest)))
        except OSError as e:
            raise SkillCacheError(f"目标目录不可用（{dest}）: {e}") from e
        try:
            staged = tmp_root / "staged"
            entries, aggregate, files = _extract_package(name, data, staged)
            _check_response_headers(headers, aggregate, files)
            expected = str((remote or {}).get("package_sha256") or "").strip().lower()
            if expected and expected != aggregate:
                raise SkillCacheError(
                    f"包聚合哈希与服务端 manifest.package_sha256 不一致——拒绝落盘: {aggregate}")
            skill_stage = staged / name
            if not skill_stage.is_dir():
                raise SkillCacheError(f"包内缺少技能根目录 <{name}>/——拒绝落盘")

            # 清单写进**新目录**再整体替换：旧副本永远不会读到半更新状态
            manifest = {
                "name": name,
                "version": version or "",
                "content_hash": aggregate,
                "files": [{"path": rel, "sha256": digest} for digest, rel in entries],
                "fetched_at": _now_iso(),
                "catalog_version": catalog_version or "",
            }
            (skill_stage / MANIFEST_FILE).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            target = dest / name
            backup: Optional[Path] = None
            if target.exists():
                backup = tmp_root / ".previous"
                os.replace(str(target), str(backup))
            try:
                os.replace(str(skill_stage), str(target))
            except OSError as e:
                if backup is not None:
                    os.replace(str(backup), str(target))
                raise SkillCacheError(f"落盘失败（已回滚原目录）: {e}") from e
            self.last_manifest = manifest
            return target
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)


# ---------- 模块级便捷函数（脚本/自检直接用） ----------


def _default_transport(base_url: Optional[str] = None,
                       api_key: Optional[str] = None) -> UrllibTransport:
    """按环境变量构造默认传输：SGME_BASE_URL / SGME_AGENT_KEY。"""
    url = base_url or os.environ.get("SGME_BASE_URL") or "http://127.0.0.1:9910"
    return UrllibTransport(url, api_key=api_key or os.environ.get("SGME_AGENT_KEY") or "")


def ensure_skill(
    name: str,
    dest_dir: str | Path,
    *,
    transport: Any = None,
    state_dir: Optional[str | Path] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Path:
    """模块级 ``ensure_skill``：默认传输走环境变量（自检脚本用）。"""
    cache = SkillCache(
        transport or _default_transport(base_url, api_key),
        state_dir=Path(state_dir).expanduser() if state_dir else None,
    )
    return cache.ensure_skill(name, dest_dir)


def refresh_catalog(
    *,
    transport: Any = None,
    state_dir: Optional[str | Path] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """模块级 ``refresh_catalog``：返回 True = 目录版本有变化。"""
    cache = SkillCache(
        transport or _default_transport(base_url, api_key),
        state_dir=Path(state_dir).expanduser() if state_dir else None,
    )
    return cache.refresh_catalog()
