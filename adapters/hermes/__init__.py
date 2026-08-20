# -*- coding: utf-8 -*-
"""SGME MemoryProvider — 桥接 Hermes 与 SGME 记忆引擎 Gateway.

瘦桥接层：不碰 LLM、不碰数据库，纯 HTTP 调 SGME Gateway（默认 http://192.168.10.10:9910，NAS 部署；SGME_BASE_URL 可覆盖）。
重活（L1/L1.5/L2 提炼、向量、TTL）全在 SGME Gateway 侧。

接入方式（Hermes 原生 memory provider 槽位）：
  config.yaml → memory.provider: sgme

生命周期（MemoryProvider ABC）：
  - system_prompt_block(): SGME 画像摘要（Tier0，/v1/inject mode=full 精简）
  - prefetch(query): 每轮 LLM 前召回相关记忆（/v1/search，<100ms 预算）
  - sync_turn(): 每轮对话后写原始层（/v1/append，后台线程异步）
  - on_session_end(): 会话结束触发提炼（/v1/admin/refine/trigger）
  - get_tool_schemas()/handle_tool_call(): sgme_memory_search / sgme_conversation_search

故障隔离：SGME Gateway 不可达 → 静默降级（is_available=false / try-except），
绝不阻塞 Hermes 主流程。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

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
_DEFAULT_BASE_URL = os.environ.get("SGME_BASE_URL", "http://192.168.10.10:9910")
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
        """懒创建 httpx 客户端（trust_env=False 防 Clash 劫持 localhost）。"""
        if not _HAS_HTTPX:
            return None
        if self._client is None:
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

    # ---------- 工具（Agent 主动检索） ----------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """暴露 SGME 检索工具。"""
        return [
            {
                "name": "sgme_memory_search",
                "description": "检索 SGME 长期记忆（标签化记忆池，跨三层带溯源）。"
                               "用于查找用户的长期事实、偏好、项目历史、决策记录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词或问题"},
                        "limit": {"type": "integer", "description": "返回条数（默认 5，最大 20）"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "sgme_conversation_search",
                "description": "检索 SGME 原始会话全文（BM25）。用于查找历史对话原文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词"},
                        "limit": {"type": "integer", "description": "返回条数（默认 5）"},
                    },
                    "required": ["query"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """处理 sgme_* 工具调用 → /v1/search。"""
        if tool_name not in ("sgme_memory_search", "sgme_conversation_search"):
            return json.dumps({"error": f"未知工具 {tool_name}"}, ensure_ascii=False)
        cli = self._http()
        if cli is None or not self._probe():
            return json.dumps({"error": "SGME Gateway 不可达"}, ensure_ascii=False)
        query = str(args.get("query", ""))[:200]
        limit = min(int(args.get("limit", 5)), 20)
        try:
            r = cli.post(
                f"{self.base_url}/v1/search",
                json={"query": query, "scopes": ["memory"], "limit": limit},
                headers={"X-API-Key": self.agent_key},
            )
            if r.status_code != 200:
                return json.dumps({"error": f"SGME 返回 {r.status_code}"}, ensure_ascii=False)
            data = r.json()
            results = data.get("results", [])
            return json.dumps(results[:limit], ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ---------- 生命周期 ----------

    def shutdown(self) -> None:
        if self._client is not None:
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
