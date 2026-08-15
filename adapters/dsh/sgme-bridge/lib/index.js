import Schema from "schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
//#region src/sgme-client.ts
/**
* SGME HTTP 客户端。
*
* 防代理劫持：fetch 不读 HTTP_PROXY 环境变量（防 Clash 劫持 localhost），
* 用显式 127.0.0.1（由 baseUrl 配置保证）+ dispatcher 禁用代理。
*
* 故障隔离：所有方法失败返回 null，绝不抛异常（调用方按 null 判断降级）。
*/
var SgmeClient = class {
	baseUrl;
	agentKey;
	adminKey;
	agentId;
	timeoutMs;
	constructor(config) {
		this.baseUrl = config.baseUrl.replace(/\/+$/, "");
		this.agentKey = config.agentKey;
		this.adminKey = config.adminKey;
		this.agentId = config.agentId;
		this.timeoutMs = config.timeoutMs ?? 5e3;
	}
	/** 统一 POST 请求，返回 [data, error]。失败时 data=null。 */
	async post(path, body, keyType) {
		const key = keyType === "agent" ? this.agentKey : this.adminKey;
		const url = `${this.baseUrl}${path}`;
		try {
			const ctrl = new AbortController();
			const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
			const resp = await fetch(url, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-API-Key": key
				},
				body: JSON.stringify(body),
				signal: ctrl.signal
			});
			clearTimeout(timer);
			if (!resp.ok) {
				const text = await resp.text().catch(() => "");
				return [null, `HTTP ${resp.status}: ${text.slice(0, 200)}`];
			}
			return [await resp.json(), null];
		} catch (e) {
			return [null, `fetch error: ${e instanceof Error ? e.message : String(e)}`];
		}
	}
	/** 记忆+wiki 检索（POST /v1/search，Agent Key）。失败返回 null。 */
	async search(req) {
		const [data, err] = await this.post("/v1/search", req, "agent");
		if (err) {
			console.warn(`[sgme-bridge] search failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 画像注入（POST /v1/inject，Agent Key）。失败返回 null。 */
	async inject(req) {
		const [data, err] = await this.post("/v1/inject", req, "agent");
		if (err) {
			console.warn(`[sgme-bridge] inject failed: ${err}`);
			return null;
		}
		return data;
	}
	/** L0 写入（POST /v1/append，Agent Key）。失败返回 null。 */
	async append(req) {
		const [data, err] = await this.post("/v1/append", req, "agent");
		if (err) {
			console.warn(`[sgme-bridge] append failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 触发批量提炼（POST /v1/admin/refine/trigger_async，Admin Key）。
	* 实际返回 200（非 202），兼容两种状态码。失败返回 null。
	*/
	async triggerRefine(req) {
		const [data, err] = await this.post("/v1/admin/refine/trigger_async", req, "admin");
		if (err) {
			console.warn(`[sgme-bridge] triggerRefine failed: ${err}`);
			return null;
		}
		return data;
	}
	/** GET 请求（信号拉取用，与 POST 并列；同样防代理 + 故障隔离）。 */
	async get(path, keyType) {
		const key = keyType === "agent" ? this.agentKey : this.adminKey;
		const url = `${this.baseUrl}${path}`;
		try {
			const ctrl = new AbortController();
			const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
			const resp = await fetch(url, {
				method: "GET",
				headers: { "X-API-Key": key },
				signal: ctrl.signal
			});
			clearTimeout(timer);
			if (!resp.ok) {
				const text = await resp.text().catch(() => "");
				return [null, `HTTP ${resp.status}: ${text.slice(0, 200)}`];
			}
			return [await resp.json(), null];
		} catch (e) {
			return [null, `fetch error: ${e instanceof Error ? e.message : String(e)}`];
		}
	}
	/** 拉取未消费关怀信号（GET /v1/admin/care/signals?unconsumed_only=true）。失败返回 null。 */
	async pullCareSignals(signalType, limit = 20) {
		const params = new URLSearchParams({
			unconsumed_only: "true",
			limit: String(limit)
		});
		if (signalType) params.set("signal_type", signalType);
		const [data, err] = await this.get(`/v1/admin/care/signals?${params.toString()}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] pullCareSignals failed: ${err}`);
			return null;
		}
		return data?.signals ?? null;
	}
	/**
	* 原子认领信号（POST /v1/admin/care/signals/{id}/consume）。
	* 返回 true=本次认领成功 / false=已被他人消费（409）或失败 / null=网关不可达。
	*/
	async claimSignal(eventId) {
		const [data, err] = await this.post(`/v1/admin/care/signals/${eventId}/consume`, {}, "agent");
		if (err) {
			if (err.startsWith("HTTP 409")) return false;
			console.warn(`[sgme-bridge] claimSignal failed: ${err}`);
			return null;
		}
		return data?.status === "consumed";
	}
	/** 写消费回执（POST /v1/admin/care/signals/{id}/ack）。返回是否写入成功。 */
	async ackSignal(eventId, status, result) {
		const [data, err] = await this.post(`/v1/admin/care/signals/${eventId}/ack`, {
			status,
			result
		}, "agent");
		if (err) {
			console.warn(`[sgme-bridge] ackSignal failed: ${err}`);
			return false;
		}
		return data?.status === status;
	}
};
/**
* 消息列表 → SGME L0 消息块文本。
*
* 格式（与 reasonix bridge.py to_l0 完全一致，对齐 sgme/raw/store.py parse_body_messages）：
* - user：`# {ts} user\n{content}`
* - assistant：`## {ts} assistant\n{content}`
* - tool：`## {ts} tool\n**tool**: {name}\n{content}`
*/
function toL0(messages) {
	const blocks = [];
	for (const m of messages) if (m.role === "user") blocks.push(`# ${m.ts} user\n${m.content}`);
	else if (m.role === "tool") blocks.push(`## ${m.ts} tool\n**tool**: ${m.toolName ?? "tool"}\n${m.content}`);
	else blocks.push(`## ${m.ts} assistant\n${m.content}`);
	return blocks.join("\n\n") + "\n";
}
//#endregion
//#region src/tools.ts
/**
* tools.ts — memory_search / wiki_search 工具注册
*
* 把 SGME 检索能力暴露为 dsh 工具，模型可按需调用查询记忆/知识库。
*
* 契约对齐：POST /v1/search（Agent Key）
* - memory_search：scopes=["memory"]
* - wiki_search：scopes=["wiki","wiki_pages"]
*
* dsh 工具规范（2026-08-14 T-53 本地加载确认，对齐 @deepseek-ai/dsh-tools 官方文档）：
* - 使用 defineTool() helper 生成 ToolDefinition（参数类型自动推导）
* - parameters 用扁平映射 { name: { type, required?, description?, enum? } }
* - execute(args, exec) — exec 含 signal，协作式取消
* - output { schema, render(args, value) } — schema 是 ValueSchemaSpec，render 把 value 转 ContentBlock[]
*/
/**
* 创建 memory_search 工具（检索 L1.5 记忆池）。
*
* 模型调用此工具查询用户/项目的长期记忆，例如"用户之前提过什么相关需求"。
*/
function createMemorySearchTool(client, defaultLimit) {
	return defineTool({
		name: "memory_search",
		description: [
			"检索 SGME 长期记忆池（L1.5 标签化记忆）。",
			"用于查询用户/项目的历史事实、偏好、决策——当问题涉及\"之前/以前/上次/还记得\"时必用。",
			"查询不到时返回空，应如实告知\"记忆库中未找到\"。"
		].join(" "),
		parameters: {
			query: {
				type: "string",
				required: true,
				description: "检索关键词或自然语言问题"
			},
			limit: {
				type: "number",
				description: `返回条数上限（默认 ${defaultLimit}）`
			},
			dimensions: {
				type: "array",
				description: "维度过滤（注册表 id，如 identity/projects/status/focus/tasks/goals/ideas）"
			},
			match: {
				type: "string",
				enum: ["any", "all"],
				description: "维度匹配语义：any=任一命中，all=全部命中（默认 any）"
			}
		},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(args, _exec) {
			const a = args;
			const resp = await client.search({
				query: a.query,
				scopes: ["memory"],
				limit: a.limit ?? defaultLimit,
				dimensions: a.dimensions ?? null,
				match: a.match ?? "any"
			});
			if (!resp) return "[memory_search 失败：SGME Gateway 不可达，稍后重试]";
			if (resp.results.length === 0) return `[memory_search 无结果：query="${a.query}"]`;
			return formatSearchResults(resp.results);
		}
	});
}
/**
* 创建 wiki_search 工具（检索 L2 知识库）。
*
* 差异化能力：dsh-mnemon 只有记忆检索，SGME 额外提供场景化知识库。
*/
function createWikiSearchTool(client, defaultLimit) {
	return defineTool({
		name: "wiki_search",
		description: [
			"检索 SGME 知识库（L2 场景 + wiki_pages）。",
			"用于查询已经过 L1.5 冲突提炼的结构化场景知识，比记忆池更精炼。",
			"与 memory_search 互补：memory 是原始记忆，wiki 是提炼后的场景。"
		].join(" "),
		parameters: {
			query: {
				type: "string",
				required: true,
				description: "检索关键词或自然语言问题"
			},
			limit: {
				type: "number",
				description: `返回条数上限（默认 ${defaultLimit}）`
			}
		},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(args, _exec) {
			const a = args;
			const resp = await client.search({
				query: a.query,
				scopes: ["wiki", "wiki_pages"],
				limit: a.limit ?? defaultLimit
			});
			if (!resp) return "[wiki_search 失败：SGME Gateway 不可达，稍后重试]";
			if (resp.results.length === 0) return `[wiki_search 无结果：query="${a.query}"]`;
			return formatSearchResults(resp.results);
		}
	});
}
/**
* 格式化检索结果为模型可读文本。
*
* 格式（对齐 reasonix fetch_search 输出）：
* ```
* ## 1. [memory] 内容摘要...
*    routes: bm25, vector
* ```
*/
function formatSearchResults(results) {
	const lines = [];
	for (const r of results) {
		const titlePrefix = r.title ? `「${r.title}」` : "";
		const routes = r.routes && r.routes.length > 0 ? ` [${r.routes.join(",")}]` : "";
		const content = r.content.length > 500 ? r.content.slice(0, 500) + "…" : r.content;
		lines.push(`## ${r.rank}. [${r.source}]${titlePrefix}${routes}\n${content}`);
	}
	return lines.join("\n\n");
}
/**
* 向 dsh ctx 注册全部工具（检索 + 信号消费）。
*
* 调用方：index.ts apply() 内调用，传入 ctx 和 client。
*/
function registerTools(ctx, client, defaultLimit) {
	ctx.tools.register(createMemorySearchTool(client, defaultLimit));
	ctx.tools.register(createWikiSearchTool(client, defaultLimit));
	ctx.tools.register(createSignalPullTool(client));
	ctx.tools.register(createSignalClaimTool(client));
	ctx.tools.register(createSignalAckTool(client));
}
/** 创建 signal_pull 工具（拉取未消费关怀信号）。 */
function createSignalPullTool(client) {
	return defineTool({
		name: "signal_pull",
		description: [
			"拉取 SGME 未消费的关怀信号（care_todo_due 待办到期 / care_mood 情绪低落 / care_overwork 过劳 / care_daily 每日问候）。",
			"会话开始主动消费：拉取后决定是否主动关怀用户。",
			"信号消费=主动关怀，谁消费谁标记：先 signal_claim 原子认领，处理完 signal_ack 写回执。"
		].join(" "),
		parameters: {
			signal_type: {
				type: "string",
				description: "可选过滤：care_todo_due/care_mood/care_overwork/care_daily；不传拉全部"
			},
			limit: {
				type: "number",
				description: "返回条数上限（默认 20）"
			}
		},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(args, _exec) {
			const a = args;
			const signals = await client.pullCareSignals(a.signal_type ?? null, a.limit ?? 20);
			if (signals === null) return "[signal_pull 失败：SGME Gateway 不可达，稍后重试]";
			if (signals.length === 0) return "[signal_pull 无未消费关怀信号]";
			return signals.map((s) => {
				let payload = s.payload;
				try {
					payload = JSON.parse(s.payload);
				} catch {}
				return `## ${s.type}（${s.ts}）\nevent_id=${s.event_id}\n${JSON.stringify(payload)}`;
			}).join("\n\n");
		}
	});
}
/** 创建 signal_claim 工具（原子认领信号）。 */
function createSignalClaimTool(client) {
	return defineTool({
		name: "signal_claim",
		description: [
			"原子认领一条关怀信号（谁消费谁标记，防多 agent 重复关怀）。",
			"认领成功后应主动关怀用户，然后调 signal_ack 写回执。",
			"返回 claimed=false 说明已被其他 agent 消费，跳过即可。"
		].join(" "),
		parameters: { event_id: {
			type: "string",
			required: true,
			description: "信号 event_id（signal_pull 返回）"
		} },
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(args, _exec) {
			const a = args;
			const claimed = await client.claimSignal(a.event_id);
			if (claimed === null) return "[signal_claim 失败：SGME Gateway 不可达，稍后重试]";
			return claimed ? `[signal_claim 认领成功：event_id=${a.event_id}，请主动关怀用户后调 signal_ack 回执]` : `[signal_claim 已被消费：event_id=${a.event_id}，跳过]`;
		}
	});
}
/** 创建 signal_ack 工具（写消费回执）。 */
function createSignalAckTool(client) {
	return defineTool({
		name: "signal_ack",
		description: ["写信号消费回执（claimed/acked/failed）。", "认领（signal_claim）并处理完信号后调用，报告处理结果（如「已转告用户」「检查正常」）。"].join(" "),
		parameters: {
			event_id: {
				type: "string",
				required: true,
				description: "信号 event_id"
			},
			status: {
				type: "string",
				required: true,
				enum: [
					"claimed",
					"acked",
					"failed"
				],
				description: "回执状态"
			},
			result: {
				type: "string",
				description: "处理结果摘要"
			}
		},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(args, _exec) {
			const a = args;
			return await client.ackSignal(a.event_id, a.status, a.result) ? `[signal_ack 已回执：event_id=${a.event_id} status=${a.status}]` : "[signal_ack 失败]";
		}
	});
}
//#endregion
//#region src/context.ts
/**
* 注册画像首步注入。
*
* 实现方式：监听 agent 事件（首步触发），拉取 SGME 画像 → 拼接为指令文本 → 返回给 dsh。
*
* 注意：dsh 的 agent.inject(message) API 在 v0.1 不稳定，本实现先用返回值方式
* （通过 agent/pre-step 事件返回注入内容），T-53 本地加载时确认实际注入路径。
*
* @returns 清理函数（由 ctx.effect 调用方管理生命周期）
*/
function registerContextInjection(ctx, client, config) {
	let injected = false;
	const handler = (...args) => {
		let event;
		for (const a of args) if (typeof a === "object" && a !== null && "type" in a) {
			event = a;
			break;
		}
		if (event?.type !== "turn/start") return;
		if (injected) return;
		injected = true;
		(async () => {
			try {
				const [profile, related] = await Promise.all([client.inject({ mode: config.injectMode }), config.projectHint ? client.search({
					query: config.projectHint,
					scopes: ["memory"],
					limit: config.searchLimit
				}) : Promise.resolve(null)]);
				const injectionText = buildInjectionText(profile, related);
				if (injectionText) ctx.logger.info(`[SGME 画像注入]\n${injectionText}`);
			} catch (e) {
				ctx.logger.warn(`[SGME 画像注入失败] ${e instanceof Error ? e.message : String(e)}`);
			}
		})();
	};
	return ctx.on("session/event", handler);
}
/**
* 拼接画像注入文本（模型可读格式）。
*
* 格式（对齐 reasonix cmd_start 注入）：
* ```
* --- SGME 用户画像 ---
* [Tier0 摘要]
* ...
* [记忆区块 1: identity]
* - 记忆内容...
* --- 相关记忆 ---
* 1. 内容...
* ```
*/
function buildInjectionText(profile, related) {
	const hasTier0 = profile?.tier0.present && profile.tier0.content;
	if (!profile || profile.blocks.length === 0 && !hasTier0) {
		if (related && related.results.length > 0) return formatRelatedMemories(related.results);
		return "";
	}
	const parts = ["--- SGME 用户画像 ---"];
	if (profile.tier0.present && profile.tier0.content) parts.push("[Tier0 摘要]", profile.tier0.content);
	for (const block of profile.blocks) {
		if (block.items.length === 0) continue;
		parts.push(`[${block.title}]`);
		for (const item of block.items) {
			const content = item.content ?? JSON.stringify(item);
			const truncated = content.length > 200 ? content.slice(0, 200) + "…" : content;
			parts.push(`- ${truncated}`);
		}
	}
	if (related && related.results.length > 0) {
		parts.push("--- 相关记忆 ---");
		parts.push(formatRelatedMemories(related.results));
	}
	parts.push("（以上为 SGME 注入的画像与记忆，可直接引用，不必重复询问用户）");
	return parts.join("\n");
}
/** 格式化相关记忆列表。 */
function formatRelatedMemories(results) {
	return results.map((r) => {
		const truncated = r.content.length > 200 ? r.content.slice(0, 200) + "…" : r.content;
		return `${r.rank}. ${truncated}`;
	}).join("\n");
}
//#endregion
//#region src/commands.ts
/**
* 执行 /sgme 检索，返回 dsh CommandResult。
*
* @param query 用户输入的检索关键词（/sgme 后的参数）
*/
async function executeSgmeCommand(client, config, query) {
	const trimmed = query.trim();
	if (!trimmed) return {
		kind: "error",
		text: [
			"用法：/sgme <关键词>",
			"",
			"检索 SGME 记忆池 + 知识库，查询用户/项目的历史事实与场景知识。",
			"示例：/sgme 之前提过的项目"
		].join("\n")
	};
	const resp = await client.search({
		query: trimmed,
		scopes: [
			"memory",
			"wiki",
			"wiki_pages"
		],
		limit: config.searchLimit
	});
	if (!resp) return {
		kind: "error",
		text: "[/sgme 失败：SGME Gateway 不可达，请检查服务是否运行]"
	};
	if (resp.results.length === 0) return {
		kind: "success",
		text: `[/sgme 无结果：query="${trimmed}"]\n\n记忆库中未找到相关内容。`
	};
	const lines = [`[/sgme 检索结果：query="${trimmed}"]`, ""];
	for (const r of resp.results) {
		const titlePrefix = r.title ? `「${r.title}」` : "";
		const routes = r.routes && r.routes.length > 0 ? ` [${r.routes.join(",")}]` : "";
		const content = r.content.length > 500 ? r.content.slice(0, 500) + "…" : r.content;
		lines.push(`## ${r.rank}. [${r.source}]${titlePrefix}${routes}`);
		lines.push(content);
		lines.push("");
	}
	return {
		kind: "success",
		text: lines.join("\n")
	};
}
/**
* 向 dsh ctx 注册 /sgme 命令（对齐 dsh-commands 官方 register 签名）。
*
* ctx.commands.register(definition) 单参数，返回 disposer。
*/
function registerSgmeCommand(ctx, client, config) {
	ctx.commands.register({
		name: "sgme",
		description: "检索 SGME 记忆 + 知识库（query 为关键词）",
		async handler(invocation) {
			return executeSgmeCommand(client, config, invocation.rawInput);
		}
	});
}
//#endregion
//#region src/session-sync.ts
/**
* 注册 turn/end 会话同步（v1.1 累积式）。
*
* @returns 清理函数（由调用方通过 ctx.effect 管理生命周期）
*/
function registerSessionSync(ctx, client, config) {
	if (!config.syncOnTurnEnd) {
		ctx.logger.info("[SGME session-sync] 已禁用（syncOnTurnEnd=false）");
		return () => {};
	}
	let currentTurnMessages = [];
	let currentTurnStartMs;
	let currentTurnId;
	let sessionKey;
	/** 从事件 args 中提取 event 对象（兼容 (event) / (session, event) 形态）。 */
	function pickEvent(args) {
		for (const a of args) if (typeof a === "object" && a !== null && "type" in a) return a;
	}
	/** 毫秒时间戳 → ISO 8601 字符串。 */
	function msToIso(ms) {
		if (typeof ms === "number" && Number.isFinite(ms)) return new Date(ms).toISOString();
	}
	/** 从 user/message 事件提取消息并推入 buffer。 */
	function handleUserMessage(event) {
		const contentArr = (event.data ?? {}).content;
		if (!Array.isArray(contentArr)) return;
		const text = contentArr.map((c) => typeof c === "object" && c !== null ? c.text : null).filter((t) => typeof t === "string").join("\n");
		if (!text.trim()) return;
		const ts = msToIso(event.time);
		if (!sessionKey && ts) sessionKey = String(event.time);
		currentTurnMessages.push({
			role: "user",
			content: text,
			ts: ts ?? (/* @__PURE__ */ new Date()).toISOString()
		});
	}
	/** 从 assistant/message 事件提取文本消息并推入 buffer（忽略 reasoning / tool-call 块）。 */
	function handleAssistantMessage(event) {
		const message = (event.data ?? {}).message;
		if (!message) return;
		const contentArr = message.content;
		if (!Array.isArray(contentArr)) return;
		const text = contentArr.filter((c) => {
			if (typeof c !== "object" || c === null) return false;
			return c.type === "text";
		}).map((c) => c.text).filter((t) => typeof t === "string").join("\n");
		if (!text.trim()) return;
		currentTurnMessages.push({
			role: "assistant",
			content: text,
			ts: msToIso(event.time) ?? (/* @__PURE__ */ new Date()).toISOString()
		});
	}
	/** 从 tool/result 事件提取工具结果文本并推入 buffer。 */
	function handleToolResult(event) {
		const message = (event.data ?? {}).message;
		if (!message) return;
		const contentArr = message.content;
		if (!Array.isArray(contentArr)) return;
		for (const c of contentArr) {
			if (typeof c !== "object" || c === null) continue;
			const item = c;
			if (item.type !== "tool-result") continue;
			const inner = item.content;
			if (!Array.isArray(inner)) continue;
			const text = inner.map((t) => typeof t === "object" && t !== null ? t.text : null).filter((t) => typeof t === "string").join("\n");
			if (!text.trim()) continue;
			currentTurnMessages.push({
				role: "tool",
				content: text,
				toolName: "tool",
				ts: msToIso(event.time) ?? (/* @__PURE__ */ new Date()).toISOString()
			});
		}
	}
	/** turn/start：记录 turn 起始时间，清空 buffer 准备新 turn。 */
	function handleTurnStart(event) {
		const data = event.data ?? {};
		currentTurnId = typeof data.turn === "number" ? data.turn : void 0;
		currentTurnStartMs = typeof event.time === "number" ? event.time : void 0;
		currentTurnMessages = [];
	}
	/** turn/end：打包 buffer → append → 触发提炼。 */
	function handleTurnEnd(event) {
		const data = event.data ?? {};
		const turnId = typeof data.turn === "number" ? data.turn : currentTurnId;
		const endedAt = msToIso(event.time);
		const startedAt = msToIso(currentTurnStartMs) ?? currentTurnMessages[0]?.ts ?? (/* @__PURE__ */ new Date()).toISOString();
		const payload = { messages: currentTurnMessages.map((m) => ({
			role: m.role,
			content: m.content,
			ts: m.ts,
			...m.toolName !== void 0 ? { toolName: m.toolName } : {}
		})) };
		if (sessionKey) payload.sessionId = sessionKey;
		if (turnId !== void 0) payload.turnId = turnId;
		if (startedAt) payload.startedAt = startedAt;
		if (endedAt) payload.endedAt = endedAt;
		if (currentTurnMessages.length === 0) {
			ctx.logger.info(`[SGME session-sync] turn ${turnId ?? "?"} 无有效消息，跳过`);
			return;
		}
		syncTurnToSgme(ctx, client, config, payload);
		currentTurnMessages = [];
	}
	const handler = (...args) => {
		const event = pickEvent(args);
		if (!event?.type) return;
		switch (event.type) {
			case "turn/start":
				handleTurnStart(event);
				return;
			case "user/message":
				handleUserMessage(event);
				return;
			case "assistant/message":
				handleAssistantMessage(event);
				return;
			case "tool/result":
				handleToolResult(event);
				return;
			case "turn/end":
				handleTurnEnd(event);
				return;
			default: return;
		}
	};
	const dispose = ctx.on("session/event", handler);
	ctx.logger.info("[SGME session-sync] 已注册 v1.1 累积式同步监听（user/assistant/tool/turn）");
	return dispose;
}
/**
* 同步单个 turn 到 SGME。
*
* 1. 收集本 turn 消息 → 转 L0 格式
* 2. POST /v1/append（session_key=dsh-{sessionId}，started_at=turn 起始时间）
* 3. POST /v1/admin/refine/trigger_async（fire-and-forget，失败只 log）
*/
async function syncTurnToSgme(ctx, client, config, payload) {
	try {
		const messages = extractMessages(payload);
		if (messages.length === 0) {
			ctx.logger.info("[SGME session-sync] turn 无有效消息，跳过");
			return;
		}
		const l0Text = toL0(messages);
		const sessionKey = `dsh-${payload.sessionId ?? "unknown"}`;
		const startedAt = payload.startedAt ?? messages[0].ts;
		const appendResp = await client.append({
			session_key: sessionKey,
			started_at: startedAt,
			content: l0Text,
			agent_id: config.agentId,
			...payload.endedAt ? { ended_at: payload.endedAt } : {}
		});
		if (!appendResp) {
			ctx.logger.warn(`[SGME session-sync] append 失败：session=${sessionKey}`);
			return;
		}
		ctx.logger.info(`[SGME session-sync] append 成功：session=${sessionKey} status=${appendResp.status}` + (appendResp.idempotent ? " (幂等命中)" : "") + (appendResp.appended ? " (追加段)" : ""));
		const refineResp = await client.triggerRefine({ limit: 50 });
		if (!refineResp) ctx.logger.warn("[SGME session-sync] 提炼触发失败（数据已在 L0 等待，可稍后手动触发）");
		else ctx.logger.info(`[SGME session-sync] 提炼已触发：${refineResp.file_id} ${refineResp.status}`);
	} catch (e) {
		ctx.logger.warn(`[SGME session-sync] 同步异常：${e instanceof Error ? e.message : String(e)}`);
	}
}
/**
* 从 payload 提取消息列表，过滤 system 消息 + 空内容。
*/
function extractMessages(payload) {
	if (!payload.messages) return [];
	const messages = [];
	for (const m of payload.messages) {
		if (m.role === "system") continue;
		if (!m.content || !m.content.trim()) continue;
		const msg = {
			role: normalizeRole(m.role),
			content: m.content,
			ts: m.ts ?? (/* @__PURE__ */ new Date()).toISOString()
		};
		if (m.toolName !== void 0) msg.toolName = m.toolName;
		messages.push(msg);
	}
	return messages;
}
/** 角色归一化（dsh 可能用 'tool_result' 等变体，统一到 L0 格式）。 */
function normalizeRole(role) {
	if (role === "user") return "user";
	if (role === "assistant") return "assistant";
	return "tool";
}
//#endregion
//#region src/index.ts
/**
* dsh-sgme — SGME 记忆引擎 × DeepSeek Harness 桥接插件
*
* 把 SGME 的多 Agent 共享长期记忆能力接入 dsh：
* - 画像 + 相关记忆首步注入（agent/pre-step 拦截）
* - memory_search / wiki_search 工具
* - /sgme 综合检索命令
* - 每轮对话结束自动入库（session/event turn/end → /v1/append + 触发提炼）
*
* 运行时零 Python 依赖，全部通过 HTTP 调 SGME Gateway。
*
* 契约来源：sgme/server/routes_memory.py / routes_admin.py（2026-08-14 调研确认）
*/
const name = "dsh-sgme";
const inject = ["tools", "commands"];
const Config = Schema.object({
	baseUrl: Schema.string().default("http://192.168.10.10:9910").description("SGME Gateway 地址"),
	agentKey: Schema.string().default("").description("SGME agent key（/v1/admin/agents/register 签发）"),
	adminKey: Schema.string().default("").description("SGME admin key（触发提炼用）"),
	agentId: Schema.string().default("dsh").description("SGME agent id"),
	injectMode: Schema.union([
		"daily",
		"full",
		"coding",
		"work"
	]).default("daily").description("画像注入模式"),
	injectMaxTokens: Schema.number().default(800).description("画像注入 token 上限"),
	searchLimit: Schema.number().default(5).description("检索返回条数上限"),
	syncOnTurnEnd: Schema.boolean().default(true).description("是否在 turn/end 时同步入库"),
	turnBatchSize: Schema.number().default(1).description("入库攒批大小（v1=1 即每 turn 即 append）")
});
/**
* 插件入口（Cordis apply）。
*
* 拼装 5 类能力：
* 1. sgmeClient — HTTP 客户端（其他能力共享）
* 2. tools — memory_search + wiki_search 工具注册
* 3. context — 画像首步注入（agent/pre-step 拦截）
* 4. commands — /sgme 综合检索命令
* 5. sessionSync — turn/end 会话入库
*
* 故障隔离：所有 SGME 调用失败只 log，绝不阻塞 dsh 主循环。
*/
function apply(ctx, config) {
	const logger = ctx.logger("sgme-bridge");
	logger.info(`SGME bridge loaded: baseUrl=${config.baseUrl} agentId=${config.agentId} mode=${config.injectMode}`);
	const client = new SgmeClient({
		baseUrl: config.baseUrl,
		agentKey: config.agentKey,
		adminKey: config.adminKey,
		agentId: config.agentId
	});
	registerTools({ tools: ctx.tools }, client, config.searchLimit);
	logger.info("工具已注册：memory_search, wiki_search");
	const disposeContext = registerContextInjection({
		on: ctx.on,
		logger: {
			info: logger.info,
			warn: logger.warn
		}
	}, client, {
		injectMode: config.injectMode,
		injectMaxTokens: config.injectMaxTokens,
		searchLimit: config.searchLimit
	});
	ctx.effect(() => disposeContext, "sgme-context-injection");
	registerSgmeCommand({ commands: ctx.commands }, client, { searchLimit: config.searchLimit });
	logger.info("命令已注册：/sgme");
	const disposeSync = registerSessionSync({
		on: ctx.on,
		logger: {
			info: logger.info,
			warn: logger.warn
		}
	}, client, {
		agentId: config.agentId,
		syncOnTurnEnd: config.syncOnTurnEnd,
		turnBatchSize: config.turnBatchSize
	});
	ctx.effect(() => disposeSync, "sgme-session-sync");
	logger.info("SGME bridge 全部能力已注册（画像注入 + 工具 + 命令 + 会话同步）");
}
//#endregion
export { Config, apply, inject, name };
