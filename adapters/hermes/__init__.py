# -*- coding: utf-8 -*-
"""SGME MemoryProvider — 桥接 Hermes 与 SGME 记忆引擎 Gateway.

瘦桥接层：不碰 LLM、不碰数据库，纯 HTTP 调 SGME Gateway（默认 http://127.0.0.1:9910；生产部署用 SGME_BASE_URL 指向 NAS）。
重活（L1/L1.5/L2 提炼、向量、TTL）全在 SGME Gateway 侧。

接入方式（Hermes 原生 memory provider 槽位）：
  config.yaml → memory.provider: sgme

生命周期（MemoryProvider ABC）：
  - system_prompt_block(): SGME 画像摘要（Tier0，/v1/inject mode=full 精简）
  - prefetch(query): 每轮 LLM 前召回相关记忆（/v1/search，<100ms 预算）
  - sync_turn(): 每轮对话后写原始层（/v1/append，后台线程异步）
  - on_session_end(): 会话结束触发提炼（/v1/admin/refine/trigger）
  - get_tool_schemas()/handle_tool_call(): 40 个 sgme_* 工具（对齐 MCP 41 工具基准；
    agent_onboarding / append 两项永久豁免，理由见 adapters/hermes/README.md）

故障隔离：SGME Gateway 不可达 → 静默降级（is_available=false / try-except），
绝不阻塞 Hermes 主流程。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    _HAS_HTTPX = False

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # pragma: no cover
    # 独立测试环境兜底：无 Hermes 时用最小 ABC
    from abc import ABC, abstractmethod

    class MemoryProvider(ABC):  # type: ignore[no-redef]
        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs) -> None: ...

        @abstractmethod
        def get_tool_schemas(self) -> List[Dict[str, Any]]: ...

        def system_prompt_block(self) -> str:
            return ""

        def prefetch(self, query: str, *, session_id: str = "") -> str:
            return ""

        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
            pass

        def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
            pass

        def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
            raise NotImplementedError

        def shutdown(self) -> None:
            pass

logger = logging.getLogger("sgme.provider")

# 默认 SGME 端点与 Key 环境变量（可经 plugin.yaml config 段覆盖）
_DEFAULT_BASE_URL = os.environ.get("SGME_BASE_URL", "http://127.0.0.1:9910")
_DEFAULT_AGENT_KEY = os.environ.get("SGME_AGENT_KEY", "dev-agent-key-change-me")
_DEFAULT_ADMIN_KEY = os.environ.get("SGME_ADMIN_KEY", "dev-admin-key-change-me")
_DEFAULT_MODE = os.environ.get("SGME_INJECT_MODE", "daily")
_DEFAULT_MAX_TOKENS = int(os.environ.get("SGME_INJECT_MAX_TOKENS", "800"))
_DEFAULT_TIMEOUT = float(os.environ.get("SGME_HTTP_TIMEOUT", "5.0"))
# 溯源 agent_id（B35 自报，2026-08-11）：append body 带唯一标识，
# 与共享鉴权 key 解耦——Hermes 写入的记忆可正确溯源到 hermes
_DEFAULT_AGENT_ID = os.environ.get("SGME_HERMES_AGENT_ID", "hermes")

# _probe 探测结果缓存 TTL（秒）：失败短缓存允许 Gateway 事后启动恢复；成功长缓存避免每轮重复探测
_PROBE_FAIL_TTL = 3.0
_PROBE_OK_TTL = 30.0

# ---------- 工具面常量与参数辅助（能力面对齐 MCP 41 工具基准） ----------

# 检索层白名单（取值见 sgme/operations/search.py）
_SEARCH_SCOPES = ("memory", "wiki", "wiki_pages", "sessions", "skills")
# 检索默认层：记忆池 + 技能层（技能不预载——按需检索后再 sgme_skill_get 取全文）
_DEFAULT_SEARCH_SCOPES = ["memory", "skills"]
# 工具名 → 处理器方法名的例外表（其余工具按 sgme_<name> → _t_<name> 推导）
_TOOL_HANDLERS: Dict[str, str] = {
    "sgme_memory_search": "_t_search",
    "sgme_conversation_search": "_t_conversation_search",
    "sgme_signal_pull": "_tool_signal_pull",
    "sgme_signal_claim": "_tool_signal_claim",
    "sgme_signal_ack": "_tool_signal_ack",
}
# 技能节名前缀（# / ## ...）剥除正则
_SECTION_PREFIX_RE = re.compile(r"^#+\s*")


def _path_seg(value: Any) -> str:
    """URL 路径段编码（page_id / memory_id / 技能名，防斜杠与特殊字符破坏路由）。"""
    return quote(str(value), safe="")


def _normalize_section(section: Any) -> Optional[str]:
    """技能节名归一化：剥掉 # 前缀与两侧空白。

    契约坑（SGME 1.1.0 实测）：服务端 section 要**纯标题文本**（`前置条件`），
    而 skill_digest 的 sections 骨架给的是**带 # 的原样行**（`## 前置条件`）——
    照抄骨架传上去必 404，且错误文案误导为「技能不存在」。
    """
    if section is None:
        return None
    text = _SECTION_PREFIX_RE.sub("", str(section)).strip()
    return text or None


def _arg_str(args: Dict[str, Any], name: str, max_len: Optional[int] = 200) -> str:
    """取字符串参数（去空白；max_len=None 表示不限长，如技能/页面正文）。"""
    value = args.get(name)
    if value is None:
        return ""
    text = str(value).strip()
    return text if max_len is None else text[:max_len]


def _bool_arg(value: Any, default: bool) -> bool:
    """取布尔参数（容忍 "true"/"1"/"yes"/"on" 字符串形态）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _clamp_limit(value: Any, default: int, maximum: int) -> int:
    """取整数 limit：缺失/非法用 default，结果夹在 [1, maximum]。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = default
    return max(1, min(num, maximum))


def _pick_scopes(value: Any) -> List[str]:
    """取 scopes 列表：按白名单过滤（未知层丢弃，防误传到服务端）。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [s for s in (str(x).strip() for x in value) if s in _SEARCH_SCOPES]


def _as_tags(value: Any) -> Optional[List[str]]:
    """标签归一化：接受逗号分隔字符串或字符串数组；空值返回 None。"""
    if value is None:
        return None
    raw = list(value) if isinstance(value, (list, tuple)) else str(value).split(",")
    items = [str(t).strip() for t in raw]
    items = [t for t in items if t]
    return items or None


