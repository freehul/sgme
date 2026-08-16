import { createRequire } from "node:module";
import Schema from "schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import "@deepseek-ai/cordis";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
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
//#region node_modules/.pnpm/@deepseek-ai+cosmokit@1.8.2/node_modules/@deepseek-ai/cosmokit/lib/index.js
/** Return true when a value is `null` or `undefined`. */
function isNullable(value) {
	return value === null || value === void 0;
}
/** Return true for non-array object values. */
function isPlainObject(data) {
	return data && typeof data === "object" && !Array.isArray(data);
}
/** Filter object entries and return a new object. */
function filterKeys(object, filter) {
	return Object.fromEntries(Object.entries(object).filter(([key, value]) => filter(key, value)));
}
/** Map object values while preserving the original key set. */
function mapValues(object, transform) {
	return Object.fromEntries(Object.entries(object).map(([key, value]) => [key, transform(value, key)]));
}
/** Pick selected keys from an object, optionally including `undefined` values. */
function pick(source, keys, forced) {
	if (!keys) return { ...source };
	const result = {};
	for (const key of keys) if (forced || source[key] !== void 0) result[key] = source[key];
	return result;
}
/** Test values using `instanceof` with a `toStringTag` fallback. */
function is(type, value) {
	if (arguments.length === 1) return (value) => is(type, value);
	return type in globalThis && value instanceof globalThis[type] || Object.prototype.toString.call(value).slice(8, -1) === type;
}
function isArrayBufferLike(value) {
	return is("ArrayBuffer", value) || is("SharedArrayBuffer", value);
}
function isArrayBufferSource(value) {
	return isArrayBufferLike(value) || ArrayBuffer.isView(value);
}
/** Binary source detection and base64/hex conversion helpers. */
var Binary;
(function(Binary) {
	Binary.is = isArrayBufferLike;
	Binary.isSource = isArrayBufferSource;
	function fromSource(source) {
		if (ArrayBuffer.isView(source)) return source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength);
		else return source;
	}
	Binary.fromSource = fromSource;
	function toBase64(source) {
		source = fromSource(source);
		if (typeof Buffer !== "undefined") return Buffer.from(source).toString("base64");
		let binary = "";
		const bytes = new Uint8Array(source);
		for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
		return btoa(binary);
	}
	Binary.toBase64 = toBase64;
	function fromBase64(source) {
		if (typeof Buffer !== "undefined") return fromSource(Buffer.from(source, "base64"));
		return Uint8Array.from(atob(source), (c) => c.charCodeAt(0));
	}
	Binary.fromBase64 = fromBase64;
	function toHex(source) {
		source = fromSource(source);
		if (typeof Buffer !== "undefined") return Buffer.from(source).toString("hex");
		return Array.from(new Uint8Array(source), (byte) => byte.toString(16).padStart(2, "0")).join("");
	}
	Binary.toHex = toHex;
	function fromHex(source) {
		if (typeof Buffer !== "undefined") return fromSource(Buffer.from(source, "hex"));
		const hex = source.length % 2 === 0 ? source : source.slice(0, source.length - 1);
		const buffer = [];
		for (let i = 0; i < hex.length; i += 2) buffer.push(parseInt(`${hex[i]}${hex[i + 1]}`, 16));
		return Uint8Array.from(buffer).buffer;
	}
	Binary.fromHex = fromHex;
})(Binary || (Binary = {}));
Binary.fromBase64;
Binary.toBase64;
Binary.fromHex;
Binary.toHex;
/** Deep-clone common JavaScript values while preserving prototypes and cycles. */
function clone(source, refs = /* @__PURE__ */ new Map()) {
	if (!source || typeof source !== "object") return source;
	if (is("Date", source)) return new Date(source.valueOf());
	if (is("RegExp", source)) return new RegExp(source.source, source.flags);
	if (isArrayBufferLike(source)) return source.slice(0);
	if (ArrayBuffer.isView(source)) return source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength);
	const cached = refs.get(source);
	if (cached) return cached;
	if (Array.isArray(source)) {
		const result = [];
		refs.set(source, result);
		source.forEach((value, index) => {
			result[index] = Reflect.apply(clone, null, [value, refs]);
		});
		return result;
	}
	const result = Object.create(Object.getPrototypeOf(source));
	refs.set(source, result);
	for (const key of Reflect.ownKeys(source)) {
		const descriptor = { ...Reflect.getOwnPropertyDescriptor(source, key) };
		if ("value" in descriptor) descriptor.value = Reflect.apply(clone, null, [descriptor.value, refs]);
		Reflect.defineProperty(result, key, descriptor);
	}
	return result;
}
/** Deeply compare arrays, dates, regexps, buffers, and plain object fields. */
function deepEqual(a, b, strict) {
	if (a === b) return true;
	if (!strict && isNullable(a) && isNullable(b)) return true;
	if (typeof a !== typeof b) return false;
	if (typeof a !== "object") return false;
	if (!a || !b) return false;
	function check(test, then) {
		return test(a) ? test(b) ? then(a, b) : false : test(b) ? false : void 0;
	}
	return check(Array.isArray, (a, b) => a.length === b.length && a.every((item, index) => deepEqual(item, b[index]))) ?? check(is("Date"), (a, b) => a.valueOf() === b.valueOf()) ?? check(is("RegExp"), (a, b) => a.source === b.source && a.flags === b.flags) ?? check(isArrayBufferLike, (a, b) => {
		if (a.byteLength !== b.byteLength) return false;
		const viewA = new Uint8Array(a);
		const viewB = new Uint8Array(b);
		for (let i = 0; i < viewA.length; i++) if (viewA[i] !== viewB[i]) return false;
		return true;
	}) ?? Object.keys({
		...a,
		...b
	}).every((key) => deepEqual(a[key], b[key], strict));
}
/** Time constants plus parsing and formatting helpers. */
var Time;
(function(Time) {
	Time.millisecond = 1;
	Time.second = 1e3;
	Time.minute = Time.second * 60;
	Time.hour = Time.minute * 60;
	Time.day = Time.hour * 24;
	Time.week = Time.day * 7;
	let timezoneOffset = (/* @__PURE__ */ new Date()).getTimezoneOffset();
	function setTimezoneOffset(offset) {
		timezoneOffset = offset;
	}
	Time.setTimezoneOffset = setTimezoneOffset;
	function getTimezoneOffset() {
		return timezoneOffset;
	}
	Time.getTimezoneOffset = getTimezoneOffset;
	function getDateNumber(date = /* @__PURE__ */ new Date(), offset) {
		if (typeof date === "number") date = new Date(date);
		if (offset === void 0) offset = timezoneOffset;
		return Math.floor((date.valueOf() / Time.minute - offset) / 1440);
	}
	Time.getDateNumber = getDateNumber;
	function fromDateNumber(value, offset) {
		const date = new Date(value * Time.day);
		if (offset === void 0) offset = timezoneOffset;
		return new Date(+date + offset * Time.minute);
	}
	Time.fromDateNumber = fromDateNumber;
	const numeric = /\d+(?:\.\d+)?/.source;
	const timeRegExp = new RegExp(`^${[
		"w(?:eek(?:s)?)?",
		"d(?:ay(?:s)?)?",
		"h(?:our(?:s)?)?",
		"m(?:in(?:ute)?(?:s)?)?",
		"s(?:ec(?:ond)?(?:s)?)?"
	].map((unit) => `(${numeric}${unit})?`).join("")}$`);
	function parseTime(source) {
		const capture = timeRegExp.exec(source);
		if (!capture) return 0;
		return (parseFloat(capture[1]) * Time.week || 0) + (parseFloat(capture[2]) * Time.day || 0) + (parseFloat(capture[3]) * Time.hour || 0) + (parseFloat(capture[4]) * Time.minute || 0) + (parseFloat(capture[5]) * Time.second || 0);
	}
	Time.parseTime = parseTime;
	function parseDate(date) {
		const parsed = parseTime(date);
		if (parsed) date = Date.now() + parsed;
		else if (/^\d{1,2}(:\d{1,2}){1,2}$/.test(date)) date = `${(/* @__PURE__ */ new Date()).toLocaleDateString()}-${date}`;
		else if (/^\d{1,2}-\d{1,2}-\d{1,2}(:\d{1,2}){1,2}$/.test(date)) date = `${(/* @__PURE__ */ new Date()).getFullYear()}-${date}`;
		return date ? new Date(date) : /* @__PURE__ */ new Date();
	}
	Time.parseDate = parseDate;
	function format(ms) {
		const abs = Math.abs(ms);
		if (abs >= Time.day - Time.hour / 2) return Math.round(ms / Time.day) + "d";
		else if (abs >= Time.hour - Time.minute / 2) return Math.round(ms / Time.hour) + "h";
		else if (abs >= Time.minute - Time.second / 2) return Math.round(ms / Time.minute) + "m";
		else if (abs >= Time.second) return Math.round(ms / Time.second) + "s";
		return ms + "ms";
	}
	Time.format = format;
	function toDigits(source, length = 2) {
		return source.toString().padStart(length, "0");
	}
	Time.toDigits = toDigits;
	function template(template, time = /* @__PURE__ */ new Date()) {
		return template.replace("yyyy", time.getFullYear().toString()).replace("yy", time.getFullYear().toString().slice(2)).replace("MM", toDigits(time.getMonth() + 1)).replace("dd", toDigits(time.getDate())).replace("hh", toDigits(time.getHours())).replace("mm", toDigits(time.getMinutes())).replace("ss", toDigits(time.getSeconds())).replace("SSS", toDigits(time.getMilliseconds(), 3));
	}
	Time.template = template;
})(Time || (Time = {}));
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+schemastery@3.18.1/node_modules/@deepseek-ai/schemastery/lib/index.mjs
const kSchema = Symbol.for("schemastery");
const kValidationError = Symbol.for("ValidationError");
globalThis.__schemastery_index__ ??= 0;
globalThis.__schemastery_refs__ = void 0;
var ValidationError = class extends TypeError {
	options;
	name = "ValidationError";
	constructor(message, options) {
		let prefix = "$";
		for (const segment of options.path || []) if (typeof segment === "string") prefix += "." + segment;
		else if (typeof segment === "number") prefix += "[" + segment + "]";
		else if (typeof segment === "symbol") prefix += `[Symbol(${segment.toString()})]`;
		if (prefix.startsWith(".")) prefix = prefix.slice(1);
		super((prefix === "$" ? "" : `${prefix} `) + message);
		this.options = options;
	}
	static is(error) {
		return !!error?.[kValidationError];
	}
};
Object.defineProperty(ValidationError.prototype, kValidationError, { value: true });
const Schema$1 = function(options) {
	const schema = function(data, options = {}) {
		return Schema$1.resolve(data, schema, options)[0];
	};
	if (options.refs) {
		const refs = mapValues(options.refs, (options) => new Schema$1(options));
		const getRef = (uid) => refs[uid];
		for (const key in refs) {
			const options = refs[key];
			options.sKey = getRef(options.sKey);
			options.inner = getRef(options.inner);
			options.list = options.list && options.list.map(getRef);
			options.dict = options.dict && mapValues(options.dict, getRef);
		}
		return refs[options.uid];
	}
	Object.assign(schema, options);
	if (typeof schema.callback === "string") try {
		schema.callback = new Function("return " + schema.callback)();
	} catch {}
	Object.defineProperty(schema, "uid", { value: globalThis.__schemastery_index__++ });
	Object.setPrototypeOf(schema, Schema$1.prototype);
	schema.meta ||= {};
	schema.toString = schema.toString.bind(schema);
	return schema;
};
Schema$1.prototype = Object.create(Function.prototype);
Schema$1.prototype[kSchema] = true;
Object.defineProperty(Schema$1.prototype, "~standard", { get() {
	return {
		version: 1,
		vendor: "schemastery",
		validate: (value) => {
			try {
				return { value: Schema$1.resolve(value, this, {})[0] };
			} catch (error) {
				if (ValidationError.is(error)) return { issues: [{
					message: error.message,
					path: error.options.path
				}] };
				throw error;
			}
		}
	};
} });
Schema$1.ValidationError = ValidationError;
Schema$1.prototype.toJSON = function toJSON() {
	if (globalThis.__schemastery_refs__) {
		globalThis.__schemastery_refs__[this.uid] ??= JSON.parse(JSON.stringify({ ...this }));
		return this.uid;
	}
	globalThis.__schemastery_refs__ = { [this.uid]: { ...this } };
	globalThis.__schemastery_refs__[this.uid] = JSON.parse(JSON.stringify({ ...this }));
	const result = {
		uid: this.uid,
		refs: globalThis.__schemastery_refs__
	};
	globalThis.__schemastery_refs__ = void 0;
	return result;
};
Schema$1.prototype.set = function set(key, value) {
	this.dict[key] = value;
	return this;
};
Schema$1.prototype.push = function push(value) {
	this.list.push(value);
	return this;
};
function mergeDesc(original, messages) {
	const result = typeof original === "string" ? { "": original } : { ...original };
	for (const locale in messages) {
		const value = messages[locale];
		if (value?.$description || value?.$desc) result[locale] = value.$description || value.$desc;
		else if (typeof value === "string") result[locale] = value;
	}
	return result;
}
function getInner(value) {
	return value?.$value ?? value?.$inner;
}
function extractKeys(data) {
	return filterKeys(data ?? {}, (key) => !key.startsWith("$"));
}
Schema$1.prototype.i18n = function i18n(messages) {
	const schema = Schema$1(this);
	const desc = mergeDesc(schema.meta.description, messages);
	if (Object.keys(desc).length) schema.meta.description = desc;
	if (schema.dict) schema.dict = mapValues(schema.dict, (inner, key) => {
		return inner.i18n(mapValues(messages, (data) => getInner(data)?.[key] ?? data?.[key]));
	});
	if (schema.list) schema.list = schema.list.map((inner, index) => {
		return inner.i18n(mapValues(messages, (data = {}) => {
			if (Array.isArray(getInner(data))) return getInner(data)[index];
			if (Array.isArray(data)) return data[index];
			return extractKeys(data);
		}));
	});
	if (schema.inner) schema.inner = schema.inner.i18n(mapValues(messages, (data) => {
		if (getInner(data)) return getInner(data);
		return extractKeys(data);
	}));
	if (schema.sKey) schema.sKey = schema.sKey.i18n(mapValues(messages, (data) => data?.$key));
	return schema;
};
Schema$1.prototype.extra = function extra(key, value) {
	const schema = Schema$1(this);
	schema.meta = {
		...schema.meta,
		[key]: value
	};
	return schema;
};
for (const key of [
	"required",
	"disabled",
	"collapse",
	"hidden",
	"loose"
]) Object.assign(Schema$1.prototype, { [key](value = true) {
	const schema = Schema$1(this);
	schema.meta = {
		...schema.meta,
		[key]: value
	};
	return schema;
} });
Schema$1.prototype.deprecated = function deprecated() {
	const schema = Schema$1(this);
	schema.meta.badges ||= [];
	schema.meta.badges.push({
		text: "deprecated",
		type: "danger"
	});
	return schema;
};
Schema$1.prototype.experimental = function experimental() {
	const schema = Schema$1(this);
	schema.meta.badges ||= [];
	schema.meta.badges.push({
		text: "experimental",
		type: "warning"
	});
	return schema;
};
Schema$1.prototype.pattern = function pattern(regexp) {
	const schema = Schema$1(this);
	const pattern = pick(regexp, ["source", "flags"]);
	schema.meta = {
		...schema.meta,
		pattern
	};
	return schema;
};
Schema$1.prototype.simplify = function simplify(value) {
	if (deepEqual(value, this.meta.default, this.type === "dict")) return null;
	if (isNullable(value)) return value;
	if (this.type === "object" || this.type === "dict") {
		const result = {};
		for (const key in value) {
			const item = (this.type === "object" ? this.dict[key] : this.inner)?.simplify(value[key]);
			if (this.type === "dict" || !isNullable(item)) result[key] = item;
		}
		if (deepEqual(result, this.meta.default, this.type === "dict")) return null;
		return result;
	} else if (this.type === "array" || this.type === "tuple") {
		const result = [];
		value.forEach((value, index) => {
			const schema = this.type === "array" ? this.inner : this.list[index];
			const item = schema ? schema.simplify(value) : value;
			result.push(item);
		});
		return result;
	} else if (this.type === "intersect") {
		const result = {};
		for (const item of this.list) Object.assign(result, item.simplify(value));
		return result;
	} else if (this.type === "union") for (const schema of this.list) try {
		Schema$1.resolve(value, schema, {});
		return schema.simplify(value);
	} catch {}
	return value;
};
Schema$1.prototype.toString = function toString(inline) {
	return formatters[this.type]?.(this, inline) ?? `Schema<${this.type}>`;
};
Schema$1.prototype.role = function role(role, extra) {
	const schema = Schema$1(this);
	schema.meta = {
		...schema.meta,
		role,
		extra
	};
	return schema;
};
for (const key of [
	"default",
	"link",
	"comment",
	"description",
	"max",
	"min",
	"step"
]) Object.assign(Schema$1.prototype, { [key](value) {
	const schema = Schema$1(this);
	schema.meta = {
		...schema.meta,
		[key]: value
	};
	return schema;
} });
const resolvers = {};
Schema$1.extend = function extend(type, resolve) {
	resolvers[type] = resolve;
};
Schema$1.resolve = function resolve(data, schema, options = {}, strict = false) {
	if (!schema) return [data];
	if (options.ignore?.(data, schema)) return [data];
	if (isNullable(data) && schema.type !== "lazy") {
		if (schema.meta.required) throw new ValidationError(`missing required value`, options);
		let current = schema;
		let fallback = schema.meta.default;
		while (current?.type === "intersect" && isNullable(fallback)) {
			current = current.list[0];
			fallback = current?.meta.default;
		}
		if (isNullable(fallback)) return [data];
		data = clone(fallback);
	}
	const callback = resolvers[schema.type];
	if (!callback) throw new ValidationError(`unsupported type "${schema.type}"`, options);
	try {
		return callback(data, schema, options, strict);
	} catch (error) {
		if (!schema.meta.loose) throw error;
		return [schema.meta.default];
	}
};
Schema$1.from = function from(source) {
	if (isNullable(source)) return Schema$1.any();
	else if ([
		"string",
		"number",
		"boolean"
	].includes(typeof source)) return Schema$1.const(source).required();
	else if (source[kSchema]) return source;
	else if (typeof source === "function") switch (source) {
		case String: return Schema$1.string().required();
		case Number: return Schema$1.number().required();
		case Boolean: return Schema$1.boolean().required();
		case Function: return Schema$1.function().required();
		default: return Schema$1.is(source).required();
	}
	else throw new TypeError(`cannot infer schema from ${source}`);
};
Schema$1.lazy = function lazy(builder) {
	const toJSON = () => {
		if (!schema.inner[kSchema]) {
			schema.inner = schema.builder();
			schema.inner.meta = {
				...schema.meta,
				...schema.inner.meta
			};
		}
		return schema.inner.toJSON();
	};
	const schema = new Schema$1({
		type: "lazy",
		builder,
		inner: { toJSON }
	});
	return schema;
};
Schema$1.natural = function natural() {
	return Schema$1.number().step(1).min(0);
};
Schema$1.percent = function percent() {
	return Schema$1.number().step(.01).min(0).max(1).role("slider");
};
Schema$1.date = function date() {
	return Schema$1.union([Schema$1.is(Date), Schema$1.transform(Schema$1.string().role("datetime"), (value, options) => {
		const date = new Date(value);
		if (isNaN(+date)) throw new ValidationError(`invalid date "${value}"`, options);
		return date;
	}, true)]);
};
Schema$1.regExp = function regExp(flag = "") {
	return Schema$1.union([Schema$1.is(RegExp), Schema$1.transform(Schema$1.string().role("regexp", { flag }), (value, options) => {
		try {
			return new RegExp(value, flag);
		} catch (e) {
			throw new ValidationError(e.message, options);
		}
	}, true)]);
};
Schema$1.arrayBuffer = function arrayBuffer(encoding) {
	return Schema$1.union([
		Schema$1.is(ArrayBuffer),
		Schema$1.is(SharedArrayBuffer),
		Schema$1.transform(Schema$1.any(), (value, options) => {
			if (Binary.isSource(value)) return Binary.fromSource(value);
			throw new ValidationError(`expected ArrayBufferSource but got ${value}`, options);
		}, true),
		...encoding ? [Schema$1.transform(Schema$1.string(), (value, options) => {
			try {
				return encoding === "base64" ? Binary.fromBase64(value) : Binary.fromHex(value);
			} catch (e) {
				throw new ValidationError(e.message, options);
			}
		}, true)] : []
	]);
};
Schema$1.extend("lazy", (data, schema, options, strict) => {
	if (!schema.inner[kSchema]) {
		schema.inner = schema.builder();
		schema.inner.meta = {
			...schema.meta,
			...schema.inner.meta
		};
	}
	return Schema$1.resolve(data, schema.inner, options, strict);
});
Schema$1.extend("any", (data) => {
	return [data];
});
Schema$1.extend("never", (data, _, options) => {
	throw new ValidationError(`expected nullable but got ${data}`, options);
});
Schema$1.extend("const", (data, { value }, options) => {
	if (deepEqual(data, value)) return [value];
	throw new ValidationError(`expected ${value} but got ${data}`, options);
});
function checkWithinRange(data, meta, description, options, skipMin = false) {
	const { max = Infinity, min = -Infinity } = meta;
	if (data > max) throw new ValidationError(`expected ${description} <= ${max} but got ${data}`, options);
	if (data < min && !skipMin) throw new ValidationError(`expected ${description} >= ${min} but got ${data}`, options);
}
Schema$1.extend("string", (data, { meta }, options) => {
	if (typeof data !== "string") throw new ValidationError(`expected string but got ${data}`, options);
	if (meta.pattern) {
		const regexp = new RegExp(meta.pattern.source, meta.pattern.flags);
		if (!regexp.test(data)) throw new ValidationError(`expect string to match regexp ${regexp}`, options);
	}
	checkWithinRange(data.length, meta, "string length", options);
	return [data];
});
function decimalShift(data, digits) {
	const str = data.toString();
	if (str.includes("e")) return data * Math.pow(10, digits);
	const index = str.indexOf(".");
	if (index === -1) return data * Math.pow(10, digits);
	const frac = str.slice(index + 1);
	const integer = str.slice(0, index);
	if (frac.length <= digits) return +(integer + frac.padEnd(digits, "0"));
	return +(integer + frac.slice(0, digits) + "." + frac.slice(digits));
}
function isMultipleOf(data, min, step) {
	step = Math.abs(step);
	if (!/^\d+\.\d+$/.test(step.toString())) return (data - min) % step === 0;
	const index = step.toString().indexOf(".");
	const digits = step.toString().slice(index + 1).length;
	return Math.abs(decimalShift(data, digits) - decimalShift(min, digits)) % decimalShift(step, digits) === 0;
}
Schema$1.extend("number", (data, { meta }, options) => {
	if (typeof data !== "number") throw new ValidationError(`expected number but got ${data}`, options);
	checkWithinRange(data, meta, "number", options);
	const { step } = meta;
	if (step && !isMultipleOf(data, meta.min ?? 0, step)) throw new ValidationError(`expected number multiple of ${step} but got ${data}`, options);
	return [data];
});
Schema$1.extend("boolean", (data, _, options) => {
	if (typeof data === "boolean") return [data];
	throw new ValidationError(`expected boolean but got ${data}`, options);
});
Schema$1.extend("bitset", (data, { bits, meta }, options) => {
	let value = 0, keys = [];
	if (typeof data === "number") {
		value = data;
		for (const key in bits) if (data & bits[key]) keys.push(key);
	} else if (Array.isArray(data)) {
		keys = data;
		for (const key of keys) {
			if (typeof key !== "string") throw new ValidationError(`expected string but got ${key}`, options);
			if (key in bits) value |= bits[key];
		}
	} else throw new ValidationError(`expected number or array but got ${data}`, options);
	if (value === meta.default) return [value];
	return [value, keys];
});
Schema$1.extend("function", (data, _, options) => {
	if (typeof data === "function") return [data];
	throw new ValidationError(`expected function but got ${data}`, options);
});
Schema$1.extend("is", (data, { constructor }, options) => {
	if (typeof constructor === "function") {
		if (data instanceof constructor) return [data];
		throw new ValidationError(`expected ${constructor.name} but got ${data}`, options);
	} else {
		if (isNullable(data)) throw new ValidationError(`expected ${constructor} but got ${data}`, options);
		let prototype = Object.getPrototypeOf(data);
		while (prototype) {
			if (prototype.constructor?.name === constructor) return [data];
			prototype = Object.getPrototypeOf(prototype);
		}
		throw new ValidationError(`expected ${constructor} but got ${data}`, options);
	}
});
function property(data, key, schema, options) {
	try {
		const [value, adapted] = Schema$1.resolve(data[key], schema, {
			...options,
			path: [...options.path || [], key]
		});
		if (adapted !== void 0) data[key] = adapted;
		return value;
	} catch (e) {
		if (!options?.autofix) throw e;
		delete data[key];
		return schema.meta.default;
	}
}
Schema$1.extend("array", (data, { inner, meta }, options) => {
	if (!Array.isArray(data)) throw new ValidationError(`expected array but got ${data}`, options);
	checkWithinRange(data.length, meta, "array length", options, !isNullable(inner.meta.default));
	return [data.map((_, index) => property(data, index, inner, options))];
});
Schema$1.extend("dict", (data, { inner, sKey }, options, strict) => {
	if (!isPlainObject(data)) throw new ValidationError(`expected object but got ${data}`, options);
	const result = {};
	for (const key in data) {
		let rKey;
		try {
			rKey = Schema$1.resolve(key, sKey, options)[0];
		} catch (error) {
			if (strict) continue;
			throw error;
		}
		result[rKey] = property(data, key, inner, options);
		data[rKey] = data[key];
		if (key !== rKey) delete data[key];
	}
	return [result];
});
Schema$1.extend("tuple", (data, { list }, options, strict) => {
	if (!Array.isArray(data)) throw new ValidationError(`expected array but got ${data}`, options);
	const result = list.map((inner, index) => property(data, index, inner, options));
	if (strict) return [result];
	result.push(...data.slice(list.length));
	return [result];
});
function merge(result, data) {
	for (const key in data) {
		if (key in result) continue;
		result[key] = data[key];
	}
}
Schema$1.extend("object", (data, { dict }, options, strict) => {
	if (!isPlainObject(data)) throw new ValidationError(`expected object but got ${data}`, options);
	const result = {};
	for (const key in dict) {
		const value = property(data, key, dict[key], options);
		if (!isNullable(value) || key in data) result[key] = value;
	}
	if (!strict) merge(result, data);
	return [result];
});
Schema$1.extend("union", (data, { list, toString }, options, strict) => {
	const messages = [];
	for (const inner of list) try {
		return Schema$1.resolve(data, inner, options, strict);
	} catch (error) {
		messages.push(error);
	}
	throw new ValidationError(`expected ${toString()} but got ${JSON.stringify(data)}`, options);
});
Schema$1.extend("intersect", (data, { list, toString }, options, strict) => {
	if (!list.length) return [data];
	let result;
	for (const inner of list) {
		const value = Schema$1.resolve(data, inner, options, true)[0];
		if (isNullable(value)) continue;
		if (isNullable(result)) result = value;
		else if (typeof result !== typeof value) throw new ValidationError(`expected ${toString()} but got ${JSON.stringify(data)}`, options);
		else if (typeof value === "object") merge(result ??= {}, value);
		else if (result !== value) throw new ValidationError(`expected ${toString()} but got ${JSON.stringify(data)}`, options);
	}
	if (!strict && isPlainObject(data)) merge(result, data);
	return [result];
});
Schema$1.extend("transform", (data, { inner, callback, preserve }, options) => {
	const [result, adapted = data] = Schema$1.resolve(data, inner, options, true);
	if (preserve) return [callback(result)];
	else return [callback(result), callback(adapted)];
});
const formatters = {};
function defineMethod(name, keys, format) {
	formatters[name] = format;
	Object.assign(Schema$1, { [name](...args) {
		const schema = new Schema$1({ type: name });
		keys.forEach((key, index) => {
			switch (key) {
				case "sKey":
					schema.sKey = args[index] ?? Schema$1.string();
					break;
				case "inner":
					schema.inner = Schema$1.from(args[index]);
					break;
				case "list":
					schema.list = args[index].map(Schema$1.from);
					break;
				case "dict":
					schema.dict = mapValues(args[index], Schema$1.from);
					break;
				case "bits":
					schema.bits = {};
					for (const key in args[index]) {
						if (typeof args[index][key] !== "number") continue;
						schema.bits[key] = args[index][key];
					}
					break;
				case "callback": {
					const callback = schema.callback = args[index];
					callback["toJSON"] ||= () => callback.toString();
					break;
				}
				case "constructor": {
					const constructor = schema.constructor = args[index];
					if (typeof constructor === "function") constructor["toJSON"] ||= () => constructor["name"];
					break;
				}
				default: schema[key] = args[index];
			}
		});
		if (name === "object" || name === "dict") schema.meta.default = {};
		else if (name === "array" || name === "tuple") schema.meta.default = [];
		else if (name === "bitset") schema.meta.default = 0;
		return schema;
	} });
}
defineMethod("is", ["constructor"], ({ constructor }) => {
	if (typeof constructor === "function") return constructor.name;
	else return constructor;
});
defineMethod("any", [], () => "any");
defineMethod("never", [], () => "never");
defineMethod("const", ["value"], ({ value }) => typeof value === "string" ? JSON.stringify(value) : value);
defineMethod("string", [], () => "string");
defineMethod("number", [], () => "number");
defineMethod("boolean", [], () => "boolean");
defineMethod("bitset", ["bits"], () => "bitset");
defineMethod("function", [], () => "function");
defineMethod("array", ["inner"], ({ inner }) => `${inner.toString(true)}[]`);
defineMethod("dict", ["inner", "sKey"], ({ inner, sKey }) => `{ [key: ${sKey.toString()}]: ${inner.toString()} }`);
defineMethod("tuple", ["list"], ({ list }) => `[${list.map((inner) => inner.toString()).join(", ")}]`);
defineMethod("object", ["dict"], ({ dict }) => {
	if (Object.keys(dict).length === 0) return "{}";
	return `{ ${Object.entries(dict).map(([key, inner]) => {
		return `${key}${inner.meta.required ? "" : "?"}: ${inner.toString()}`;
	}).join(", ")} }`;
});
defineMethod("union", ["list"], ({ list }, inline) => {
	const result = list.map(({ toString: format }) => format()).join(" | ");
	return inline ? `(${result})` : result;
});
defineMethod("intersect", ["list"], ({ list }) => {
	return `${list.map((inner) => inner.toString(true)).join(" & ")}`;
});
defineMethod("transform", [
	"inner",
	"callback",
	"preserve"
], ({ inner }, isInner) => inner.toString(isInner));
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+dsh-timeout@0.1.0-rc.6_@deepseek-ai+cordis@4.0.1_@deepseek-ai+dsh-invariants@0.1_rfhz2vo7cpgebyrk6pl5k7eyo4/node_modules/@deepseek-ai/dsh-timeout/lib/index.js
/** Largest delay Node schedules without clamping it to one millisecond. */
const MAX_TIMER_DELAY_MS = 2147483647;
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+dsh-llm@0.1.0-rc.6_@deepseek-ai+cordis@4.0.1_@deepseek-ai+dsh-attachment@0.1.0-r_hprhh64pvy7rp3ljnmddhare6q/node_modules/@deepseek-ai/dsh-llm/lib/index.js
/**
* dsh-llm's owned branded ids: tool-call correlation and provider request
* diagnostics.
*
* The `Branded<B>` primitive itself lives in `@deepseek-ai/dsh-brand` (a
* zero-dependency type-only package) so every owner of a cross-boundary id can
* brand it without depending on dsh-llm; see that package's README for the
* nominal-typing policy.
*
* @module @deepseek-ai/dsh-llm/brand
*/
/**
* Brand a message identifier.
* @param id - the opaque message identifier.
* @returns the same string, branded; no validation is performed.
*/
function MessageId(id) {
	return id;
}
/**
* Deep-freeze a value in place with an iterative traversal, guarding cycles,
* so later mutation throws without imposing a JavaScript call-stack depth cap.
* {@link AbortSignal} objects are deliberately skipped because they are the
* request's live cancellation channel and freezing them breaks abort.
* @param value - the value to freeze in place.
* @returns the same value, frozen.
*/
function deepFreeze(value) {
	const seen = /* @__PURE__ */ new WeakSet();
	const pending = [{
		kind: "visit",
		node: value
	}];
	while (pending.length > 0) {
		const task = pending.pop();
		/* v8 ignore next -- the loop condition guarantees one pending task. */
		if (task === void 0) continue;
		if (task.kind === "property") {
			pending.push({
				kind: "visit",
				node: task.source[task.key]
			});
			continue;
		}
		const node = task.node;
		if (node === null || typeof node !== "object") continue;
		if (node instanceof AbortSignal) continue;
		if (seen.has(node)) continue;
		seen.add(node);
		Object.freeze(node);
		const keys = Object.keys(node);
		for (let index = keys.length - 1; index >= 0; index--) {
			const key = keys[index];
			/* v8 ignore next -- the loop is bounded by the captured key count. */
			if (key === void 0) continue;
			pending.push({
				kind: "property",
				source: node,
				key
			});
		}
	}
	return value;
}
/**
* Detach and deep-freeze a message whose identity already exists.
* @param message - complete message, including its stable identity.
* @returns an immutable snapshot that preserves the identity.
*/
function freezeMessage(message) {
	return deepFreeze(structuredClone(message));
}
/**
* Create one identified message and freeze it before publication.
* @param input - complete role, content, and source for a new message.
* @returns an immutable message with a fresh stable identity.
*/
function createMessage(input) {
	return freezeMessage({
		...input,
		id: MessageId(crypto.randomUUID())
	});
}
/**
* Create one identified user-role message and freeze it before publication.
* @param input - complete content and source for a new user message.
* @returns an immutable user message with a fresh stable identity.
*/
function createUserMessage(input) {
	return createMessage({
		...input,
		role: "user"
	});
}
/**
* Canonical provider-neutral code for a response that completed normally but
* carried no content blocks at all. Providers occasionally emit a degenerate
* completion (a terminal stop with zero output); adapters classify it as this
* failure instead of yielding an empty assistant message, because an empty
* message silently ends the turn with nothing for the user or the loop to act
* on. The attempt produced nothing durable, so retry policy treats it as safe
* to repeat.
*/
const EMPTY_RESPONSE_CODE = "EMPTY_RESPONSE";
new RegExp(String.raw`(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]` + String.raw`(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])`, "i");
new RegExp(String.raw`\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?` + String.raw`too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?` + String.raw`(?:model(?:'s)?\s+)?context(?:\s+window)?\b`, "i");
new RegExp(String.raw`\b(?:input|prompt|request|messages?)\b.{0,40}` + String.raw`\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}` + String.raw`\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b`, "i");
/**
* Provider-owned request-retry policy configuration and resolution.
*
* Adapters expose one resolved policy per registered provider route; the
* optional dsh-llm-retry plugin executes it on the agent's failed-step extension point.
*
* @module @deepseek-ai/dsh-llm/retry-policy
*/
const DEFAULT_MAX_RETRIES = 2;
const DEFAULT_INITIAL_DELAY_MS = 500;
const DEFAULT_MAX_DELAY_MS = 1e4;
const DEFAULT_JITTER_RATIO = .1;
const DEFAULT_RETRYABLE_CODES = Object.freeze([
	EMPTY_RESPONSE_CODE,
	"RATE_LIMIT",
	"SERVER",
	"TIMEOUT",
	"TRANSPORT"
]);
const backoffSchema = Schema$1.object({
	initialDelayMs: Schema$1.number().max(MAX_TIMER_DELAY_MS).default(DEFAULT_INITIAL_DELAY_MS),
	maxDelayMs: Schema$1.number().max(MAX_TIMER_DELAY_MS).default(DEFAULT_MAX_DELAY_MS),
	jitterRatio: Schema$1.number().min(0).max(1).default(DEFAULT_JITTER_RATIO)
});
const normalPolicySchema = Schema$1.object({
	mode: Schema$1.const("normal").required(),
	maxRetries: Schema$1.number().step(1).min(0).max(Number.MAX_SAFE_INTEGER).default(DEFAULT_MAX_RETRIES),
	retryableCodes: Schema$1.array(Schema$1.string()).default([...DEFAULT_RETRYABLE_CODES]),
	backoff: backoffSchema
});
const alwaysPolicySchema = Schema$1.object({
	mode: Schema$1.const("always").required(),
	backoff: backoffSchema
});
Schema$1.union([normalPolicySchema, alwaysPolicySchema]);
/**
* Centralize the non-secret product identity every provider request sends as `User-Agent`, keeping
* adapters from drifting. See
* `.agents/notes/implemented/architecture/2026-06-21-mandatory-app-attribution-headers.md`.
*
* App-attribution vocabulary for provider requests.
* @module @deepseek-ai/dsh-llm/attribution
*/
const { version } = createRequire(import.meta.url)("../package.json");
//#endregion
//#region src/context.ts
/**
* context.ts — 画像 + 相关记忆首步注入（v2：agent/pre-step middleware 真注入）
*
* v2 策略（2026-08-16 对齐 dsh-agent-instructions 官方做法）：
* - 挂接 agent/pre-step waterfall middleware（与 dsh 内置 agent-instructions 同通道），
*   在首次 step 时拉取 SGME 画像（/v1/inject）+ 项目相关记忆（/v1/search），
*   通过返回 {kind:'enter', messages} 把注入消息真正插入模型决策流。
* - v1 的缺陷：只 ctx.logger.info 打日志，消息从未进入模型上下文（实测会话日志
*   agent/inbox/spliced 中只有用户消息，无 SGME 画像）→ 本次修复。
* - 注入时机：首个 step（step === 1）注入一次，之后不再重复（避免每轮污染上下文）。
*
* 契约对齐：POST /v1/inject（Agent Key，mode + custom_filter 二选一）
*/
/** 注入消息源标记（对齐 agent-instructions 的 source.kind=plugin 约定）。 */
const PLUGIN_NAME = "dsh-sgme";
/** 相同内容判定（对齐 agent-instructions sameContextPayload：content + source 全等）。 */
function sameContextPayload(left, right) {
	if (typeof left !== "object" || left === null || typeof right !== "object" || right === null) return left === right;
	const l = left;
	const r = right;
	return JSON.stringify(l.content) === JSON.stringify(r.content) && JSON.stringify(l.source) === JSON.stringify(r.source);
}
/**
* 注册画像首步注入（agent/pre-step middleware）。
*
* 实现方式：监听 agent/pre-step（waterfall），首次 step 时拉取 SGME 画像 + 相关记忆，
* 拼接为 user 角色消息，返回 {kind:'enter', messages: ...} 注入模型决策流。
*
* 与 agent-instructions 共存：同通道多 middleware 串行叠加，SGME 消息插在
* claimed messages 之后（lastClaimedIndex+1），不影响 agent-instructions 的注入。
*
* @returns 清理函数（由 ctx.effect 调用方管理生命周期）
*/
function registerContextInjection(ctx, client, config) {
	let profileCache = null;
	let fetching = null;
	let injected = false;
	/** 预拉取画像 + 相关记忆（turn/start 触发，失败不置位，下轮重试）。 */
	const prefetch = (projectHint) => {
		if (fetching) return;
		fetching = (async () => {
			try {
				const [profile, related] = await Promise.all([client.inject({ mode: config.injectMode }), projectHint ? client.search({
					query: projectHint,
					scopes: ["memory"],
					limit: config.searchLimit
				}) : Promise.resolve(null)]);
				const text = buildInjectionText(profile, related);
				if (text) profileCache = {
					text,
					ts: Date.now()
				};
			} catch (e) {
				ctx.logger.warn(`[SGME 画像预拉取失败] ${e instanceof Error ? e.message : String(e)}`);
			} finally {
				fetching = null;
			}
		})();
	};
	const handler = async (payload, next) => {
		const decision = await next();
		if (injected) return decision;
		if (payload.step !== 1) return decision;
		if (decision.kind === "reject") return decision;
		const projectHint = config.projectHint || process.env.SGME_PROJECT_HINT || (payload.agent?.session?.header?.cwd ? payload.agent.session.header.cwd.split(/[\\/]/).filter(Boolean).pop() : void 0);
		if (fetching) try {
			await fetching;
		} catch {}
		if (!profileCache) try {
			const [profile, related] = await Promise.all([client.inject({ mode: config.injectMode }), projectHint ? client.search({
				query: projectHint,
				scopes: ["memory"],
				limit: config.searchLimit
			}) : Promise.resolve(null)]);
			const text = buildInjectionText(profile, related);
			if (text) profileCache = {
				text,
				ts: Date.now()
			};
		} catch (e) {
			ctx.logger.warn(`[SGME 画像注入失败] ${e instanceof Error ? e.message : String(e)}`);
			return decision;
		}
		if (!profileCache) return decision;
		injected = true;
		const desired = createUserMessage({
			content: [{
				type: "text",
				text: profileCache.text
			}],
			source: {
				kind: "plugin",
				plugin: PLUGIN_NAME
			}
		});
		if (decision.messages.some((message) => sameContextPayload(message, desired))) return decision;
		const firstClaimedIndex = decision.messages.findIndex((message) => (payload.messages ?? []).includes(message));
		const insertAt = firstClaimedIndex === -1 ? 0 : firstClaimedIndex;
		ctx.logger.info(`[SGME 画像注入] 已注入 ${profileCache.text.length} 字符（step ${payload.step}）`);
		return {
			kind: "enter",
			messages: decision.messages.toSpliced(insertAt, 0, desired)
		};
	};
	const disposePrefetch = ctx.on("turn/start", (payload) => {
		const agent = payload?.agent;
		const projectHint = config.projectHint || process.env.SGME_PROJECT_HINT || (agent?.session?.header?.cwd ? agent.session.header.cwd.split(/[\\/]/).filter(Boolean).pop() : void 0);
		prefetch(projectHint);
	});
	const disposePreStep = ctx.on("agent/pre-step", handler);
	return () => {
		disposePrefetch();
		disposePreStep();
	};
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
//#region src/rules.ts
/**
* rules.ts — DSH 用户级规则加载（dsg:rules system section）
*
* 读取 ~/.dsh/dsg-rules/rules.md（DSH 专用配置：身份/铁律/SGME手册/用户偏好/环境事实），
* 注册为 dsh-system-prompt 的稳定 section（order -70，位于 harness:identity(-100) 与
* persona(0) 之间）——稳定内容进 system 层，前缀缓存全命中。
*
* 设计要点（2026-08-16 定稿）：
* - 单文件单 section：规则类内容阅读/修改场景一致，不拆多文件
* - 用户级目录（~/.dsh/）不在任何 git 仓库内，天然不被提交；.gitignore 双保险
* - 文件缺失/读取失败 → 静默跳过，绝不阻塞插件启动
* - 支持热重载：文件 mtime 变化时更新 section（保留变更通知）
*/
/** dsg:rules section 的 order（位于 harness:identity=-100 与 persona=0 之间）。 */
const DSG_RULES_SECTION = "dsg:rules";
/** 默认规则文件路径（~/.dsh/dsg-rules/rules.md）。 */
function defaultRulesPath(dshHome) {
	const home = dshHome ?? process.env.DSH_HOME ?? join(homedir(), ".dsh");
	return join(home, "dsg-rules", "rules.md");
}
/**
* 注册 dsg:rules section。
* 读取规则文件 → 注册为稳定 section；文件不存在时跳过（不报错）。
*
* @returns 清理函数（由 ctx.effect 调用方管理生命周期）
*/
async function registerRulesSection(ctx, rulesPath = defaultRulesPath()) {
	let dispose = null;
	const loadAndRegister = async () => {
		let content;
		try {
			content = await readFile(rulesPath, "utf8");
		} catch (e) {
			if (e.code === "ENOENT") ctx.logger.info(`[dsg-rules] ${rulesPath} 不存在，跳过规则注入`);
			else ctx.logger.warn(`[dsg-rules] 读取失败: ${e instanceof Error ? e.message : String(e)}`);
			return;
		}
		const text = content.trim();
		if (!text) return;
		dispose?.();
		dispose = ctx.systemPrompt.section({
			name: DSG_RULES_SECTION,
			order: -70,
			text
		});
		ctx.logger.info(`[dsg-rules] 已注册 ${DSG_RULES_SECTION}（order -70，${text.length} 字符）`);
	};
	await loadAndRegister();
	return () => {
		dispose?.();
	};
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
const inject = [
	"tools",
	"commands",
	"systemPrompt"
];
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
	projectHint: Schema.string().default("").description("项目名提示（用于相关记忆检索，可空；缺省按会话 cwd 目录名推断）"),
	rulesPath: Schema.string().default("").description("DSH 用户级规则文件（缺省 ~/.dsh/dsg-rules/rules.md，注册为 dsg:rules system section）"),
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
	const contextCtx = {
		on: ctx.on,
		logger: {
			info: logger.info,
			warn: logger.warn
		}
	};
	const projectHint = config.projectHint || (process.env.SGME_PROJECT_HINT ?? "");
	const disposeContext = registerContextInjection(contextCtx, client, {
		injectMode: config.injectMode,
		injectMaxTokens: config.injectMaxTokens,
		searchLimit: config.searchLimit,
		...projectHint ? { projectHint } : {}
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
	const rulesPath = config.rulesPath || defaultRulesPath();
	registerRulesSection({
		systemPrompt: ctx.systemPrompt,
		logger: {
			info: logger.info,
			warn: logger.warn
		}
	}, rulesPath).then((disposeRules) => {
		ctx.effect(() => disposeRules, "sgme-rules-section");
	}).catch((e) => {
		const msg = e instanceof Error ? e.message : String(e);
		logger.warn(`[dsg-rules] 注册失败: ${msg}`);
	});
	logger.info("SGME bridge 全部能力已注册（画像注入 + 工具 + 命令 + 会话同步 + dsg-rules）");
}
//#endregion
export { Config, apply, inject, name };