class SGMEProvider(MemoryProvider):
    """SGME 记忆引擎 Hermes 桥接 provider。

    配置优先级：plugin.yaml config 段 > 环境变量 > 默认值。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        agent_key: Optional[str] = None,
        admin_key: Optional[str] = None,
        inject_mode: Optional[str] = None,
        inject_max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        capture_enabled: bool = True,
        refine_on_end: bool = True,
        agent_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.agent_key = agent_key or _DEFAULT_AGENT_KEY
        self.admin_key = admin_key or _DEFAULT_ADMIN_KEY
        self.inject_mode = inject_mode or _DEFAULT_MODE
        self.inject_max_tokens = inject_max_tokens or _DEFAULT_MAX_TOKENS
        self.timeout = timeout or _DEFAULT_TIMEOUT
        self.capture_enabled = capture_enabled
        self.refine_on_end = refine_on_end
        # 溯源标识（B35）：append body 自报，可经 plugin.yaml config 段覆盖
        self.agent_id = agent_id or _DEFAULT_AGENT_ID

        self._session_id: str = ""
        self._hermes_home: str = ""
        self._client: Optional[httpx.Client] = None
        self._client_lock = threading.Lock()  # B152：client 创建/关闭互斥（防并发 close 套接字竞争）
        self._session_key: str = ""
        self._started_at: str = ""
        self._lock = threading.Lock()
        self._turn_buffer: List[Dict[str, Any]] = []  # 会话内消息缓冲（session 级 append）
        self._exported_keys: set = set()  # 已导出消息指纹（增量去重，ST-23③ 2026-08-11）
        self._last_started_at: str = ""  # 上次 append 的 started_at（单调兜底，防同刻碰撞）
        self._available: Optional[bool] = None  # 探测缓存（带 TTL，见 _probe）
        self._probe_at: Optional[float] = None  # 上次探测时间戳（time.monotonic）

    # ---------- 基础 ----------

    @property
    def name(self) -> str:
        return "sgme"

    def _http(self) -> Optional[httpx.Client]:
        """懒创建 httpx 客户端（trust_env=False 防 Clash 劫持 localhost）。

        B152（2026-09-05）：is_closed 检测——client 被 shutdown()（agent 拆卸时序）
        关闭后自动重建。原实现只判 None：后台线程在 shutdown 前拿到 client 引用，
        关闭后再发请求报 "Cannot send a request, as the client has been closed."
        （生产 341 次/天，2026-09-05 实锤）。
        """
        if not _HAS_HTTPX:
            return None
        with self._client_lock:
            if self._client is None or self._client.is_closed:
                try:
                    self._client = httpx.Client(
                        timeout=self.timeout, trust_env=False,
                    )
                except Exception:
                    return None
            return self._client

    def _probe(self) -> bool:
        """探测 SGME Gateway 可达性（结果带 TTL 缓存）。

        失败只缓存 _PROBE_FAIL_TTL 秒——Gateway 事后启动可恢复；
        成功缓存 _PROBE_OK_TTL 秒——避免每轮对话重复探测。
        """
        now = time.monotonic()
        if self._available is not None and self._probe_at is not None:
            ttl = _PROBE_OK_TTL if self._available else _PROBE_FAIL_TTL
            if now - self._probe_at < ttl:
                return self._available
        cli = self._http()
        if cli is None:
            self._available = False
            self._probe_at = now
            return False
        try:
            r = cli.get(f"{self.base_url}/v1/health", timeout=1.5)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        self._probe_at = now
        return self._available

    def is_available(self) -> bool:
        """只检查配置与依赖（不网络调用——ABC 约束）。"""
        if not _HAS_HTTPX:
            return False
        if not self.agent_key:
            return False
        # 不在这里探测网络（is_available 不许网络调用）；由 initialize 探测
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """会话初始化：记 session 上下文 + 后台探测 Gateway。"""
        self._session_id = session_id
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        agent_context = kwargs.get("agent_context") or "primary"
        if agent_context != "primary":
            # 非 primary（cron/subagent）不写入，防画像污染
            self.capture_enabled = False
            logger.info("sgme provider: agent_context=%s，禁用写入", agent_context)
        # v0.5（2026-08-07）：会话级元数据——session_key 与 started_at 固定为
        # 会话开始时刻，append 全程复用（原实现每轮取 now，导致 started_at
        # 失真为最后一轮时间，且同会话轮次时间戳漂移）
        from datetime import datetime, timezone
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not self._session_key:
            self._session_key = f"hermes-{session_id[:12]}"
        # 后台探测（不阻塞初始化）
        threading.Thread(target=self._probe, daemon=True).start()

    # ---------- 注入（读方向） ----------

    def system_prompt_block(self) -> str:
        """静态画像摘要（Tier0），进 system prompt。

        与 prefetch 的差别：这里是会话级静态信息，prefetch 是每轮动态召回。
        调用 /v1/inject mode=daily 拿 Tier0 摘要块。
        """
        cli = self._http()
        if cli is None or not self._probe():
            return ""
        try:
            r = cli.post(
                f"{self.base_url}/v1/inject",
                json={"mode": self.inject_mode, "max_tokens": self.inject_max_tokens},
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                return ""
            data = r.json()
            blocks = data.get("blocks", [])
            if not blocks:
                return ""
            lines = []
            for b in blocks:
                title = b.get("title", "")
                items = b.get("items", [])
                if not items:
                    continue
                lines.append(f"【{title}】")
                for it in items:
                    c = it.get("content", "")
                    if c:
                        lines.append(f"- {c}")
            if not lines:
                return ""
            return "\n".join(["# 用户画像（SGME）", *lines])
        except Exception as e:
            logger.warning("sgme system_prompt_block 失败: %s", e)
            return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮 LLM 前召回相关记忆 + 匹配场景（/v1/search，双 scope）。

        T-42 修正（2026-08-13 用户定）：场景注入 = 对话内容驱动——用当前
        对话语义检索 L2 场景（wiki scope），命中哪个场景就注入哪个，
        不是会话打开固定注入。时间预算 <100ms 目标：本地 HTTP 达标；
        失败返回空串不阻塞。
        """
        if not query or not self._probe():
            return ""
        cli = self._http()
        if cli is None:
            return ""
        try:
            r = cli.post(
                f"{self.base_url}/v1/search",
                json={"query": query[:200], "scopes": ["memory", "wiki"], "limit": 5},
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                return ""
            data = r.json()
            results = data.get("results", [])
            if not results:
                return ""
            # 按来源分块：记忆事实 vs L2 场景（语义匹配）
            mem_lines: List[str] = []
            scene_lines: List[str] = []
            for it in results:
                src = it.get("source", "")
                c = it.get("content", "")
                if not c:
                    continue
                if src == "wiki_scene":
                    title = it.get("title", "")
                    scene_lines.append(f"- [{title}] {c[:120]}")
                else:
                    mem_lines.append(f"- {c}")
            lines: List[str] = []
            if mem_lines:
                lines.append("# 相关记忆（SGME）")
                lines.extend(mem_lines[:5])
            if scene_lines:
                lines.append("# 相关场景（L2 匹配）")
                lines.extend(scene_lines[:3])
            return "\n".join(lines)
        except Exception as e:
            logger.warning("sgme prefetch 失败: %s", e)
            return ""

    # ---------- 写入（capture 方向） ----------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """每轮对话后写原始层（/v1/append，后台线程异步）。

        ST-23③（2026-08-11）：消息驱动增量导出——Hermes 运行时经
        memory_manager.sync_all 传入 messages 全量，本方法只导出本轮新增消息
        （_exported_keys 指纹去重），每轮用导出时刻作 started_at（恢复引擎
        追加语义；v0.5 固定 started_at 曾导致 08-07 起每轮捕获失效）。
        """
        if not self.capture_enabled:
            return
        # 后台写，不阻塞主流程
        if messages is not None:
            threading.Thread(
                target=self._append_delta,
                args=(messages,),
                daemon=True,
            ).start()
            return
        # 退化路径（无消息列表的调用方）：保留旧行为，写本轮 user/assistant 文本
        threading.Thread(
            target=self._append_turn,
            args=(user_content or "", assistant_content or ""),
            daemon=True,
        ).start()

    @staticmethod
    def _msg_key(msg: Dict[str, Any]) -> str:
        """消息去重指纹：tool 消息用 tool_call_id；其余用 role+内容哈希。

        ST-23③ tool 消息重复治理（2026-08-11 实锤：同时间戳 tool 输出
        重复 3-6 次）——指纹保证同一 tool 事件只导出一次。
        """
        role = str(msg.get("role", ""))
        if role == "tool":
            tid = str(msg.get("tool_call_id") or "")
            if tid:
                return f"tool:{tid}"
        content = str(msg.get("content") or "")
        return f"{role}:{hash(content)}"

    def _append_delta(self, messages: List[Dict[str, Any]]) -> None:
        """增量导出本轮新增消息（指纹去重）→ /v1/append。

        started_at 用本轮导出时刻：不同轮次时间戳不同 → 引擎追加语义生效
        （同 session_key + 同 started_at 幂等丢弃，不同则追加）。
        指纹标记先于网络请求（锁内完成），防并发重复导出。
        """
        cli = self._http()
        if cli is None or not self._probe():
            return
        from datetime import datetime, timezone, timedelta

        # 毫秒精度 + 单调兜底：秒级时间戳同秒内两轮 append 会撞幂等（08-11 实测）；
        # 单调递增保证快速连续调用也不碰撞（引擎侧同 session_key+同 started_at 丢弃）
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        # 锁内完成过滤 + 标记（原子），网络请求在锁外
        items: List[tuple[str, str]] = []
        with self._lock:
            if self._last_started_at and now_ts <= self._last_started_at:
                base = datetime.fromisoformat(self._last_started_at.replace("Z", "+00:00"))
                now_ts = (base + timedelta(milliseconds=1)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f"
                )[:-3] + "Z"
            self._last_started_at = now_ts
            for msg in messages or []:
                role = str(msg.get("role", ""))
                content = str(msg.get("content") or "")
                if role not in ("user", "assistant", "tool") or not content:
                    continue
                key = self._msg_key(msg)
                if key in self._exported_keys:
                    continue
                self._exported_keys.add(key)
                items.append((role, content))
        if not items:
            return
        content = "".join(f"# {now_ts} {role}\n{text}\n" for role, text in items)
        try:
            r = cli.post(
                f"{self.base_url}/v1/append",
                json={
                    "session_key": self._session_key or f"hermes-{self._session_id[:12]}",
                    "started_at": now_ts,
                    "agent_id": self.agent_id,
                    "content": content,
                },
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                logger.warning("sgme append 失败: %s %s", r.status_code, r.text[:150])
        except Exception as e:
            logger.warning("sgme append 异常: %s", e)

    def _append_turn(self, user_content: str, assistant_content: str) -> None:
        cli = self._http()
        if cli is None or not self._probe():
            return
        try:
            from datetime import datetime, timezone

            def _now() -> str:
                return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # v0.5：started_at 固定为会话开始时刻（initialize 记录），
            # 消息时间戳取各自真实生成时刻（不再统一 now）
            content = ""
            if user_content:
                content += f"# {_now()} user\n{user_content}\n"
            if assistant_content:
                content += f"# {_now()} assistant\n{assistant_content}\n"
            if not content:
                return
            r = cli.post(
                f"{self.base_url}/v1/append",
                json={
                    "session_key": self._session_key or f"hermes-{self._session_id[:12]}",
                    "started_at": self._started_at or _now(),
                    "agent_id": self.agent_id,
                    "content": content,
                },
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                logger.warning("sgme append 失败: %s %s", r.status_code, r.text[:150])
        except Exception as e:
            logger.warning("sgme append 异常: %s", e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """会话结束：补最后一轮增量 + 触发提炼（/v1/admin/refine/trigger）。

        ST-23③（2026-08-11）：先补导 messages 中尚未导出的消息（_exported_keys
        增量语义，覆盖末轮 append 后新增的消息），再触发提炼。
        提炼可能耗时数分钟（真实 LLM 分块），必须 fire-and-forget：
        独立线程 + 短连接超时，绝不阻塞 Hermes 会话结束流程。
        提炼在 SGME Gateway 侧后台进行，失败由 SGME 批扫兜底。
        """
        if not self.capture_enabled or not self.refine_on_end:
            return
        threading.Thread(target=self._on_session_end_worker, args=(messages or [],), daemon=True).start()

    def _on_session_end_worker(self, messages: List[Dict[str, Any]]) -> None:
        """补最后增量 + 触发提炼（同一后台线程，保证顺序：先落盘后提炼）。"""
        if messages:
            self._append_delta(messages)
        self._trigger_refine()

    def _trigger_refine(self) -> None:
        cli = self._http()
        if cli is None or not self._probe():
            return
        try:
            # 异步端点：SGME 后台线程提炼，立即返回（fire-and-forget）
            r = cli.post(
                f"{self.base_url}/v1/admin/refine/trigger_async",
                json={"limit": 50},
                headers={"X-API-Key": self.admin_key},
                timeout=5.0,
            )
            if r.status_code != 200:
                logger.warning("sgme refine trigger 失败: %s", r.status_code)
        except Exception as e:
            logger.warning("sgme refine trigger 异常: %s", e)

    # ---------- 工具（Agent 主动检索 / 治理：对齐 MCP 41 工具基准） ----------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """暴露 SGME 能力面工具。

        对齐唯一基准 = SGME MCP 工具面（sgme/mcp_server.py，41 个）；
        hermes 侧 2 项永久豁免（agent_onboarding / append）理由见 README。
        """
        return self._TOOL_SCHEMAS

    _TOOL_SCHEMAS: List[Dict[str, Any]] = [
        {
            "name": "sgme_memory_search",
            "description": "检索 SGME 记忆池（L1.5 标签化记忆）+ 技能层（默认两层并检）。"
                           "可检索技能层：命中时只返回技能名与触发描述（不含正文），"
                           "配合 sgme_skill_get 取全文后执行。"
                           "用于查找用户的长期事实、偏好、项目历史、决策记录；"
                           "查询不到时返回空，应如实告知「记忆库中未找到」。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或自然语言问题"},
                    "limit": {"type": "integer", "description": "返回条数（默认 5，最大 20）"},
                    "scopes": {
                        "type": "array", "items": {"type": "string"},
                        "description": "检索层（默认 [memory, skills]；可选 memory/skills/wiki/"
                                       "wiki_pages/sessions）",
                    },
                    "dimensions": {
                        "type": "array", "items": {"type": "string"},
                        "description": "维度过滤（identity/status/focus/goals/ideas 等）",
                    },
                    "match": {"type": "string", "description": "维度匹配语义：any=任一命中 / all=全部命中（默认 any）"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "sgme_conversation_search",
            "description": "检索 SGME 原始会话层（L0 raw，sessions scope，子串/BM25 匹配）。"
                           "用于查找「某次对话的原话」——记忆池是提炼后的事实，本工具是会话原文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "limit": {"type": "integer", "description": "返回条数（默认 5，最大 20）"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "sgme_inject",
            "description": "按场景模式拉取 SGME 画像注入（POST /v1/inject）。"
                           "模式：daily 日常 / coding 编码（技术栈/踩坑/工作方式）/ work 工作（目标/关系）/ full 全量。"
                           "低频使用：切换模式会改变注入块，同一会话内请勿反复切换。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "画像模板名（daily/coding/work/full；缺省用插件配置）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_answer",
            "description": "向 SGME 提聚合型问题并直接拿答案（跨会话计数/列举/时序推理，比检索多一步 LLM 合成）。"
                           "适用「我一共提过几次 X」「Y 是什么时候改的」「列出做过 Z 的项目」；"
                           "纯检索请用 sgme_memory_search。会消耗一次服务端 LLM 调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "自然语言问题"},
                    "question_type": {"type": "string", "description": "题型：temporal 时序 / aggregate 计数列举 / generic 通用（省略自动分派）"},
                    "limit": {"type": "integer", "description": "检索候选条数（默认 8，服务端上限 20）"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "sgme_wiki_search",
            "description": "检索 SGME wiki 知识库页面（含技能手册，执行通道不过滤 skill）。"
                           "用于查找操作手册/经验文档正文；配合 sgme_wiki_pages（列目录）与 "
                           "sgme_wiki_page（按 page_id 拉全文）使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或自然语言问题"},
                    "limit": {"type": "integer", "description": "返回条数（默认 10，最大 50）"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "sgme_wiki_pages",
            "description": "列出 SGME 知识库页面（可按 category 过滤，如 skill/sgme、design），返回轻量字段。"
                           "渐进式披露：先列目录判断加载哪本，再用 sgme_wiki_page 按 page_id 拉全文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "分类过滤（省略列出全部 active 页）"},
                    "limit": {"type": "integer", "description": "返回条数（默认 50，最大 200）"},
                    "offset": {"type": "integer", "description": "分页偏移（默认 0）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_wiki_page",
            "description": "按 page_id 拉取 SGME 知识库页面全文（含 frontmatter 与踩坑记录）。"
                           "page_id 来自 sgme_wiki_pages / sgme_wiki_search 返回结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "页面 id（wiki_pages 返回的 page_id）"},
                },
                "required": ["page_id"],
            },
        },
        {
            "name": "sgme_wiki_page_add",
            "description": "创建 SGME 知识库页面（原样写入，不走 LLM 提炼；同 title+content 幂等 upsert）。"
                           "category 建议 skill/<domain>（技能手册）或 design（设计方案）。"
                           "写入后立即可被 sgme_wiki_search 检索。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "页面标题（如「XXX 操作手册」）"},
                    "content": {"type": "string", "description": "页面正文（markdown）"},
                    "category": {"type": "string", "description": "分类（如 skill/sgme、design；可选）"},
                    "tags": {"type": "string", "description": "标签，逗号分隔（可选，如 sgme,运维,踩坑）"},
                    "description": {"type": "string", "description": "摘要（索引用，可选）"},
                    "author": {"type": "string", "description": "作者标识（可选，如 agent 名）"},
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "sgme_wiki_page_update",
            "description": "按 page_id 更新 SGME 知识库页面（默认 append=true 追加正文，append=false 整体覆盖）。"
                           "用于修正手册内容、追加踩坑记录或更新元数据（title/category/tags/description）。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "页面 id"},
                    "content": {"type": "string", "description": "要写入的正文（append=true 时追加到末尾）"},
                    "append": {"type": "boolean", "description": "默认 true 追加；false 整体覆盖"},
                    "title": {"type": "string", "description": "新标题（可选）"},
                    "category": {"type": "string", "description": "新分类（可选）"},
                    "tags": {"type": "string", "description": "新标签，逗号分隔（可选）"},
                    "description": {"type": "string", "description": "新摘要（可选）"},
                    "author": {"type": "string", "description": "作者标识（可选）"},
                },
                "required": ["page_id", "content"],
            },
        },
        {
            "name": "sgme_wiki_evolve_trigger",
            "description": "手动触发 SGME 自进化（会话经验 → 写回 wiki 手册）。"
                           "本适配器每轮结束已自动触发，本工具用于补触发（如指定会话立即提炼）。"
                           "服务端有费用门禁与规则闸门兜底（消息块不足会跳过），但仍会计入 LLM 调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_key": {"type": "string", "description": "指定会话（如 hermes-<会话id>）；省略由服务端按游标处理"},
                    "min_rounds": {"type": "integer", "description": "费用门禁：会话消息块下限（默认 5）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_memory_get",
            "description": "查询单条 SGME 记忆详情（内容/维度/状态 + 溯源 + 归档链）。"
                           "memory_id 来自 sgme_memory_search 结果，用于核实记忆准确性。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 id（memory_search 返回）"},
                },
                "required": ["memory_id"],
            },
        },
        {
            "name": "sgme_memory_reject",
            "description": "标记记忆「不采用」（用户指出记忆有误时调用；不删除、可恢复，之后不再注入/检索）。"
                           "幂等（重复调用更新理由）。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 id"},
                    "reason": {"type": "string", "description": "纠错理由（用户说明的错误原因）"},
                },
                "required": ["memory_id"],
            },
        },
        {
            "name": "sgme_memory_unreject",
            "description": "撤销记忆的「不采用」标记，恢复为 active（重新参与注入与检索）。"
                           "用于 sgme_memory_reject 误操作后的恢复。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆 id"},
                },
                "required": ["memory_id"],
            },
        },
        {
            "name": "sgme_refine_trigger",
            "description": "同步触发提炼：给 file_id 提炼单个会话原文，或扫 status=new 批量提炼。"
                           "⚠️ 同步阻塞直到完成且真实消耗 LLM 额度；批量任务优先用 sgme_refine_batch（异步）。"
                           "仅在用户明确要求立即提炼时调用。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "单个会话原文 id；省略则批量扫 status=new"},
                    "limit": {"type": "integer", "description": "批量上限（默认 100）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_refine_batch",
            "description": "异步批量提炼：后台线程执行，立即返回排队结果（不阻塞对话）。"
                           "⚠️ 会真实消耗 LLM 额度；仅在用户明确要求补提炼时调用。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "只提炼该文件；省略则批量扫 status=new"},
                    "limit": {"type": "integer", "description": "批量上限（默认 100）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_refine_status",
            "description": "查看 SGME 提炼状态：最近提炼批次记录（含 error/running）。"
                           "用于排查「会话入库了但没变成记忆」——看队列是否堆积、最近批次是否报错。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回最近批次条数（默认 10）"},
                    "status": {"type": "string", "description": "只看该状态的批次：running/ok/error（省略=全部）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_stats",
            "description": "SGME 统计概览：记忆总数/归档数、原始文件各状态计数、维度分布、提炼水位、已注册 agent。"
                           "用户问「记忆库现在多少条」「哪些维度用得最多」时用。需管理员 Key。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "sgme_health",
            "description": "SGME 健康自检：服务版本、LLM 可用性、提炼水位与是否停摆、向量库水位。"
                           "排查「记忆检索没结果」「刚说的话没进记忆」时先跑本工具定位是哪一环断了。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "sgme_config_get",
            "description": "读取 SGME 服务端运行时配置（整体读或按段读：l1/l2/refine/search/backup 等）。"
                           "用于核实服务端实际生效的配置值。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "配置段名（l1/l2/refine/search/backup）；省略返回全部"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_config_update",
            "description": "更新 SGME 服务端配置段（部分更新，合并后落盘并热生效）。"
                           "⚠️ 会改变记忆引擎的实际运行行为且立即生效，仅在用户明确要求时调用，"
                           "不确定当前值先用 sgme_config_get 读。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "要更新的配置段名（l1/l2/refine/search/backup）"},
                    "values": {"type": "object", "description": "该段的键值对（只传要改的键，未传的保留）"},
                },
                "required": ["section", "values"],
            },
        },
        {
            "name": "sgme_idea_add",
            "description": "添加创意到 SGME 创意池（仅当用户主动提出创意时才记录——不要自行发散）。"
                           "创意长期保存（无 TTL）；删除/升格由用户在 WebUI 操作。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "创意内容（一句话概括）"},
                    "priority": {"type": "integer", "description": "优先级 0-100（默认 50）"},
                    "source_ref": {"type": "string", "description": "溯源标识（可选，如会话主题）"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "sgme_demand_create",
            "description": "登记待办到 SGME 待办池（跨项目统一待办）。"
                           "会话中遇到用户要办的事/项目任务/后续跟进事项，主动调用本工具登记，不要只留在对话里。"
                           "project_id 是自由标记（未登记项目也允许）。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "待办标题（一句概括）"},
                    "content": {"type": "string", "description": "详情（可选）"},
                    "priority": {"type": "integer", "description": "优先级 0-100（默认 50）"},
                    "project_id": {"type": "string", "description": "关联项目 id（自由标记，一律大写，可选）"},
                    "source_ref": {"type": "string", "description": "溯源标识（可选）"},
                },
                "required": ["title"],
            },
        },
        {
            "name": "sgme_project_register",
            "description": "登记/创建项目到 SGME 项目池（仅当用户主动提出立项/创建时调用；upsert，二次登记=更新）。"
                           "project_id 用纯英文且一律大写；新建时 path 必填。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目 id（纯英文，一律大写，如 DHVS）"},
                    "path": {"type": "string", "description": "项目本地路径（新建时必填）"},
                    "name": {"type": "string", "description": "项目显示名（可选）"},
                    "git_repo": {"type": "string", "description": "git 仓库地址（可选）"},
                    "milestone": {"type": "string", "description": "当前里程碑（可选）"},
                },
                "required": ["project_id"],
            },
        },
        {
            "name": "sgme_signal_pull",
            "description": "拉取 SGME 未消费关怀信号（care_*：待办到期/情绪/过劳/每日）。"
                           "会话开始时调用一次，只拉取不消费；拿到事件后用 sgme_signal_claim "
                           "原子认领 → 关怀用户 → sgme_signal_ack 写回执（谁消费谁标记）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数（默认 20，最大 20）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_signal_claim",
            "description": "原子认领关怀信号（谁消费谁标记）。认领成功后应关怀用户，"
                           "再调 sgme_signal_ack 写回执；已被他人消费时返回 409，跳过即可。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "信号事件 id（来自 sgme_signal_pull 结果）"},
                },
                "required": ["event_id"],
            },
        },
        {
            "name": "sgme_signal_ack",
            "description": "写关怀信号消费回执（claimed/acked/failed）。"
                           "认领后处理完调用，报告处理结果供溯源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "信号事件 id"},
                    "status": {"type": "string", "description": "回执状态（默认 acked；可选 claimed/acked/failed）"},
                    "result": {"type": "string", "description": "处理结果摘要（如「已转告用户，提醒喝水」）"},
                },
                "required": ["event_id"],
            },
        },
        {
            "name": "sgme_signal_clear",
            "description": "批量清空 SGME 未消费信号（全部标记已消费，幂等；二次调用 consumed=0）。"
                           "用于信号堆积时的一次性清理。⚠️ 清空后 pull 不再推送这些信号——"
                           "仅在用户明确要求清理时调用。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "signal_type": {"type": "string", "description": "只清空该类型（如 anomaly_warn）；省略=全部类型"},
                    "subscriber_id": {"type": "string", "description": "同步推进该订阅者的持久游标（如 hermes）；省略则不推进"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_role_list",
            "description": "列出 SGME 可用角色模板（管家/伴侣/朋友/导师，含人设摘要）。"
                           "会话开始（或用户指定角色）时调用；选定后调 sgme_role_assemble 拿人设——换皮不换芯。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "sgme_role_assemble",
            "description": "装配角色沟通提示词（角色卡 system_prompt + persona + 关怀策略 + 可选画像）。"
                           "role_id 来自 sgme_role_list；产物作为风格指引使用——按角色语气说话，"
                           "但记忆与事实以记忆池为准。",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_id": {"type": "string", "description": "角色 id（sgme_role_list 返回）"},
                    "inject_mode": {"type": "string", "description": "画像注入模式（daily/coding/work/full；省略不带画像）"},
                },
                "required": ["role_id"],
            },
        },
        {
            "name": "sgme_role_active_get",
            "description": "读取当前沟通角色（未设置返回 role_id=null）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "sgme_role_active_set",
            "description": "设置当前沟通角色（换皮不换芯：只换沟通外皮，记忆池不动）。"
                           "role_id 必须存在（sgme_role_list 可见）；用户要求切换角色时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "role_id": {"type": "string", "description": "角色 id（sgme_role_list 返回）"},
                },
                "required": ["role_id"],
            },
        },
        {
            "name": "sgme_skill_search",
            "description": "检索 SGME 技能库（BM25+向量融合，全量技能）。需要某项专业能力但不确定有没有时必用。"
                           "只返回技能名与触发描述（不含正文）——选定后调 sgme_skill_get 取全文再执行，"
                           "不要凭空编造操作步骤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或能力描述（如「docker 部署」「pdf 提取」）"},
                    "limit": {"type": "integer", "description": "返回条数（默认 5，最大 20）"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "sgme_skill_digest",
            "description": "查看技能摘要（L1）：frontmatter 字段 + 正文小节骨架 + uses 依赖清单。"
                           "用于执行前审核——先看骨架判断是否对症、有没有依赖要一并拉，再决定要不要 sgme_skill_get 全文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名（sgme_skill_search 返回，kebab-case）"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "sgme_skill_get",
            "description": "拉取技能全文（L2）并注入上下文——拿到后按其步骤执行，不要凭空编造。"
                           "正文较长时传 section 只取该标题下的内容省 token（节名不对会 404，先 sgme_skill_digest 看骨架）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名（sgme_skill_search / sgme_skill_digest 返回）"},
                    "section": {"type": "string", "description": "只取该小节，传标题文本如「前置条件」（带 # 前缀会自动剥离）"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "sgme_skill_materialize",
            "description": "把 SGME 技能物化成真文件：字节保真写盘 dest_dir/<name>/SKILL.md，返回 path 与 sha256。"
                           "⚠️ 落盘发生在 SGME 服务端：dest_dir 与返回的 path 都是服务端路径；"
                           "跨机部署（agent 在本地、SGME 在远端）时本地拿不到产物，请改用 sgme_skill_get 取正文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名（kebab-case）"},
                    "dest_dir": {"type": "string", "description": "目标目录（技能写到 <dest_dir>/<name>/SKILL.md，服务端路径）"},
                },
                "required": ["name", "dest_dir"],
            },
        },
        {
            "name": "sgme_skill_list",
            "description": "列出 SGME 技能库索引（L0：name/description/category，分页浏览全量）。"
                           "不确定有没有某个技能时用 sgme_skill_search 更精准；本工具适合浏览摸底。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数（默认 50）"},
                    "offset": {"type": "integer", "description": "分页偏移（默认 0）"},
                },
                "required": [],
            },
        },
        {
            "name": "sgme_skill_coldstart",
            "description": "拉取技能冷启动包：仅注入《技能检索协议》+ SGME 操作手册（全量技能不预载）。"
                           "会话开始调一次即知道「要用技能时先检索、再拉全文」的正确姿势。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "sgme_skill_put",
            "description": "写入/覆盖 SGME 技能（content 传 SKILL.md 全文，服务端自动解析 frontmatter）。"
                           "⚠️ 服务端过 lint 门禁 + 三层查重后落盘并提交技能源仓（同名同内容/同内容异名会被拒）。"
                           "仅在用户明确要求沉淀技能时调用；写入前先 sgme_skill_search 查重。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名（kebab-case）"},
                    "content": {"type": "string", "description": "SKILL.md 全文（含 frontmatter）"},
                    "skip_limits": {"type": "boolean", "description": "超限从拒绝降为警告（仅历史存量整体入库用，默认 false）"},
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "sgme_skill_delete",
            "description": "删除 SGME 技能：默认软删（标记 deprecated，可恢复）；hard=true 物理删目录。"
                           "⚠️ 有入向 uses 引用时服务端会拒绝，需 force=true 强制——属破坏性操作，"
                           "仅在用户明确要求删除时才调用。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名"},
                    "hard": {"type": "boolean", "description": "物理删除（默认 false=软删标记 deprecated）"},
                    "force": {"type": "boolean", "description": "强制清理入向引用后删除（默认 false）"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "sgme_skill_rename",
            "description": "技能改名（墓碑制：写新名副本 + 旧位置留 superseded_by 墓碑，永不原地改名）。"
                           "⚠️ 新名已占用或过不了门禁会被拒；仅在用户明确要求改名时调用。需管理员 Key。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "旧技能名"},
                    "new_name": {"type": "string", "description": "新技能名（kebab-case）"},
                },
                "required": ["name", "new_name"],
            },
        },
    ]

    # ---------- 工具实现（统一 HTTP 桥接；一切失败结构化返回，绝不抛异常） ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        key: Optional[str] = "agent",
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> "tuple[Optional[Any], Optional[str]]":
        """统一 HTTP 调用：返回 (data, error)；任何异常都转 error，不抛。

        key："agent" 用 Agent Key / "admin" 用管理员 Key / None 免鉴权（仅 /v1/health）。
        客户端 trust_env=False（防代理劫持回环请求，项目铁律）。
        """
        cli = self._http()
        if cli is None:
            return None, "httpx 不可用（插件依赖缺失）"
        headers: Dict[str, str] = {}
        if key == "agent":
            headers["X-API-Key"] = self.agent_key
        elif key == "admin":
            headers["X-API-Key"] = self.admin_key
        sender = getattr(cli, str(method).lower(), None)
        if sender is None:
            return None, f"客户端不支持 {method} 请求"
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": timeout or self.timeout}
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            r = sender(f"{self.base_url}{path}", **kwargs)
        except Exception as e:
            return None, f"SGME Gateway 不可达: {e}"
        if not 200 <= int(getattr(r, "status_code", 0)) < 300:
            detail = str(getattr(r, "text", "") or "")[:200]
            return None, f"SGME 返回 {r.status_code}: {detail}"
        try:
            return r.json(), None
        except Exception as e:
            return None, f"响应解析失败: {e}"

    @staticmethod
    def _ok(payload: Any) -> str:
        """工具成功返回：JSON 字符串（Hermes 工具层约定返回值类型）。"""
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _err(message: str) -> str:
        """工具失败返回：结构化错误（不抛异常，不阻塞 Hermes 主流程）。"""
        return json.dumps({"error": message}, ensure_ascii=False)

    @staticmethod
    def _ok_receipt(data: Any) -> str:
        """登记类工具回执**精简**（T-204 D0：输入侧防污染）。

        背景：工具返回值会进入会话上下文，随下一轮 ``append`` 写入 L0，再被 L1
        当「用户事实」提炼 → 生产实证残迹 ``…已建档（source_ref=「hermes 会话…」），
        priority=70``。登记类回执里的 ``priority`` / ``source_ref`` / ``content``
        等结构化字段本就不是事实，回显即污染源。

        故只保留 id 类与 status，其余一律不回显（信息不丢：需要详情走对应查询工具）。
        """
        if not isinstance(data, dict):
            return SGMEProvider._ok({"status": "ok"})
        out: Dict[str, Any] = {"status": "ok"}
        for key in ("demand_id", "idea_id", "project_id", "page_id", "memory_id"):
            if key in data:
                out[key] = data[key]
        return SGMEProvider._ok(out)

    def _t_search(self, args: Dict[str, Any]) -> str:
        """sgme_memory_search：统一检索（默认 memory + skills 两层，可按 scopes 覆盖）。"""
        query = _arg_str(args, "query", 200)
        if not query:
            return self._err("缺少 query")
        scopes = _pick_scopes(args.get("scopes")) or list(_DEFAULT_SEARCH_SCOPES)
        limit = _clamp_limit(args.get("limit"), 5, 20)
        body: Dict[str, Any] = {"query": query, "scopes": scopes, "limit": limit}
        dimensions = args.get("dimensions")
        if isinstance(dimensions, (list, tuple)) and dimensions:
            body["dimensions"] = [str(d).strip() for d in dimensions if str(d).strip()]
        match = _arg_str(args, "match")
        if match in ("any", "all"):
            body["match"] = match
        data, err = self._request("POST", "/v1/search", json_body=body)
        if err:
            return self._err(f"检索失败: {err}")
        results = data.get("results") if isinstance(data, dict) else None
        results = results or []
        return self._ok({
            "query": query, "scopes": scopes,
            "count": len(results), "results": results[:limit],
        })

    def _t_conversation_search(self, args: Dict[str, Any]) -> str:
        """sgme_conversation_search：L0 原始会话层检索（sessions scope）。"""
        query = _arg_str(args, "query", 200)
        if not query:
            return self._err("缺少 query")
        limit = _clamp_limit(args.get("limit"), 5, 20)
        data, err = self._request(
            "POST", "/v1/search",
            json_body={"query": query, "scopes": ["sessions"], "limit": limit},
        )
        if err:
            return self._err(f"会话检索失败: {err}")
        results = data.get("results") if isinstance(data, dict) else None
        results = results or []
        return self._ok({
            "query": query, "scopes": ["sessions"],
            "count": len(results), "results": results[:limit],
        })

    def _t_inject(self, args: Dict[str, Any]) -> str:
        """sgme_inject：按场景模式拉取画像（POST /v1/inject）。"""
        mode = _arg_str(args, "mode") or self.inject_mode
        data, err = self._request(
            "POST", "/v1/inject", json_body={"mode": mode},
        )
        if err:
            return self._err(f"画像注入失败: {err}")
        if not isinstance(data, dict):
            return self._ok({"mode": mode, "blocks": []})
        out = dict(data)
        out.setdefault("mode", mode)
        return self._ok(out)

    def _t_answer(self, args: Dict[str, Any]) -> str:
        """sgme_answer：聚合型提问（服务端检索候选 + LLM 合成答案）。"""
        query = _arg_str(args, "query", 500)
        if not query:
            return self._err("缺少 query")
        body: Dict[str, Any] = {"query": query}
        question_type = _arg_str(args, "question_type")
        if question_type in ("temporal", "aggregate", "generic"):
            body["question_type"] = question_type
        if args.get("limit") not in (None, ""):
            body["limit"] = _clamp_limit(args.get("limit"), 8, 20)
        data, err = self._request("POST", "/v1/answer", json_body=body)
        if err:
            return self._err(f"聚合问答失败（LLM 链路可能不可用）: {err}")
        if not isinstance(data, dict):
            return self._err("聚合问答响应异常")
        return self._ok(data)

    def _t_wiki_search(self, args: Dict[str, Any]) -> str:
        """sgme_wiki_search：wiki 知识库页面检索（执行通道，含 skill 手册）。"""
        query = _arg_str(args, "query", 200)
        if not query:
            return self._err("缺少 query")
        limit = _clamp_limit(args.get("limit"), 10, 50)
        data, err = self._request(
            "GET", "/v1/wiki/search", params={"q": query, "limit": limit},
        )
        if err:
            return self._err(f"wiki 检索失败: {err}")
        results = data.get("results") if isinstance(data, dict) else None
        results = results or []
        return self._ok({"query": query, "count": len(results), "results": results})

    def _t_wiki_pages(self, args: Dict[str, Any]) -> str:
        """sgme_wiki_pages：按分类列出知识库页面（轻量字段）。"""
        params: Dict[str, Any] = {
            "limit": _clamp_limit(args.get("limit"), 50, 200),
            "offset": max(0, _clamp_limit(args.get("offset"), 0, 100000) if args.get("offset") else 0),
        }
        category = _arg_str(args, "category")
        if category:
            params["category"] = category
        data, err = self._request("GET", "/v1/wiki/pages", params=params)
        if err:
            return self._err(f"列知识库页面失败: {err}")
        if not isinstance(data, dict):
            return self._err("列知识库页面响应异常")
        return self._ok(data)

    def _t_wiki_page(self, args: Dict[str, Any]) -> str:
        """sgme_wiki_page：按 page_id 拉取知识库页面全文。"""
        page_id = _arg_str(args, "page_id")
        if not page_id:
            return self._err("缺少 page_id")
        data, err = self._request("GET", f"/v1/wiki/pages/{_path_seg(page_id)}")
        if err:
            return self._err(f"拉取页面失败（page_id={page_id}）: {err}")
        if not isinstance(data, dict):
            return self._err("拉取页面响应异常")
        return self._ok(data)

    def _t_wiki_page_add(self, args: Dict[str, Any]) -> str:
        """sgme_wiki_page_add：创建知识库页面（幂等 upsert，不走 LLM）。需管理员 Key。"""
        title = _arg_str(args, "title", 500)
        content = _arg_str(args, "content", None)
        if not title or not content:
            return self._err("缺少 title 或 content")
        body: Dict[str, Any] = {"title": title, "content": content}
        category = _arg_str(args, "category")
        if category:
            body["category"] = category
        tags = _as_tags(args.get("tags"))
        if tags:
            body["tags"] = tags
        description = _arg_str(args, "description", 1000)
        if description:
            body["description"] = description
        author = _arg_str(args, "author") or self.agent_id
        body["author"] = author
        data, err = self._request("POST", "/v1/wiki/pages", key="admin", json_body=body)
        if err:
            return self._err(f"写入知识库页面失败: {err}")
        return self._ok(data if isinstance(data, dict) else {"status": "ok"})

    def _t_wiki_page_update(self, args: Dict[str, Any]) -> str:
        """sgme_wiki_page_update：按 page_id 更新/追加页面正文。需管理员 Key。"""
        page_id = _arg_str(args, "page_id")
        content = _arg_str(args, "content", None)
        if not page_id or not content:
            return self._err("缺少 page_id 或 content")
        body: Dict[str, Any] = {"content": content, "append": _bool_arg(args.get("append"), True)}
        tags = _as_tags(args.get("tags"))
        if tags:
            body["tags"] = tags
        for field, max_len in (("title", 500), ("category", 200), ("description", 1000)):
            value = _arg_str(args, field, max_len)
            if value:
                body[field] = value
        author = _arg_str(args, "author") or self.agent_id
        body["author"] = author
        data, err = self._request(
            "PATCH", f"/v1/wiki/pages/{_path_seg(page_id)}", key="admin", json_body=body,
        )
        if err:
            return self._err(f"更新知识库页面失败（page_id={page_id}）: {err}")
        return self._ok(data if isinstance(data, dict) else {"page_id": page_id, "status": "ok"})

    def _t_wiki_evolve_trigger(self, args: Dict[str, Any]) -> str:
        """sgme_wiki_evolve_trigger：手动补触发自进化（会话经验 → wiki 手册）。"""
        body: Dict[str, Any] = {"min_rounds": _clamp_limit(args.get("min_rounds"), 5, 100)}
        session_key = _arg_str(args, "session_key")
        if session_key:
            body["session_key"] = session_key
        data, err = self._request("POST", "/v1/wiki/evolve/trigger", json_body=body)
        if err:
            return self._err(f"自进化触发失败: {err}")
        return self._ok(data if isinstance(data, dict) else {"status": "ok"})

    def _t_memory_get(self, args: Dict[str, Any]) -> str:
        """sgme_memory_get：单条记忆详情（含溯源与归档链）。"""
        memory_id = _arg_str(args, "memory_id")
        if not memory_id:
            return self._err("缺少 memory_id")
        data, err = self._request("GET", f"/v1/memory/{_path_seg(memory_id)}")
        if err:
            return self._err(f"查询记忆失败（memory_id={memory_id}）: {err}")
        if not isinstance(data, dict):
            return self._err("查询记忆响应异常")
        return self._ok(data)

    def _t_memory_reject(self, args: Dict[str, Any]) -> str:
        """sgme_memory_reject：标记记忆不采用（不删除、可恢复）。需管理员 Key。"""
        memory_id = _arg_str(args, "memory_id")
        if not memory_id:
            return self._err("缺少 memory_id")
        reason = _arg_str(args, "reason", 500) or "用户纠错"
        data, err = self._request(
            "POST", f"/v1/memory/{_path_seg(memory_id)}/reject",
            key="admin", json_body={"reason": reason},
        )
        if err:
            return self._err(f"标记不采用失败（memory_id={memory_id}）: {err}")
        return self._ok(data if isinstance(data, dict) else {"memory_id": memory_id, "status": "rejected"})

    def _t_memory_unreject(self, args: Dict[str, Any]) -> str:
        """sgme_memory_unreject：撤销「不采用」，恢复 active。需管理员 Key。"""
        memory_id = _arg_str(args, "memory_id")
        if not memory_id:
            return self._err("缺少 memory_id")
        data, err = self._request(
            "POST", f"/v1/memory/{_path_seg(memory_id)}/unreject",
            key="admin", json_body={},
        )
        if err:
            return self._err(f"撤销不采用失败（memory_id={memory_id}）: {err}")
        return self._ok(data if isinstance(data, dict) else {"memory_id": memory_id, "status": "active"})

    def _t_refine_trigger(self, args: Dict[str, Any]) -> str:
        """sgme_refine_trigger：同步提炼（阻塞 + 消耗 LLM 额度）。需管理员 Key。"""
        body: Dict[str, Any] = {}
        file_id = _arg_str(args, "file_id")
        if file_id:
            body["file_id"] = file_id
        if args.get("limit") not in (None, ""):
            body["limit"] = _clamp_limit(args.get("limit"), 100, 1000)
        data, err = self._request("POST", "/v1/admin/refine/trigger", key="admin", json_body=body)
        if err:
            return self._err(f"提炼失败: {err}")
        return self._ok(data if isinstance(data, dict) else {"status": "ok"})

    def _t_refine_batch(self, args: Dict[str, Any]) -> str:
        """sgme_refine_batch：异步批量提炼（排队即返）。需管理员 Key。"""
        body: Dict[str, Any] = {}
        file_id = _arg_str(args, "file_id")
        if file_id:
            body["file_id"] = file_id
        if args.get("limit") not in (None, ""):
            body["limit"] = _clamp_limit(args.get("limit"), 100, 1000)
        data, err = self._request(
            "POST", "/v1/admin/refine/trigger_async", key="admin", json_body=body,
        )
        if err:
            return self._err(f"批量提炼排队失败: {err}")
        return self._ok(data if isinstance(data, dict) else {"status": "queued"})

    def _t_refine_status(self, args: Dict[str, Any]) -> str:
        """sgme_refine_status：提炼批次记录（服务端无独立 status 端点，用 refine_runs 近似）。需管理员 Key。"""
        params: Dict[str, Any] = {"limit": _clamp_limit(args.get("limit"), 10, 200)}
        status = _arg_str(args, "status")
        if status in ("running", "ok", "error"):
            params["status"] = status
        data, err = self._request("GET", "/v1/admin/refine_runs", key="admin", params=params)
        if err:
            return self._err(f"查询提炼状态失败: {err}")
        if not isinstance(data, dict):
            return self._err("查询提炼状态响应异常")
        return self._ok(data)

    def _t_stats(self, args: Dict[str, Any]) -> str:
        """sgme_stats：统计概览。需管理员 Key。"""
        data, err = self._request("GET", "/v1/admin/stats", key="admin")
        if err:
            return self._err(f"查询统计失败: {err}")
        if not isinstance(data, dict):
            return self._err("查询统计响应异常")
        return self._ok(data)

    def _t_health(self, args: Dict[str, Any]) -> str:
        """sgme_health：健康自检（/v1/health 免鉴权）。"""
        data, err = self._request("GET", "/v1/health", key=None, timeout=min(self.timeout, 3.0))
        if err:
            return self._err(f"SGME Gateway 不可达（本插件是桥接插件，请确认 SGME 本体在运行）: {err}")
        if not isinstance(data, dict):
            return self._err("健康检查响应异常")
        return self._ok(data)

    def _t_config_get(self, args: Dict[str, Any]) -> str:
        """sgme_config_get：读服务端运行时配置（整体或按段）。需管理员 Key。"""
        section = _arg_str(args, "section")
        path = f"/v1/admin/config/{_path_seg(section)}" if section else "/v1/admin/config"
        data, err = self._request("GET", path, key="admin")
        if err:
            return self._err(f"读配置失败: {err}")
        if not isinstance(data, dict):
            return self._err("读配置响应异常")
        return self._ok(data)

    def _t_config_update(self, args: Dict[str, Any]) -> str:
        """sgme_config_update：更新服务端配置段（热生效 + 落盘）。需管理员 Key。"""
        section = _arg_str(args, "section")
        values = args.get("values")
        if not section:
            return self._err("缺少 section（分段更新，防整段误覆盖）")
        if not isinstance(values, dict) or not values:
            return self._err("缺少 values（要修改的键值对）")
        data, err = self._request(
            "POST", "/v1/admin/config", key="admin",
            json_body={"section": section, "values": values},
        )
        if err:
            return self._err(f"更新配置失败（section={section}）: {err}")
        return self._ok(data if isinstance(data, dict) else {"section": section, "status": "ok"})

    def _t_idea_add(self, args: Dict[str, Any]) -> str:
        """sgme_idea_add：登记创意池（用户主动提出才记录）。需管理员 Key。"""
        content = _arg_str(args, "content", 2000)
        if not content:
            return self._err("缺少 content")
        body: Dict[str, Any] = {"content": content}
        if args.get("priority") not in (None, ""):
            body["priority"] = _clamp_limit(args.get("priority"), 50, 100)
        source_ref = _arg_str(args, "source_ref", 500)
        if source_ref:
            body["source_ref"] = source_ref
        data, err = self._request("POST", "/v1/admin/ideas", key="admin", json_body=body)
        if err:
            return self._err(f"登记创意失败: {err}")
        return self._ok_receipt(data)  # T-204 D0：回执精简，防结构化字段进 L0

    def _t_demand_create(self, args: Dict[str, Any]) -> str:
        """sgme_demand_create：登记待办池（跨项目统一）。需管理员 Key。"""
        title = _arg_str(args, "title", 500)
        if not title:
            return self._err("缺少 title")
        body: Dict[str, Any] = {"title": title}
        content = _arg_str(args, "content", 4000)
        if content:
            body["content"] = content
        if args.get("priority") not in (None, ""):
            body["priority"] = _clamp_limit(args.get("priority"), 50, 100)
        for field, max_len in (("project_id", 200), ("source_ref", 500)):
            value = _arg_str(args, field, max_len)
            if value:
                body[field] = value
        data, err = self._request("POST", "/v1/admin/demands", key="admin", json_body=body)
        if err:
            return self._err(f"登记待办失败: {err}")
        return self._ok_receipt(data)  # T-204 D0：回执精简，防结构化字段进 L0

    def _t_project_register(self, args: Dict[str, Any]) -> str:
        """sgme_project_register：登记/更新项目池（upsert）。需管理员 Key。"""
        project_id = _arg_str(args, "project_id", 200)
        if not project_id:
            return self._err("缺少 project_id")
        body: Dict[str, Any] = {"project_id": project_id}
        for field, max_len in (("path", 500), ("name", 300), ("git_repo", 500), ("milestone", 300)):
            value = _arg_str(args, field, max_len)
            if value:
                body[field] = value
        data, err = self._request("POST", "/v1/admin/projects", key="admin", json_body=body)
        if err:
            return self._err(f"登记项目失败: {err}")
        receipt = self._ok_receipt(data)  # T-204 D0：回执精简
        # project_id 是调用方自己传的登记值，回显无污染风险且便于串联后续调用
        if isinstance(data, dict) and "project_id" not in data:
            import json as _json
            payload = _json.loads(receipt)
            payload["project_id"] = project_id
            receipt = self._ok(payload)
        return receipt

    def _t_signal_clear(self, args: Dict[str, Any]) -> str:
        """sgme_signal_clear：批量清空未消费信号（幂等）。需管理员 Key。"""
        params: Dict[str, Any] = {}
        signal_type = _arg_str(args, "signal_type")
        if signal_type:
            params["type"] = signal_type
        subscriber_id = _arg_str(args, "subscriber_id")
        if subscriber_id:
            params["subscriber_id"] = subscriber_id
        data, err = self._request(
            "POST", "/v1/admin/events/consume_all", key="admin",
            params=params or None, json_body={},
        )
        if err:
            return self._err(f"清空信号失败: {err}")
        return self._ok(data if isinstance(data, dict) else {"status": "ok"})

    def _t_role_list(self, args: Dict[str, Any]) -> str:
        """sgme_role_list：可用角色模板列表。"""
        data, err = self._request("GET", "/v1/admin/roles")
        if err:
            return self._err(f"列角色失败: {err}")
        if not isinstance(data, dict):
            return self._err("列角色响应异常")
        return self._ok(data)

    def _t_role_assemble(self, args: Dict[str, Any]) -> str:
        """sgme_role_assemble：装配角色沟通提示词（换皮不换芯）。"""
        role_id = _arg_str(args, "role_id")
        if not role_id:
            return self._err("缺少 role_id")
        inject_mode = _arg_str(args, "inject_mode")
        params = {"inject_mode": inject_mode} if inject_mode else None
        data, err = self._request(
            "GET", f"/v1/admin/roles/{_path_seg(role_id)}/assemble", params=params,
        )
        if err:
            return self._err(f"装配角色失败（role_id={role_id}）: {err}")
        if not isinstance(data, dict):
            return self._err("装配角色响应异常")
        return self._ok(data)

    def _t_role_active_get(self, args: Dict[str, Any]) -> str:
        """sgme_role_active_get：读当前沟通角色。"""
        data, err = self._request("GET", "/v1/admin/care/active-role")
        if err:
            return self._err(f"读当前角色失败: {err}")
        if not isinstance(data, dict):
            return self._err("读当前角色响应异常")
        return self._ok(data)

    def _t_role_active_set(self, args: Dict[str, Any]) -> str:
        """sgme_role_active_set：设置当前沟通角色。"""
        role_id = _arg_str(args, "role_id")
        if not role_id:
            return self._err("缺少 role_id")
        data, err = self._request(
            "PUT", "/v1/admin/care/active-role", json_body={"role_id": role_id},
        )
        if err:
            return self._err(f"设置角色失败（role_id={role_id}）: {err}")
        return self._ok(data if isinstance(data, dict) else {"role_id": role_id, "status": "ok"})

    def _t_skill_search(self, args: Dict[str, Any]) -> str:
        """sgme_skill_search：技能检索（/v1/search scope=skills，只给名与描述）。"""
        query = _arg_str(args, "query", 200)
        if not query:
            return self._err("缺少 query")
        limit = _clamp_limit(args.get("limit"), 5, 20)
        data, err = self._request(
            "POST", "/v1/search",
            json_body={"query": query, "scopes": ["skills"], "limit": limit},
        )
        if err:
            return self._err(f"技能检索失败（技能模块可能未启用）: {err}")
        results = data.get("results") if isinstance(data, dict) else None
        results = results or []
        return self._ok({
            "query": query, "count": len(results), "results": results[:limit],
            "hint": "结果只含技能名与触发描述；用 sgme_skill_get 传 name 取全文后执行",
        })

    def _t_skill_digest(self, args: Dict[str, Any]) -> str:
        """sgme_skill_digest：技能 L1 摘要（骨架 + uses 依赖）。"""
        name = _arg_str(args, "name")
        if not name:
            return self._err("缺少 name")
        data, err = self._request("GET", f"/v1/skills/{_path_seg(name)}/digest")
        if err:
            return self._err(f"技能不存在或 Gateway 不可达（name={name}）: {err}")
        if not isinstance(data, dict):
            return self._err("技能摘要响应异常")
        return self._ok(data)

    def _t_skill_get(self, args: Dict[str, Any]) -> str:
        """sgme_skill_get：技能 L2 全文（section 给定时只取该节）。"""
        name = _arg_str(args, "name")
        if not name:
            return self._err("缺少 name")
        section = _normalize_section(args.get("section"))
        params = {"section": section} if section else None
        data, err = self._request("GET", f"/v1/skills/{_path_seg(name)}", params=params)
        if err:
            return self._err(
                f"拉取技能失败（name={name}"
                + (f", section={section}" if section else "")
                + f"；先 sgme_skill_search 确认）: {err}"
            )
        if not isinstance(data, dict):
            return self._err("技能全文响应异常")
        out = dict(data)
        out.setdefault("name", name)
        if section:
            out.setdefault("section", section)
        return self._ok(out)

    def _t_skill_list(self, args: Dict[str, Any]) -> str:
        """sgme_skill_list：技能 L0 索引列表（分页）。"""
        params = {
            "limit": _clamp_limit(args.get("limit"), 50, 500),
            "offset": max(0, int(args.get("offset") or 0)),
        }
        data, err = self._request("GET", "/v1/skills", params=params)
        if err:
            return self._err(f"列技能失败（技能模块可能未启用）: {err}")
        if not isinstance(data, dict):
            return self._err("列技能响应异常")
        return self._ok(data)

    def _t_skill_coldstart(self, args: Dict[str, Any]) -> str:
        """sgme_skill_coldstart：冷启动包（技能检索协议 + SGME 操作手册）。"""
        data, err = self._request("GET", "/v1/skills/coldstart")
        if err:
            return self._err(f"拉取冷启动包失败（技能模块可能未启用）: {err}")
        if not isinstance(data, dict):
            return self._err("冷启动包响应异常")
        return self._ok(data)

    def _t_skill_materialize(self, args: Dict[str, Any]) -> str:
        """sgme_skill_materialize：技能 L3 物化落盘（服务端路径），返回 path + sha256。"""
        name = _arg_str(args, "name")
        dest_dir = _arg_str(args, "dest_dir", 1000)
        if not name or not dest_dir:
            return self._err("缺少 name 或 dest_dir")
        data, err = self._request(
            "POST", f"/v1/skills/{_path_seg(name)}/materialize",
            json_body={"dest_dir": dest_dir},
        )
        if err:
            return self._err(f"技能物化失败（name={name}）: {err}")
        if not isinstance(data, dict):
            return self._err("技能物化响应异常")
        return self._ok(data)

    def _t_skill_put(self, args: Dict[str, Any]) -> str:
        """sgme_skill_put：写入/覆盖技能（lint 门禁 + 查重后落盘提交）。需管理员 Key。"""
        name = _arg_str(args, "name")
        content = _arg_str(args, "content", None)
        if not name or not content:
            return self._err("缺少 name 或 content")
        body = {"content": content, "skip_limits": _bool_arg(args.get("skip_limits"), False)}
        data, err = self._request(
            "PUT", f"/v1/admin/skills/{_path_seg(name)}", key="admin", json_body=body,
        )
        if err:
            return self._err(f"写入技能失败（lint 门禁/查重拒绝/未配置管理员 Key）: {err}")
        return self._ok(data if isinstance(data, dict) else {"name": name, "status": "ok"})

    def _t_skill_delete(self, args: Dict[str, Any]) -> str:
        """sgme_skill_delete：删除技能（默认软删）。需管理员 Key。"""
        name = _arg_str(args, "name")
        if not name:
            return self._err("缺少 name")
        params: Dict[str, Any] = {}
        if _bool_arg(args.get("hard"), False):
            params["hard"] = "true"
        if _bool_arg(args.get("force"), False):
            params["force"] = "true"
        data, err = self._request(
            "DELETE", f"/v1/admin/skills/{_path_seg(name)}", key="admin",
            params=params or None,
        )
        if err:
            return self._err(f"删除技能失败（存在入向引用且未 force / 未配置管理员 Key）: {err}")
        return self._ok(data if isinstance(data, dict) else {"name": name, "status": "ok"})

    def _t_skill_rename(self, args: Dict[str, Any]) -> str:
        """sgme_skill_rename：技能改名（墓碑制）。需管理员 Key。"""
        name = _arg_str(args, "name")
        new_name = _arg_str(args, "new_name")
        if not name or not new_name:
            return self._err("缺少 name 或 new_name")
        data, err = self._request(
            "POST", f"/v1/admin/skills/{_path_seg(name)}/rename", key="admin",
            json_body={"new_name": new_name},
        )
        if err:
            return self._err(f"技能改名失败（{name} → {new_name}）: {err}")
        return self._ok(data if isinstance(data, dict) else {"name": new_name, "status": "ok"})

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """分发 sgme_* 工具调用 → SGME Gateway HTTP。

        约定：一切失败都返回结构化 JSON 错误（{"error": ...}），绝不抛异常——
        Hermes 工具层拿到异常会记 error 日志，但静默降级更符合「记忆可选」的定位。
        """
        args = args or {}
        handler_name = _TOOL_HANDLERS.get(tool_name)
        if not handler_name and tool_name.startswith("sgme_"):
            handler_name = f"_t_{tool_name[len('sgme_'):]}"
        handler = getattr(self, handler_name, None) if handler_name else None
        if handler is None:
            return self._err(f"未知工具 {tool_name}")
        try:
            return handler(args)
        except Exception as e:  # 兜底：工具层异常不得冒泡到 Hermes 主流程
            logger.warning("sgme 工具 %s 执行失败: %s", tool_name, e)
            return self._err(f"工具 {tool_name} 执行失败: {e}")

    # ---------- 关怀信号消费（ST-27 T-60：谁消费谁标记） ----------

    @staticmethod
    def _fmt_signal_events(events: List[Dict[str, Any]]) -> List[str]:
        """事件列表 → 简洁可读行（id/type/ts/payload 摘要），供 agent 快速判断。"""
        lines: List[str] = []
        for ev in events or []:
            eid = str(ev.get("id") or ev.get("event_id") or "")
            etype = str(ev.get("type") or ev.get("event_type") or "")
            ts = str(ev.get("ts") or ev.get("timestamp") or ev.get("created_at") or "")
            payload = ev.get("payload")
            if isinstance(payload, dict):
                payload = json.dumps(payload, ensure_ascii=False)[:200]
            else:
                payload = str(payload or "")[:200]
            lines.append(f"- [{eid}] {etype} @{ts} {payload}".rstrip())
        return lines

    def _tool_signal_pull(self, args: Dict[str, Any]) -> str:
        """sgme_signal_pull：拉取未消费关怀信号（只拉取，不消费）。

        契约：GET {base}/v1/events/pull?subscriber_id=<agent_id>&limit=N
        返回 {events:[...], next_cursor}。每次调用最多拉 limit 条（≤20），
        不循环拉空——会话开始时调一次即可，剩余留给后续轮次/其他消费者。
        """
        cli = self._http()
        if cli is None or not self._probe():
            return json.dumps({"error": "SGME Gateway 不可达"}, ensure_ascii=False)
        limit = min(max(int(args.get("limit", 20)), 1), 20)
        try:
            r = cli.get(
                f"{self.base_url}/v1/events/pull",
                params={"subscriber_id": self.agent_id, "limit": limit},
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                return json.dumps({"error": f"SGME 返回 {r.status_code}"}, ensure_ascii=False)
            data = r.json()
            events = data.get("events", [])[:limit]
            next_cursor = data.get("next_cursor")
            summary = "\n".join(self._fmt_signal_events(events))
            if not events:
                return json.dumps(
                    {"events": [], "next_cursor": next_cursor, "message": "暂无未消费关怀信号"},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"events": events, "next_cursor": next_cursor, "summary": summary},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": f"拉取信号失败: {e}"}, ensure_ascii=False)

    def _tool_signal_claim(self, args: Dict[str, Any]) -> str:
        """sgme_signal_claim：原子认领关怀信号（谁消费谁标记）。

        契约：POST {base}/v1/admin/care/signals/{event_id}/consume
        已被他人消费 → 409 ERR_CONFLICT（认领失败，跳过即可，不重试）。
        """
        cli = self._http()
        if cli is None or not self._probe():
            return json.dumps({"error": "SGME Gateway 不可达"}, ensure_ascii=False)
        event_id = str(args.get("event_id", "")).strip()
        if not event_id:
            return json.dumps({"error": "缺少 event_id"}, ensure_ascii=False)
        try:
            r = cli.post(
                f"{self.base_url}/v1/admin/care/signals/{event_id}/consume",
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code == 409:
                return json.dumps(
                    {"event_id": event_id, "claimed": False, "message": "该信号已被其他 agent 认领消费，跳过即可"},
                    ensure_ascii=False,
                )
            if r.status_code != 200:
                return json.dumps({"error": f"SGME 返回 {r.status_code}: {r.text[:150]}"}, ensure_ascii=False)
            data = r.json()
            if isinstance(data, dict):
                data.setdefault("event_id", event_id)
                data.setdefault("claimed", True)
            return json.dumps(data, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"认领信号失败: {e}"}, ensure_ascii=False)

    def _tool_signal_ack(self, args: Dict[str, Any]) -> str:
        """sgme_signal_ack：写关怀信号消费回执（认领后报告处理结果，供溯源）。

        契约：POST {base}/v1/admin/care/signals/{event_id}/ack
        body {"status": "acked", "result": "<摘要>"}；status 限 claimed/acked/failed。
        """
        cli = self._http()
        if cli is None or not self._probe():
            return json.dumps({"error": "SGME Gateway 不可达"}, ensure_ascii=False)
        event_id = str(args.get("event_id", "")).strip()
        if not event_id:
            return json.dumps({"error": "缺少 event_id"}, ensure_ascii=False)
        status = str(args.get("status", "acked")).strip() or "acked"
        if status not in ("claimed", "acked", "failed"):
            return json.dumps({"error": f"非法回执状态: {status}（限 claimed/acked/failed）"}, ensure_ascii=False)
        result = str(args.get("result", "")).strip()
        try:
            r = cli.post(
                f"{self.base_url}/v1/admin/care/signals/{event_id}/ack",
                json={"status": status, "result": result},
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                return json.dumps({"error": f"SGME 返回 {r.status_code}: {r.text[:150]}"}, ensure_ascii=False)
            data = r.json()
            if isinstance(data, dict):
                data.setdefault("event_id", event_id)
            return json.dumps(data, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"写回执失败: {e}"}, ensure_ascii=False)

    # ---------- 生命周期 ----------

    def shutdown(self) -> None:
        """关闭客户端。B152（2026-09-05）：加锁幂等——并发 close 同一 httpx client
        会触发套接字竞争（WinError 10038），关一次 + 置 None 即可。"""
        with self._client_lock:
            if self._client is None:
                return
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


def _load_plugin_config() -> dict:
    """读插件自身 plugin.yaml 的 config 段（不依赖任何 ctx 能力 / yaml 库，CLI/gateway 双环境都可用）。

    优先级：插件 manifest config > 环境变量（SGME_BASE_URL 等）> 代码默认值。
    用正则提取（瘦桥接原则，不引入 yaml 依赖）：只认 `  key: value` 形式的顶层键。
    """
    cfg: dict = {}
    try:
        yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.yaml")
        if not os.path.exists(yaml_path):
            return cfg
        with open(yaml_path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                # 仅顶层 config 段内的 `key: value`（两个空格缩进，value 不含冒号+空格）
                if not line.startswith("  ") or line.startswith("    "):
                    continue
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if not key or not val or val.startswith("#"):
                    continue
                # 剥离行内注释（` # ...`）与包裹引号
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()
                val = val.strip('"').strip("'").strip()
                if not val:
                    continue
                cfg[key] = val
    except Exception:
        pass
    return cfg


def register(ctx=None) -> "SGMEProvider":
    """Hermes 插件入口。

    兼容两种加载方式：
    - 插件式：ctx 是 _ProviderCollector，调 register_memory_provider 捕获
    - 直接式：无 ctx 时返回实例（MemoryProvider 子类扫描兜底）

    配置来源（2026-08-20 修复）：
    - 优先插件自身 plugin.yaml 的 config.base_url（不依赖 ctx.get_config——
      _ProviderCollector 只转发 register_*，get_config 会 AttributeError）
    - 再尝试 ctx.get_config()（真 PluginContext 场景）
    - 最后回退环境变量/默认值（SGME_BASE_URL 等）
    """
    # ① 插件 manifest config（最可靠，双环境一致）
    manifest_cfg = _load_plugin_config()
    base_url = manifest_cfg.get("base_url") or None
    inject_mode = manifest_cfg.get("inject_mode") or None

    # ② ctx.get_config() 覆盖（真 PluginContext 场景；_ProviderCollector 会 AttributeError）
    if ctx is not None and hasattr(ctx, "get_config"):
        try:
            base_url = ctx.get_config("base_url") or base_url
            inject_mode = ctx.get_config("inject_mode") or inject_mode
        except Exception:
            pass

    # ③ 环境变量优先（用户显式覆盖；密钥只从环境变量读，不落 config）
    base_url = os.environ.get("SGME_BASE_URL") or base_url
    agent_key = os.environ.get("SGME_AGENT_KEY") or None
    admin_key = os.environ.get("SGME_ADMIN_KEY") or None

    provider = SGMEProvider(
        base_url=base_url,
        agent_key=agent_key,
        admin_key=admin_key,
        inject_mode=inject_mode,
    )
    if ctx is not None and hasattr(ctx, "register_memory_provider"):
        ctx.register_memory_provider(provider)
        return provider
    return provider
