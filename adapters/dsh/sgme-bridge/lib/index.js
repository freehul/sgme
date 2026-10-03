import { createRequire } from "node:module";
import Schema from "schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { Context, Service } from "@deepseek-ai/cordis";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
//#region src/sgme-client.ts
/**
* 技能小节名归一化（供 skillGet 的 section 参数使用）。
*
* ⚠️ 契约坑（2026-08-29 实测 SGME 1.1.0）：服务端 section 参数要**纯标题文本**
* （`前置条件`），而 skill_digest 的 sections 骨架给的是**带 # 前缀的原样行**
* （`## 前置条件`）——照抄骨架传上去必 404，且错误文案误导为「技能不存在」。
* 本函数剥掉 # 前缀与两侧空白，让两种写法都能命中。
*/
function normalizeSkillSection(section) {
	if (!section) return null;
	return section.replace(/^#+\s*/, "").trim() || null;
}
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
	/** SGME 健康检查（GET /v1/health，免鉴权——Bearer 可选，不强制 X-API-Key）。失败返回 null。 */
	async health() {
		const url = this.baseUrl + "/v1/health";
		try {
			const ctrl = new AbortController();
			const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
			const resp = await fetch(url, { signal: ctrl.signal });
			clearTimeout(timer);
			if (!resp.ok) return null;
			return await resp.json();
		} catch {
			return null;
		}
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
	/** 统一 PATCH 请求，返回 [data, error]。失败时 data=null。 */
	async patch(path, body) {
		const url = `${this.baseUrl}${path}`;
		try {
			const ctrl = new AbortController();
			const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
			const resp = await fetch(url, {
				method: "PATCH",
				headers: {
					"Content-Type": "application/json",
					"X-API-Key": this.agentKey
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
	/**
	* 统一 PUT 请求（T-86：设置当前角色用），返回 [data, error]。失败时 data=null。
	*
	* ``keyType`` 默认 agent（角色切换等读侧语义）；技能写侧（skill_put）需传 admin。
	*/
	async put(path, body, keyType = "agent") {
		const key = keyType === "agent" ? this.agentKey : this.adminKey;
		const url = `${this.baseUrl}${path}`;
		try {
			const ctrl = new AbortController();
			const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
			const resp = await fetch(url, {
				method: "PUT",
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
	/**
	* 统一 DELETE 请求（技能删除用），返回 [data, error]。失败时 data=null。
	*
	* query 参数（hard/force 等）由调用方拼进 path——服务端读的是 Query 而非 body。
	*/
	async del(path, keyType) {
		const key = keyType === "agent" ? this.agentKey : this.adminKey;
		const url = `${this.baseUrl}${path}`;
		try {
			const ctrl = new AbortController();
			const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
			const resp = await fetch(url, {
				method: "DELETE",
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
	/** 检索 wiki 知识库页面（GET /v1/wiki/search，执行通道——含 skill 手册，不过滤；Agent Key）。失败返回 null。 */
	async wikiSearch(query, limit = 10) {
		const params = new URLSearchParams({
			q: query,
			limit: String(limit)
		});
		const [data, err] = await this.get(`/v1/wiki/search?${params.toString()}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] wikiSearch failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 列出 wiki 页面（GET /v1/wiki/pages，Agent Key；按 category 可选过滤）。失败返回 null。 */
	async wikiListPages(category, limit = 50, offset = 0) {
		const params = new URLSearchParams({
			limit: String(limit),
			offset: String(offset)
		});
		if (category) params.set("category", category);
		const [data, err] = await this.get(`/v1/wiki/pages?${params.toString()}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] wikiListPages failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 取 wiki 页面详情（GET /v1/wiki/pages/{id}，Agent Key）。失败返回 null。 */
	async wikiGetPage(pageId) {
		const [data, err] = await this.get(`/v1/wiki/pages/${encodeURIComponent(pageId)}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] wikiGetPage failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 更新 wiki 页面（PATCH /v1/wiki/pages/{id}，Agent Key）。失败返回 null。 */
	async wikiUpdatePage(pageId, body) {
		const [data, err] = await this.patch(`/v1/wiki/pages/${encodeURIComponent(pageId)}`, body);
		if (err) {
			console.warn(`[sgme-bridge] wikiUpdatePage failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 创建 wiki 页面（POST /v1/wiki/pages，Agent Key；T-55 幂等 upsert）。失败返回 null。 */
	async wikiCreatePage(body) {
		const [data, err] = await this.post("/v1/wiki/pages", body, "agent");
		if (err) {
			console.warn(`[sgme-bridge] wikiCreatePage failed: ${err}`);
			return null;
		}
		return data;
	}
	/** L0 索引列表（GET /v1/skills；分页浏览全量）。失败返回 null。 */
	async skillList(limit = 50, offset = 0) {
		const params = new URLSearchParams({
			limit: String(limit),
			offset: String(offset)
		});
		const [data, err] = await this.get(`/v1/skills?${params.toString()}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] skillList failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 技能检索（POST /v1/search scope=["skills"]，BM25+向量融合）。
	*
	* ⚠️ 契约要点：skills 层结果**不含 content/title**，只给 name/description/category，
	* 消费方必须走 skillDigest / skillGet 取正文，不可直接把 description 当全文用。
	* 失败返回 null。
	*/
	async skillSearch(query, limit = 5) {
		const [data, err] = await this.post("/v1/search", {
			query,
			scopes: ["skills"],
			limit
		}, "agent");
		if (err) {
			console.warn(`[sgme-bridge] skillSearch failed: ${err}`);
			return null;
		}
		if (!data?.results) return null;
		return data.results.map((r) => ({
			name: r.name ?? "",
			description: r.description ?? "",
			category: r.category ?? null,
			tags: [],
			source: r.source ?? "skills",
			version: null
		}));
	}
	/** L1 摘要（GET /v1/skills/{name}/digest；审核媒介层：骨架 + uses 依赖）。失败返回 null。 */
	async skillDigest(name) {
		const [data, err] = await this.get(`/v1/skills/${encodeURIComponent(name)}/digest`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] skillDigest failed: ${err}`);
			return null;
		}
		return data;
	}
	/** L2 全文（GET /v1/skills/{name}?section=；section 给定时只取该节省 token）。失败返回 null。 */
	async skillGet(name, section) {
		const normalized = normalizeSkillSection(section ?? null);
		const qs = normalized ? `?section=${encodeURIComponent(normalized)}` : "";
		const [data, err] = await this.get(`/v1/skills/${encodeURIComponent(name)}${qs}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] skillGet failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 冷启动包（GET /v1/skills/coldstart）。
	*
	* SGME 1.1.0 范式：只索引 1 个《技能检索协议》skill + SGME 操作手册，
	* 全量技能不预载——agent 按协议「先 skill_search 检索、再 skill_get 拉全文注入」。
	* 失败返回 null。
	*/
	async skillColdstart() {
		const [data, err] = await this.get("/v1/skills/coldstart", "agent");
		if (err) {
			console.warn(`[sgme-bridge] skillColdstart failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 自进化触发（POST /v1/wiki/evolve/trigger，Agent Key；W4 自动闭环）。失败返回 null。 */
	async evolveTrigger(sessionKey, minRounds = 5) {
		const [data, err] = await this.post("/v1/wiki/evolve/trigger", {
			session_key: sessionKey ?? null,
			min_rounds: minRounds
		}, "agent");
		if (err) {
			console.warn(`[sgme-bridge] evolveTrigger failed: ${err}`);
			return null;
		}
		return data;
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
	/** 添加创意（POST /v1/admin/ideas，Admin Key；用户主动提出才记录）。失败返回 null。 */
	async ideaAdd(body) {
		const [data, err] = await this.post("/v1/admin/ideas", body, "admin");
		if (err) {
			console.warn(`[sgme-bridge] ideaAdd failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 新建待办（POST /v1/admin/demands，Admin Key；跨项目统一待办池）。失败返回 null。 */
	async demandCreate(body) {
		const [data, err] = await this.post("/v1/admin/demands", body, "admin");
		if (err) {
			console.warn(`[sgme-bridge] demandCreate failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 登记项目（POST /v1/admin/projects，Admin Key；upsert，二次登记=更新）。失败返回 null。 */
	async projectRegister(body) {
		const [data, err] = await this.post("/v1/admin/projects", body, "admin");
		if (err) {
			console.warn(`[sgme-bridge] projectRegister failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 角色列表（GET /v1/admin/roles，Agent Key）。失败返回 null。 */
	async roleList() {
		const [data, err] = await this.get("/v1/admin/roles", "agent");
		if (err) {
			console.warn(`[sgme-bridge] roleList failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 装配角色沟通提示词（GET /v1/admin/roles/{role_id}/assemble，Agent Key）。失败返回 null。 */
	async roleAssemble(roleId, injectMode) {
		const params = new URLSearchParams();
		if (injectMode) params.set("inject_mode", injectMode);
		const qs = params.toString();
		const [data, err] = await this.get(`/v1/admin/roles/${encodeURIComponent(roleId)}/assemble${qs ? `?${qs}` : ""}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] roleAssemble failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 读取当前沟通角色（GET /v1/admin/care/active-role，Agent Key）。失败返回 null。 */
	async roleActiveGet() {
		const [data, err] = await this.get("/v1/admin/care/active-role", "agent");
		if (err) {
			console.warn(`[sgme-bridge] roleActiveGet failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 设置当前沟通角色（PUT /v1/admin/care/active-role，Agent Key）。失败返回 null。 */
	async roleActiveSet(roleId) {
		const [data, err] = await this.put("/v1/admin/care/active-role", { role_id: roleId });
		if (err) {
			console.warn(`[sgme-bridge] roleActiveSet failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 单条记忆详情（GET /v1/memory/{id}，Agent Key；含溯源 + 归档链）。失败返回 null。 */
	async memoryGet(memoryId) {
		const [data, err] = await this.get(`/v1/memory/${encodeURIComponent(memoryId)}`, "agent");
		if (err) {
			console.warn(`[sgme-bridge] memoryGet failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 标记记忆不采用（POST /v1/memory/{id}/reject，Agent Key；不删除、可恢复）。失败返回 null。 */
	async memoryReject(memoryId, reason) {
		const [data, err] = await this.post(`/v1/memory/${encodeURIComponent(memoryId)}/reject`, { reason: reason ?? null }, "agent");
		if (err) {
			console.warn(`[sgme-bridge] memoryReject failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 撤销「不采用」（POST /v1/memory/{id}/unreject，Agent Key；恢复为 active）。失败返回 null。 */
	async memoryUnreject(memoryId) {
		const [data, err] = await this.post(`/v1/memory/${encodeURIComponent(memoryId)}/unreject`, {}, "agent");
		if (err) {
			console.warn(`[sgme-bridge] memoryUnreject failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 聚合答案（POST /v1/answer，Agent Key）。
	*
	* 比 search 多一步：检索候选 → 题型分派 → LLM 生成答案。会消耗 LLM 调用，
	* 与 search 的纯检索不是一回事。LLM 全链不可用时服务端返回 503 → null。
	*/
	async answer(query, questionType, limit) {
		const [data, err] = await this.post("/v1/answer", {
			query,
			question_type: questionType ?? null,
			...limit !== void 0 ? { limit } : {}
		}, "agent");
		if (err) {
			console.warn(`[sgme-bridge] answer failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 统计：记忆/原始层计数 + 维度分布 + 提炼水位 + 注册 Agent（GET /v1/admin/stats）。失败返回 null。 */
	async stats() {
		const [data, err] = await this.get("/v1/admin/stats", "admin");
		if (err) {
			console.warn(`[sgme-bridge] stats failed: ${err}`);
			return null;
		}
		return data;
	}
	/** 读配置：整体读（section 省略）或单段读（GET /v1/admin/config[/{section}]）。失败返回 null。 */
	async configGet(section) {
		const path = section ? `/v1/admin/config/${encodeURIComponent(section)}` : "/v1/admin/config";
		const [data, err] = await this.get(path, "admin");
		if (err) {
			console.warn(`[sgme-bridge] configGet failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 更新配置段（POST /v1/admin/config，Admin Key；服务端 POST 与 PUT 等价）。
	*
	* ⚠️ 热生效 + 落盘 sgme.yaml——会真实改变服务端运行行为，非试验性调用。
	* section=null 时 values 的键即段名（单段形态）。
	*/
	async configUpdate(section, values) {
		const [data, err] = await this.post("/v1/admin/config", {
			section,
			values
		}, "admin");
		if (err) {
			console.warn(`[sgme-bridge] configUpdate failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 批量清空/全部消费未消费信号（POST /v1/admin/events/consume_all，Admin Key）。
	*
	* 幂等（二次调用 consumed=0）；subscriberId 传则同步推进该订阅者持久游标。
	*/
	async signalClear(eventType, subscriberId) {
		const params = new URLSearchParams();
		if (eventType) params.set("type", eventType);
		if (subscriberId) params.set("subscriber_id", subscriberId);
		const qs = params.toString();
		const [data, err] = await this.post(`/v1/admin/events/consume_all${qs ? `?${qs}` : ""}`, {}, "admin");
		if (err) {
			console.warn(`[sgme-bridge] signalClear failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 同步触发提炼（POST /v1/admin/refine/trigger，Admin Key）。
	*
	* ⚠️ 同步阻塞：file_id 给定时处理单文件，否则批量扫 status=new。
	* 会真实消耗 LLM 额度；批量场景优先用 triggerRefine（异步排队即返）。
	*/
	async refineTriggerSync(fileId, limit) {
		const [data, err] = await this.post("/v1/admin/refine/trigger", {
			file_id: fileId ?? null,
			...limit !== void 0 ? { limit } : {}
		}, "admin");
		if (err) {
			console.warn(`[sgme-bridge] refineTriggerSync failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 提炼记录分页（GET /v1/admin/refine_runs，Admin Key）。
	*
	* 做「提炼状态」观测用——服务端 refine_status 只有 MCP 侧无 HTTP 端点，
	* 故以本端点 + stats 近似（默认不做 status 过滤，error/running 默认可见）。
	*/
	async refineRuns(opts) {
		const params = new URLSearchParams();
		if (opts?.page !== void 0) params.set("page", String(opts.page));
		if (opts?.limit !== void 0) params.set("limit", String(opts.limit));
		if (opts?.stage) params.set("stage", opts.stage);
		if (opts?.status) params.set("status", opts.status);
		if (opts?.since) params.set("since", opts.since);
		if (opts?.until) params.set("until", opts.until);
		const qs = params.toString();
		const [data, err] = await this.get(`/v1/admin/refine_runs${qs ? `?${qs}` : ""}`, "admin");
		if (err) {
			console.warn(`[sgme-bridge] refineRuns failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 技能 L3 物化：字节保真写盘 dest_dir/<name>/SKILL.md（POST /v1/skills/{name}/materialize，Agent Key）。
	*
	* 不走 LLM 转写；返回落盘路径与 sha256，供脚本执行场景取真文件。
	*/
	async skillMaterialize(name, destDir) {
		const [data, err] = await this.post(`/v1/skills/${encodeURIComponent(name)}/materialize`, { dest_dir: destDir }, "agent");
		if (err) {
			console.warn(`[sgme-bridge] skillMaterialize failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 写入/覆盖技能（PUT /v1/admin/skills/{name}，Admin Key；SKILL.md 全文自动解析 frontmatter）。
	*
	* ⚠️ 服务端过 lint 门禁 + 三层查重，会落盘并 git commit 到技能源仓。
	*/
	async skillPut(name, content, skipLimits = false) {
		const [data, err] = await this.put(`/v1/admin/skills/${encodeURIComponent(name)}`, {
			content,
			skip_limits: skipLimits
		}, "admin");
		if (err) {
			console.warn(`[sgme-bridge] skillPut failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 删除技能（DELETE /v1/admin/skills/{name}，Admin Key）。
	*
	* 默认软删（deprecated 标记）；hard=true 物理删目录。有入向 uses 引用且
	* 未 force → 409 被拒。
	*/
	async skillDelete(name, hard = false, force = false) {
		const params = new URLSearchParams();
		if (hard) params.set("hard", "true");
		if (force) params.set("force", "true");
		const qs = params.toString();
		const [data, err] = await this.del(`/v1/admin/skills/${encodeURIComponent(name)}${qs ? `?${qs}` : ""}`, "admin");
		if (err) {
			console.warn(`[sgme-bridge] skillDelete failed: ${err}`);
			return null;
		}
		return data;
	}
	/**
	* 技能改名（POST /v1/admin/skills/{name}/rename，Admin Key；墓碑制永不原地改名）。
	*
	* 写新名副本 + 旧位置留 superseded_by 墓碑 + 登记 tombstones.json。
	* 需服务端 skills.source_dirs 指向 git 技能仓。
	*/
	async skillRename(name, newName) {
		const [data, err] = await this.post(`/v1/admin/skills/${encodeURIComponent(name)}/rename`, { new_name: newName }, "admin");
		if (err) {
			console.warn(`[sgme-bridge] skillRename failed: ${err}`);
			return null;
		}
		return data;
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
//#region node_modules/.pnpm/@deepseek-ai+dsh-typert-pro_0ed6bf01f07b8aa74e2681abf2790db7/node_modules/@deepseek-ai/dsh-typert-protocol/lib/index.js
/** The one Remote failure class shared by owners, the Gateway, and consumers. */
/**
* One Remote call failure: a real Error carrying its stable code and typed
* details. Owners throw it at the failure point; the Host Gateway encodes it
* onto the wire unchanged; the Client face rebuilds an instance for the
* `RemoteResult` error branch, so `throw result.error` keeps throw semantics.
* Discrimination is always by `code`, never by instanceof.
*/
var RemoteError = class extends Error {
	code;
	details;
	/** Structural marker: cross-realm/bundle identification never uses instanceof. */
	isDSHRemoteError = true;
	/**
	* @param code - stable failure code declared in {@link RemoteErrorDetailsMap}.
	* @param message - human diagnostic carried across the wire.
	* @param details - structured payload typed by the code.
	* @param options - standard Error options (`cause` survives in-process only).
	*/
	constructor(code, message, details, options) {
		super(message, options);
		this.code = code;
		this.details = details;
		this.name = "RemoteError";
	}
};
/**
* Remote decorators and explicit Gateway bindings backed by versioned
* descriptors carried on decorated class prototypes. Strict reflection
* remains a Typert compiler responsibility.
* @module @deepseek-ai/dsh-typert-protocol
*/
const TYPERT_REMOTE_SEGMENT_PATTERN = /^[A-Za-z0-9_$.-]+$/;
/**
* Test one generated Remote name against the Connection endpoint grammar.
* @param value - namespace, method, lookup, or Context segment.
* @returns whether the value can cross the shared RPC carrier unchanged.
*/
function isTypertRemoteSegment(value) {
	return value !== "." && value !== ".." && TYPERT_REMOTE_SEGMENT_PATTERN.test(value);
}
const REMOTE_METHOD_DESCRIPTOR = "@deepseek-ai/dsh-typert-protocol/remote-methods";
/**
* Bind one visible Service field to a Cordis key and Remote namespace. A
* service that owns a Cordis Context also gives its tree `ctx.invocation`,
* `undefined` outside a Remote call, so no `TypertRemoteService` is needed for
* a Host composition to read it.
* @param service - owning Service instance, normally `this`.
* @param serviceKey - exact Cordis service key.
* @param options - optional distinct wire namespace.
* @returns a frozen, inspectable binding with no compiler-injected metadata.
*/
function bindTypertRemote(service, serviceKey, options = {}) {
	validateName("service key", serviceKey);
	const namespace = options.namespace ?? serviceKey;
	validateName("namespace", namespace);
	const ctx = Reflect.get(service, "ctx");
	if (ctx instanceof Context) provideInvocationAccessor(ctx);
	return Object.freeze({
		service,
		serviceKey,
		namespace
	});
}
/** Cordis Service base that exposes its registered name through Typert Gateway. */
var TypertRemoteService = class extends Service {
	/** Visible binding consumed by the Gateway's source-mode discovery. */
	typertRemote;
	/**
	* Register the Service and bind the same key to Typert Gateway.
	* @param ctx - owning Cordis Context.
	* @param serviceKey - exact Cordis service key and default wire namespace.
	* @param options - optional distinct wire namespace.
	*/
	constructor(ctx, serviceKey, options = {}) {
		super(ctx, serviceKey);
		this.typertRemote = bindTypertRemote(this, this.name, options);
	}
};
/**
* Make `ctx.invocation` read as `undefined` outside a Remote call instead of the
* reflect service's "cannot get property" error; a call-derived Context shadows
* the accessor with its own property. The first Remote Service constructed in a
* tree registers it on the root, where it outlives any one Service.
*/
function provideInvocationAccessor(ctx) {
	if (Object.hasOwn(ctx.root.reflect.props, "invocation")) return;
	ctx.root.accessor("invocation", { get: () => void 0 });
}
function Remote(methodExportOrOptions, context) {
	if (typeof methodExportOrOptions === "string") {
		validateName("Remote export name", methodExportOrOptions);
		return remoteDecorator({ kind: "direct" }, void 0, methodExportOrOptions);
	}
	if (typeof methodExportOrOptions === "object") {
		if (remoteOptionMode(methodExportOrOptions) !== "stream" || Reflect.ownKeys(methodExportOrOptions).length !== 1) throw new TypeError("typert-protocol: Remote options must contain exactly mode: \"stream\"");
		return remoteDecorator({ kind: "direct" }, "stream");
	}
	if (context === void 0) throw new TypeError("typert-protocol: Remote decorator context is missing");
	addMarkerInitializer(context, { kind: "direct" });
}
function remoteOptionMode(options) {
	return Reflect.get(options, "mode");
}
function remoteDecorator(invocation, mode, exportName) {
	return function(_method, context) {
		addMarkerInitializer(context, invocation, mode, exportName);
	};
}
function readRemoteMethodDescriptor(prototype) {
	const property = Object.getOwnPropertyDescriptor(prototype, REMOTE_METHOD_DESCRIPTOR);
	if (property === void 0) return void 0;
	const descriptor = property.value;
	if (descriptor === null || typeof descriptor !== "object") throw new TypeError("typert-protocol: Remote method descriptor must be an object");
	const version = Reflect.get(descriptor, "version");
	if (version !== 1) throw new TypeError(`typert-protocol: unsupported Remote method descriptor version ${String(version)}`);
	const methods = Reflect.get(descriptor, "methods");
	if (!Array.isArray(methods)) throw new TypeError("typert-protocol: Remote method descriptor methods must be an array");
	return descriptor;
}
function addMarkerInitializer(context, invocation, mode, exportName) {
	if (context.private || context.static || typeof context.name !== "string") throw new TypeError("typert-protocol: Remote decorators require a public instance method with a string name");
	const method = context.name;
	context.addInitializer(function() {
		const prototype = Object.getPrototypeOf(this);
		if (prototype === null) throw new TypeError(`typert-protocol: cannot mark Remote method "${method}" on an object without a prototype`);
		mark(prototype, method, invocation, mode, exportName);
	});
}
function mark(prototype, method, invocation, mode, exportName) {
	const descriptor = readRemoteMethodDescriptor(prototype);
	const marker = Object.freeze({
		method,
		...exportName === void 0 || exportName === method ? {} : { exportName },
		...mode === void 0 ? {} : { mode },
		invocation: Object.freeze(invocation)
	});
	const current = descriptor?.methods.find((candidate) => candidate.method === method);
	if (current !== void 0) {
		if (current.exportName === marker.exportName && current.mode === marker.mode && sameInvocation(current.invocation, invocation)) return;
		throw new Error(`typert-protocol: Remote method "${method}" has conflicting invocation markers`);
	}
	Object.defineProperty(prototype, REMOTE_METHOD_DESCRIPTOR, {
		configurable: true,
		value: Object.freeze({
			version: 1,
			methods: Object.freeze([...descriptor?.methods ?? [], marker])
		})
	});
}
function sameInvocation(left, right) {
	if (left.kind === "direct") return right.kind === "direct";
	if (right.kind === "direct") return false;
	return left.context === right.context;
}
function validateName(subject, value) {
	if (!isTypertRemoteSegment(value)) throw new TypeError(`typert-protocol: ${subject} must contain only RPC endpoint segment characters`);
}
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+dsh-util-value_a1b445b943dd6f2898d88d6b9ffb1320/node_modules/@deepseek-ai/dsh-util-values/lib/index.js
/** Duplicate-install-safe JSON and immutable-value helpers. @module @deepseek-ai/dsh-util-values */
/**
* Mark an unreachable closed-union branch.
* @param value - impossible value; an unhandled typed variant fails at the call site.
* @param context - optional switch-site label included in the failure message.
* @returns never; a runtime value that escaped its type always throws.
*/
function assertNever(value, context) {
	const rendered = JSON.stringify(value) ?? String(value);
	throw new Error(`unreachable variant${context ? ` in ${context}` : ""}: ${rendered}`);
}
/**
* Deep-freeze an object graph in place while leaving live AbortSignal objects mutable.
* @param value - value to freeze.
* @returns the same value after every reachable enumerable child is frozen.
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
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+dsh-util-crypt_566393aaceb27aff7afe31ca52cb276c/node_modules/@deepseek-ai/dsh-util-crypto/lib/index.js
/**
* Random v4 UUID, minted from `crypto.getRandomValues`.
* @returns the UUID string.
*/
function randomUUID() {
	const bytes = globalThis.crypto.getRandomValues(/* @__PURE__ */ new Uint8Array(16));
	const hex = Array.from(bytes, (byte, index) => {
		return (index === 6 ? byte & 15 | 64 : index === 8 ? byte & 63 | 128 : byte).toString(16).padStart(2, "0");
	}).join("");
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+dsh-brand@0.2.0-rc.2_@deepseek-ai+cordis@4.0.4/node_modules/@deepseek-ai/dsh-brand/lib/index.js
/**
* Duplicate-install-safe nominal primitive helpers.
*
* A brand makes structurally identical strings or numbers non-interchangeable
* at the type level: a `SessionId` cannot be passed where a `ToolCallId` is
* expected, and an event sequence cannot be passed as a log offset. Comparison,
* logging, and serialization retain the underlying primitive behavior.
*
* This package owns no concrete domain value and keeps no runtime identity or mutable
* state, so independently installed copies produce interchangeable values.
*
* @module @deepseek-ai/dsh-brand
*/
/**
* Apply a compile-time string brand without changing the value.
* @param value - string admitted by the domain that owns the target brand.
* @returns the same string with the requested compile-time brand.
*/
function brandString(value) {
	return value;
}
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+cosmokit@1.8.5/node_modules/@deepseek-ai/cosmokit/lib/index.js
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
/** Shared config references used by schema validators and plugin runtimes. */
const write = Symbol.for("cosmokit.volatile.write");
function snapshot(value, ancestors = /* @__PURE__ */ new Set()) {
	if (typeof value === "function") throw new TypeError("volatile config cannot contain functions");
	if (value === null || typeof value !== "object") return value;
	if (ancestors.has(value)) throw new TypeError("volatile config cannot contain cycles");
	ancestors.add(value);
	try {
		if (Array.isArray(value)) return Object.freeze(value.map((item) => snapshot(item, ancestors)));
		if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) throw new TypeError("volatile config objects must be plain objects or arrays");
		return Object.freeze(Object.fromEntries(Object.entries(value).map(([key, item]) => [key, snapshot(item, ancestors)])));
	} finally {
		ancestors.delete(value);
	}
}
/**
* Create a detached reference containing an immutable copy of the supplied data.
* @param value - validated config data; class instances and functions are unsupported.
* @returns a reference whose value is updated only by its owning runtime.
*/
function createVolatile(value) {
	let current = snapshot(value);
	return Object.freeze({
		get: () => current,
		[write]: (value) => {
			current = value;
		}
	});
}
/**
* Identify references across ESM/CJS copies of the shared library.
* @param value - a parsed config value.
* @returns whether the value implements the shared reference protocol.
*/
function isVolatile(value) {
	return typeof value === "object" && value !== null && write in value;
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
/**
* Compare values recursively, treating two volatile references as equal regardless of value.
* Strict comparison distinguishes null/undefined, treats opaque objects by identity,
* compares URLs by normalized href, treats array holes as undefined, and considers distinct cyclic structures unequal.
* @param a - first value.
* @param b - second value.
* @param strict - whether to require strict data equality outside volatile references.
* @returns whether the values compare equal.
*/
function deepEqual(a, b, strict) {
	const ancestors = /* @__PURE__ */ new Set();
	function compare(a, b) {
		if (a === b) return true;
		if (isVolatile(a) || isVolatile(b)) return isVolatile(a) && isVolatile(b);
		if (!strict && isNullable(a) && isNullable(b)) return true;
		if (typeof a !== typeof b || typeof a !== "object" || !a || !b) return false;
		if (ancestors.has(a)) return false;
		function check(test, then) {
			return test(a) ? test(b) ? then(a, b) : false : test(b) ? false : void 0;
		}
		ancestors.add(a);
		try {
			return check(Array.isArray, (a, b) => {
				if (a.length !== b.length) return false;
				for (let index = 0; index < a.length; index++) if (!compare(a[index], b[index])) return false;
				return true;
			}) ?? check(is("Date"), (a, b) => a.valueOf() === b.valueOf()) ?? check(is("URL"), (a, b) => a.href === b.href) ?? check(is("RegExp"), (a, b) => a.source === b.source && a.flags === b.flags) ?? check(isArrayBufferLike, (a, b) => {
				if (a.byteLength !== b.byteLength) return false;
				const viewA = new Uint8Array(a);
				const viewB = new Uint8Array(b);
				for (let i = 0; i < viewA.length; i++) if (viewA[i] !== viewB[i]) return false;
				return true;
			}) ?? ((!strict || [a, b].every((value) => Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)) && Object.keys({
				...a,
				...b
			}).every((key) => compare(a[key], b[key])));
		} finally {
			ancestors.delete(a);
		}
	}
	return compare(a, b);
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
//#region node_modules/.pnpm/@deepseek-ai+schemastery@3.18.4/node_modules/@deepseek-ai/schemastery/lib/index.mjs
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
	if (isVolatile(value)) value = value.get();
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
Schema$1.prototype.volatile = function volatile() {
	if (this.meta.volatile) throw new TypeError("volatile schema is already wrapped");
	return this.extra("volatile", true);
};
const resolvers = {};
const checkedVolatile = Symbol("checked-volatile-schema");
function validateVolatileSchema(schema, path = [], blocked = false, seen = /* @__PURE__ */ new Map()) {
	const states = seen.get(schema) ?? /* @__PURE__ */ new Set();
	if (states.has(blocked)) return;
	states.add(blocked);
	seen.set(schema, states);
	if (schema.meta?.volatile && blocked) throw new ValidationError("volatile fields require a fixed object path without an enclosing volatile field", { path });
	const nested = blocked || !!schema.meta?.volatile;
	if (schema.dict) for (const [key, child] of Object.entries(schema.dict)) validateVolatileSchema(child, [...path, key], nested, seen);
	if (schema.sKey) validateVolatileSchema(schema.sKey, [...path, "<key>"], true, seen);
	if (schema.inner && (schema.type !== "lazy" || schema.inner[kSchema])) validateVolatileSchema(schema.inner, [...path, "*"], true, seen);
	if (schema.list) for (let index = 0; index < schema.list.length; index++) validateVolatileSchema(schema.list[index], [...path, String(index)], true, seen);
}
Schema$1.extend = function extend(type, resolve) {
	resolvers[type] = resolve;
};
Schema$1.resolve = function resolve(data, schema, options = {}, strict = false) {
	if (!schema) return [data];
	if (!options[checkedVolatile]) {
		validateVolatileSchema(schema, options.path);
		options = {
			...options,
			[checkedVolatile]: true
		};
	}
	if (schema.meta?.volatile) {
		const inner = Schema$1(schema);
		inner.meta = {
			...schema.meta,
			volatile: false
		};
		const [value, adapted] = Schema$1.resolve(data, inner, options, strict);
		try {
			return [createVolatile(value), adapted];
		} catch (error) {
			throw new ValidationError(error instanceof Error ? error.message : String(error), options);
		}
	}
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
		validateVolatileSchema(schema.inner, options.path, true);
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
		return schema.meta.volatile ? createVolatile(schema.meta.default) : schema.meta.default;
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
//#region node_modules/.pnpm/@deepseek-ai+dsh-timeout@0._b8424359025b5f3ee36fc59cc7db98be/node_modules/@deepseek-ai/dsh-timeout/lib/index.js
/** Largest delay Node schedules without clamping it to one millisecond. */
const MAX_TIMER_DELAY_MS = 2147483647;
//#endregion
//#region node_modules/.pnpm/@deepseek-ai+dsh-llm@0.2.0-rc.2_@deepseek-ai+cordis@4.0.4/node_modules/@deepseek-ai/dsh-llm/lib/index.js
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
	return deepFreeze(structuredClone({
		...input,
		id: brandString(randomUUID())
	}));
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
* Harness error base with a stable machine-routable code and chained cause.
* Package errors extend it so tool results and replay can retain failure class.
* @module @deepseek-ai/dsh-llm/error
*/
/**
* Base class for all harness errors. Carries a `code` (stable, programmatic —
* e.g. `NO_ADAPTER`, `INVALID_ARGS`, `INVARIANT`) distinct from the
* human-readable `message`, and supports `cause` chaining via the standard
* `ErrorOptions`. `name` defaults to the subclass constructor name.
*/
var HarnessError = class extends Error {
	/** Stable machine-routable failure class (e.g. `RATE_LIMIT`); route on this, never by parsing `message`. */
	code;
	constructor(message, code, options) {
		super(message, options);
		this.code = code;
		this.name = new.target.name;
	}
};
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
const DEFAULT_MAX_RETRIES = 5;
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
const NORMAL_POLICY_KEYS = /* @__PURE__ */ new Set([
	"mode",
	"maxRetries",
	"retryableCodes",
	"backoff"
]);
const ALWAYS_POLICY_KEYS = /* @__PURE__ */ new Set([
	"mode",
	"maxRetries",
	"retryableCodes",
	"backoff"
]);
const BACKOFF_KEYS = /* @__PURE__ */ new Set([
	"initialDelayMs",
	"maxDelayMs",
	"jitterRatio"
]);
function validateKeys(value, allowed, path) {
	for (const key of Object.keys(value)) if (!allowed.has(key)) throw new Error(`${path}: unknown key "${key}"`);
}
function resolveBackoff(config, path) {
	if (config !== void 0) validateKeys(config, BACKOFF_KEYS, path);
	const initialDelayMs = config?.initialDelayMs ?? DEFAULT_INITIAL_DELAY_MS;
	const maxDelayMs = config?.maxDelayMs ?? DEFAULT_MAX_DELAY_MS;
	const jitterRatio = config?.jitterRatio ?? DEFAULT_JITTER_RATIO;
	if (!Number.isFinite(initialDelayMs) || initialDelayMs <= 0 || initialDelayMs > 2147483647) throw new Error(`${path}.initialDelayMs must be a positive finite number no greater than ${MAX_TIMER_DELAY_MS}`);
	if (!Number.isFinite(maxDelayMs) || maxDelayMs <= 0 || maxDelayMs > 2147483647) throw new Error(`${path}.maxDelayMs must be a positive finite number no greater than ${MAX_TIMER_DELAY_MS}`);
	if (initialDelayMs > maxDelayMs) throw new Error(`${path}.initialDelayMs must be less than or equal to maxDelayMs`);
	if (!Number.isFinite(jitterRatio) || jitterRatio < 0 || jitterRatio > 1) throw new Error(`${path}.jitterRatio must be between 0 and 1`);
	return Object.freeze({
		initialDelayMs,
		maxDelayMs,
		jitterRatio
	});
}
/**
* Validate, default, and detach one provider-owned retry policy.
* @param config - optional provider configuration; omission selects normal defaults.
* @param path - diagnostic path naming the provider config that owns the value.
* @returns an immutable policy safe to capture in provider registration state.
*/
function resolveRetryPolicy(config, path) {
	if (config === void 0) return Object.freeze({
		mode: "normal",
		maxRetries: DEFAULT_MAX_RETRIES,
		retryableCodes: DEFAULT_RETRYABLE_CODES,
		...resolveBackoff(void 0, `${path}.backoff`)
	});
	switch (config.mode) {
		case "normal": {
			validateKeys(config, NORMAL_POLICY_KEYS, path);
			const maxRetries = config.maxRetries ?? DEFAULT_MAX_RETRIES;
			const retryableCodes = config.retryableCodes ?? [...DEFAULT_RETRYABLE_CODES];
			if (!Number.isSafeInteger(maxRetries) || maxRetries < 0) throw new Error(`${path}.maxRetries must be a non-negative safe integer`);
			if (retryableCodes.length === 0) throw new Error(`${path}.retryableCodes must not be empty`);
			if (retryableCodes.some((code) => typeof code !== "string" || code.length === 0)) throw new Error(`${path}.retryableCodes must contain only non-empty strings`);
			if (new Set(retryableCodes).size !== retryableCodes.length) throw new Error(`${path}.retryableCodes must not contain duplicates`);
			return Object.freeze({
				mode: "normal",
				maxRetries,
				retryableCodes: Object.freeze([...retryableCodes]),
				...resolveBackoff(config.backoff, `${path}.backoff`)
			});
		}
		case "always":
			validateKeys(config, ALWAYS_POLICY_KEYS, path);
			return Object.freeze({
				mode: "always",
				...resolveBackoff(config.backoff, `${path}.backoff`)
			});
		default: throw new Error(`${path}.mode must be "normal" or "always"`);
	}
}
/**
* Field-wise equality over {@link LlmCallConfig} — the comparison a caller
* runs to decide whether a proposed configuration is a real change (worth a
* logged header snapshot) or the held one restated.
* @param a - one configuration.
* @param b - the other.
* @returns whether every field (including the `stop` list, element-wise) matches.
*/
function callConfigEquals(a, b) {
	if (a.provider !== b.provider || a.model !== b.model || a.reasoningEffort !== b.reasoningEffort || a.temperature !== b.temperature || a.maxTokens !== b.maxTokens) return false;
	if (a.stop === void 0 || b.stop === void 0) return a.stop === b.stop;
	return a.stop.length === b.stop.length && a.stop.every((s, i) => s === b.stop?.[i]);
}
/**
* Normalization for values thrown by a final LLM adapter boundary.
*
* @module @deepseek-ai/dsh-llm/adapter-failure
*/
/**
* Detach serializable provider facts from a value thrown by an adapter.
* @param value - arbitrary value thrown during adapter dispatch or iteration.
* @returns immutable provider-neutral facts suitable for a terminal finish chunk.
* @internal
*/
function normalizeLlmFailure(value) {
	const error = value instanceof Error ? value : new HarnessError(thrownMessage(value), "UNKNOWN", { cause: value });
	const carried = ownFailureSnapshot(error);
	if (carried !== void 0 && carried.code === ownErrorCode(error)) return carried;
	return Object.freeze({
		message: errorMessage(error),
		code: harnessErrorCode(error)
	});
}
/** Render a non-Error throw without letting hostile coercion escape normalization. */
function thrownMessage(value) {
	try {
		const message = String(value);
		return message.length > 0 ? message : "LLM adapter failed";
	} catch (_hostileThrownValue) {
		return "LLM adapter failed";
	}
}
/** Read a foreign error's own data-backed `code` without invoking accessors. */
function ownErrorCode(error) {
	try {
		const descriptor = Object.getOwnPropertyDescriptor(error, "code");
		return descriptor !== void 0 && "value" in descriptor ? descriptor.value : void 0;
	} catch (_sdkPropertyTrap) {
		return;
	}
}
/** Snapshot an own data property without invoking an SDK-defined accessor. */
function ownFailureSnapshot(error) {
	try {
		const descriptor = Object.getOwnPropertyDescriptor(error, "failure");
		return descriptor !== void 0 && "value" in descriptor ? failureSnapshot(descriptor.value) : void 0;
	} catch (_sdkPropertyTrap) {
		return;
	}
}
/** Validate and detach an arbitrary serializable failure payload. */
function failureSnapshot(value) {
	if (typeof value !== "object" || value === null) return void 0;
	try {
		const candidate = value;
		const message = candidate.message;
		const code = candidate.code;
		const status = candidate.status;
		const providerRetryAfterMs = candidate.providerRetryAfterMs;
		const requestId = candidate.requestId;
		const offloadImages = candidate.offloadImages;
		if (typeof message !== "string" || message.length === 0 || typeof code !== "string" || code.length === 0 || status !== void 0 && (!Number.isInteger(status) || status < 100 || status > 599) || providerRetryAfterMs !== void 0 && (!Number.isFinite(providerRetryAfterMs) || providerRetryAfterMs <= 0) || requestId !== void 0 && (typeof requestId !== "string" || requestId.length === 0) || offloadImages !== void 0 && (!Number.isSafeInteger(offloadImages) || offloadImages <= 0)) return void 0;
		return Object.freeze({
			message,
			code,
			...status === void 0 ? {} : { status },
			...providerRetryAfterMs === void 0 ? {} : { providerRetryAfterMs },
			...requestId === void 0 ? {} : { requestId },
			...offloadImages === void 0 ? {} : { offloadImages }
		});
	} catch (_sdkFailureGetter) {
		return;
	}
}
/** Read an SDK error message without letting an accessor replace the primary failure. */
function errorMessage(error) {
	try {
		const message = error.message;
		if (typeof message === "string" && message.length > 0) return message;
	} catch (_sdkMessageGetter) {}
	return "LLM adapter failed";
}
/** Trust only Harness-owned codes; third-party SDK codes are not our taxonomy. */
function harnessErrorCode(error) {
	return error instanceof HarnessError ? error.code : "UNKNOWN";
}
function quoted(value) {
	return JSON.stringify(value);
}
/**
* Stable text shown to a model that cannot accept one durable image reference.
* @param ref - durable normalized attachment omitted from the request.
* @returns deterministic text-only placeholder.
*/
function textOnlyImageText(ref) {
	return `[image omitted because this model accepts text only; attachment sha256:${String(ref.attachmentId).slice(7, 15)}]`;
}
/**
* True when typed model content contains an image block. This is the one image
* walk shared by every image policy (capability gating, text-only
* serialization, compaction survey), so a consumer cannot silently diverge.
* @param content - typed model content blocks.
* @returns whether any block is an image.
*/
function contentHasImage(content) {
	return content.some((block) => block.type === "image");
}
/**
* True when typed model content contains a file block.
* Reads current content on every call without retaining scan results.
* @param content - typed model content blocks.
* @returns whether any block is a file.
*/
function contentHasFile(content) {
	for (const block of content) if (block.type === "file") return true;
	return false;
}
/**
* Stable model-facing handle for one durable file reference: the address of
* the verbatim stored copy and the instruction to read it on demand. This is
* the only representation a provider ever receives for a file.
* @param ref - durable verbatim file reference.
* @param readonlyPath - execution-world path of the stored copy, when resolvable.
* @returns deterministic handle text naming the file, its size, and its address.
*/
function fileHandleText(ref, readonlyPath) {
	const digest = String(ref.attachmentId).slice(7, 15);
	const identity = `File ${quoted(ref.name)} (${ref.bytes} bytes, sha256:${digest})`;
	if (readonlyPath === void 0) return `[${identity} was uploaded, but the current execution environment cannot access a readable path. Report that limitation if its contents are needed; do not claim to have read it.]`;
	return `[${identity}: verbatim read-only copy saved at ${quoted(readonlyPath)}. Read that path with your file tools when its contents are needed; copy it to a writable location before modifying it. When delegating file work, include this saved path in the delegation prompt; only subagents sharing this execution environment can read it.]`;
}
/** Replace every file occurrence with handle text. */
function replaceFilesWithHandles(blocks, resolvePath) {
	let next;
	for (const [index, block] of blocks.entries()) {
		if (block.type === "file") {
			next ??= blocks.slice(0, index);
			next.push({
				type: "text",
				text: fileHandleText(block.attachment, resolvePath(block.attachment))
			});
			continue;
		}
		next?.push(block);
	}
	return next ?? blocks;
}
function projectFilesToText(messages, resolvePath) {
	if (!messages.some((message) => contentHasFile(message.content))) return messages;
	return messages.map((message) => {
		const content = replaceFilesWithHandles(message.content, resolvePath);
		return content === message.content ? message : {
			...message,
			content
		};
	});
}
/** Replace every image occurrence for a text-only model. */
function replaceImagesForTextModel(blocks) {
	let next;
	for (const [index, block] of blocks.entries()) {
		if (block.type === "image") {
			next ??= blocks.slice(0, index);
			next.push({
				type: "text",
				text: textOnlyImageText(block.attachment)
			});
			continue;
		}
		next?.push(block);
	}
	return next ?? blocks;
}
function projectImagesForTextModel(messages) {
	if (!messages.some((message) => contentHasImage(message.content))) return messages;
	return messages.map((message) => {
		const content = replaceImagesForTextModel(message.content);
		return content === message.content ? message : {
			...message,
			content
		};
	});
}
function withoutDeveloperMessages(messages) {
	const retained = messages.filter((message) => message.role !== "developer");
	return retained.length === messages.length ? messages : retained;
}
function toolDeclarations(tools, mode, history) {
	const declarations = new Map(history.tools.map((tool) => [tool.name, tool]));
	for (const update of history.updates) for (const tool of update.additions) if (!declarations.has(tool.name)) declarations.set(tool.name, {
		...tool,
		deferLoading: true
	});
	switch (mode) {
		case "in-history": return declarations;
		case "addition-only": {
			const activeNames = new Set(tools?.map((tool) => tool.name));
			for (const name of declarations.keys()) if (!activeNames.has(name)) declarations.delete(name);
			return declarations;
		}
		/* v8 ignore next 2 -- closed-union exhaustiveness guard */
		default: return assertNever(mode);
	}
}
/**
* Construct provider declarations from session-folded history without changing logged active tools.
* Unsupported routes and incomplete history use current declarations without developer updates.
* Explicitly deferred baseline tools become available only after their first retained addition.
* @param messages - complete request inputs, or the prefix selected for an auxiliary call.
* @param tools - currently active tool schemas.
* @param toolUpdate - the resolved route's update mode.
* @param history - immutable state folded from committed headers and developer messages.
* @returns provider declarations and the corresponding filtered history.
*/
function projectToolUpdates(messages, tools, toolUpdate, history) {
	if (toolUpdate === void 0) {
		let immediateTools = tools;
		if (tools?.some((tool) => tool.deferLoading === true)) immediateTools = tools.map(({ deferLoading: _loading, ...tool }) => tool);
		return {
			messages: withoutDeveloperMessages(messages),
			tools: immediateTools
		};
	}
	if (history === void 0) return {
		messages: withoutDeveloperMessages(messages),
		tools
	};
	const messageIds = new Set(messages.flatMap((message) => message.role === "developer" ? [message.id] : []));
	if (history.updates.some((update) => !messageIds.has(update.messageId))) return {
		messages: withoutDeveloperMessages(messages),
		tools
	};
	const declarations = toolDeclarations(tools, toolUpdate, history);
	const updateIds = new Set(history.updates.map((update) => update.messageId));
	const offered = new Set(history.tools.filter((tool) => !tool.deferLoading).map((tool) => tool.name));
	const projectedMessages = [];
	for (const message of messages) {
		if (message.role !== "developer") {
			projectedMessages.push(message);
			continue;
		}
		if (!updateIds.has(message.id)) continue;
		const content = message.content.filter((block) => {
			switch (block.type) {
				case "tool-addition":
					if (!declarations.has(block.toolName) || offered.has(block.toolName)) return false;
					offered.add(block.toolName);
					return true;
				case "tool-removal":
					if (toolUpdate !== "in-history") return false;
					return offered.delete(block.toolName);
				default: return true;
			}
		});
		if (content.length === 0) continue;
		if (content.length === message.content.length) projectedMessages.push(message);
		else projectedMessages.push({
			...message,
			content
		});
	}
	return {
		messages: projectedMessages.length === messages.length && projectedMessages.every((message, index) => message === messages[index]) ? messages : projectedMessages,
		tools: [...declarations.values()]
	};
}
/**
* Centralize the non-secret product identity every provider request sends as `User-Agent`, keeping
* adapters from drifting. See
* `.agents/notes/implemented/architecture/2026-06-21-mandatory-app-attribution-headers.md`.
*
* App-attribution vocabulary for provider requests.
* @module @deepseek-ai/dsh-llm/attribution
*/
const { version } = createRequire(import.meta.url)("../package.json");
/**
* LLM service: adapter registry with a waterfall-interceptable streaming call
* API. Exports the `LlmRuntime` default, the abstract `LlmAdapter` for
* provider backends, and `BlockAssembler` for chunk assembly.
*
* @module @deepseek-ai/dsh-llm
*/
var __runInitializers = function(thisArg, initializers, value) {
	var useValue = arguments.length > 2;
	for (var i = 0; i < initializers.length; i++) value = useValue ? initializers[i].call(thisArg, value) : initializers[i].call(thisArg);
	return useValue ? value : void 0;
};
var __esDecorate = function(ctor, descriptorIn, decorators, contextIn, initializers, extraInitializers) {
	function accept(f) {
		if (f !== void 0 && typeof f !== "function") throw new TypeError("Function expected");
		return f;
	}
	var kind = contextIn.kind, key = kind === "getter" ? "get" : kind === "setter" ? "set" : "value";
	var target = !descriptorIn && ctor ? contextIn["static"] ? ctor : ctor.prototype : null;
	var descriptor = descriptorIn || (target ? Object.getOwnPropertyDescriptor(target, contextIn.name) : {});
	var _, done = false;
	for (var i = decorators.length - 1; i >= 0; i--) {
		var context = {};
		for (var p in contextIn) context[p] = p === "access" ? {} : contextIn[p];
		for (var p in contextIn.access) context.access[p] = contextIn.access[p];
		context.addInitializer = function(f) {
			if (done) throw new TypeError("Cannot add initializers after decoration has completed");
			extraInitializers.push(accept(f || null));
		};
		var result = (0, decorators[i])(kind === "accessor" ? {
			get: descriptor.get,
			set: descriptor.set
		} : descriptor[key], context);
		if (kind === "accessor") {
			if (result === void 0) continue;
			if (result === null || typeof result !== "object") throw new TypeError("Object expected");
			if (_ = accept(result.get)) descriptor.get = _;
			if (_ = accept(result.set)) descriptor.set = _;
			if (_ = accept(result.init)) initializers.unshift(_);
		} else if (_ = accept(result)) if (kind === "field") initializers.unshift(_);
		else descriptor[key] = _;
	}
	if (target) Object.defineProperty(target, contextIn.name, descriptor);
	done = true;
};
/**
* Typed error for LLM-related failures. Extends {@link HarnessError}, so the
* `code` string (e.g. `AUTH`, `RATE_LIMIT`, `NO_ADAPTER`) is shared taxonomy.
*/
var LlmError = class extends HarnessError {
	/** Serializable facts retained beside this live Error. */
	failure;
	/**
	* @param message - non-empty human-readable failure summary.
	* @param code - non-empty stable provider-neutral machine code.
	* @param options - optional cause and validated serializable provider facts.
	*/
	constructor(message, code, options) {
		if (typeof message !== "string" || message.length === 0) throw new Error("LlmError message must be a non-empty string");
		if (typeof code !== "string" || code.length === 0) throw new Error("LlmError code must be a non-empty string");
		if (options?.status !== void 0 && (!Number.isInteger(options.status) || options.status < 100 || options.status > 599)) throw new Error("LlmError status must be an integer from 100 through 599");
		if (options?.providerRetryAfterMs !== void 0 && (!Number.isFinite(options.providerRetryAfterMs) || options.providerRetryAfterMs <= 0)) throw new Error("LlmError providerRetryAfterMs must be a positive finite number");
		if (options?.requestId !== void 0 && (typeof options.requestId !== "string" || options.requestId.length === 0)) throw new Error("LlmError requestId must be a non-empty string");
		super(message, code, options);
		this.name = "LlmError";
		this.failure = Object.freeze({
			message,
			code,
			...options?.status === void 0 ? {} : { status: options.status },
			...options?.providerRetryAfterMs === void 0 ? {} : { providerRetryAfterMs: options.providerRetryAfterMs },
			...options?.requestId === void 0 ? {} : { requestId: options.requestId },
			...options?.offloadImages === void 0 ? {} : { offloadImages: options.offloadImages }
		});
	}
};
(() => {
	let _classSuper = TypertRemoteService;
	let _instanceExtraInitializers = [];
	let _listProviders_decorators;
	let _listConfigurableProviders_decorators;
	let _remoteDiscoverModels_decorators;
	return class LlmRuntime extends _classSuper {
		static {
			const _metadata = typeof Symbol === "function" && Symbol.metadata ? Object.create(_classSuper[Symbol.metadata] ?? null) : void 0;
			_listProviders_decorators = [Remote];
			_listConfigurableProviders_decorators = [Remote];
			_remoteDiscoverModels_decorators = [Remote("discoverModels")];
			__esDecorate(this, null, _listProviders_decorators, {
				kind: "method",
				name: "listProviders",
				static: false,
				private: false,
				access: {
					has: (obj) => "listProviders" in obj,
					get: (obj) => obj.listProviders
				},
				metadata: _metadata
			}, null, _instanceExtraInitializers);
			__esDecorate(this, null, _listConfigurableProviders_decorators, {
				kind: "method",
				name: "listConfigurableProviders",
				static: false,
				private: false,
				access: {
					has: (obj) => "listConfigurableProviders" in obj,
					get: (obj) => obj.listConfigurableProviders
				},
				metadata: _metadata
			}, null, _instanceExtraInitializers);
			__esDecorate(this, null, _remoteDiscoverModels_decorators, {
				kind: "method",
				name: "remoteDiscoverModels",
				static: false,
				private: false,
				access: {
					has: (obj) => "remoteDiscoverModels" in obj,
					get: (obj) => obj.remoteDiscoverModels
				},
				metadata: _metadata
			}, null, _instanceExtraInitializers);
			if (_metadata) Object.defineProperty(this, Symbol.metadata, {
				enumerable: true,
				configurable: true,
				writable: true,
				value: _metadata
			});
		}
		adapters = (__runInitializers(this, _instanceExtraInitializers), /* @__PURE__ */ new Map());
		directory = /* @__PURE__ */ new Map();
		discoveries = /* @__PURE__ */ new Map();
		constructor(ctx) {
			super(ctx, "llm");
		}
		/** Notify topology observers without letting one broken listener veto the commit. */
		emitAdaptersUpdated() {
			let invariantFailure;
			for (const listener of this.ctx.events.dispatch("emit", ["llm/adapters-updated"])) try {
				const returned = listener();
				if (returned != null && typeof returned.then === "function") Promise.resolve(returned).then(void 0, (error) => {
					this.warnAdaptersListenerFailure(error);
				});
			} catch (error) {
				if (error?.code === "INVARIANT") {
					invariantFailure ??= error;
					continue;
				}
				this.warnAdaptersListenerFailure(error);
			}
			if (invariantFailure !== void 0) throw invariantFailure;
		}
		/** Contained-listener diagnostic shared by the sync and async failure paths. */
		warnAdaptersListenerFailure(error) {
			this.ctx.logger.warn("llm: an llm/adapters-updated listener failed");
			this.ctx.logger.warn(error);
		}
		/**
		* Register an adapter for the given provider routes. Throws `LlmError` with code
		* `DUPLICATE_ADAPTER` if any provider already has an adapter (all-or-nothing).
		* Disposed with the fiber.
		* @param providers - every provider route this adapter should serve.
		* @param adapter - the adapter that streams calls for those providers.
		* @returns the disposer, carrying {@link AdapterRegistrationHandle.replace}.
		*/
		registerAdapter(providers, adapter) {
			const owned = /* @__PURE__ */ new Set();
			let released = false;
			const dispose = this.ctx.effect(function* () {
				if (providers.length === 0) throw new LlmError("an adapter must register at least one provider", "INVALID_ADAPTER");
				this.commitRoutes(owned, this.prepareRoutes(providers, adapter, owned));
				yield () => {
					released = true;
					for (const provider of owned) this.adapters.delete(provider);
					owned.clear();
					this.emitAdaptersUpdated();
				};
			}.bind(this), "llm.registerAdapter()");
			const handle = (() => void dispose());
			handle.replace = (next) => {
				if (released) throw new LlmError("a disposed adapter registration cannot replace its routes", "REGISTRATION_DISPOSED");
				this.commitRoutes(owned, this.prepareRoutes(next, adapter, owned));
			};
			return handle;
		}
		/**
		* Validate one candidate route set for `adapter`, treating routes this
		* registration already holds as available. Nothing is mutated: a rejected
		* candidate leaves the registry exactly as it was.
		*/
		prepareRoutes(providers, adapter, owned) {
			const unique = /* @__PURE__ */ new Set();
			const registrations = [];
			for (const provider of providers) {
				if (provider.length === 0) throw new LlmError("adapter provider names must be non-empty", "INVALID_ADAPTER");
				if (unique.has(provider) || this.adapters.has(provider) && !owned.has(provider)) throw new LlmError(`an adapter for provider "${provider}" is already registered`, "DUPLICATE_ADAPTER");
				const info = adapter.providerInfo(provider);
				if (typeof info.id !== "string" || info.id !== provider || typeof info.name !== "string" || info.name.length === 0) throw new LlmError(`adapter metadata for provider "${provider}" must preserve its id and have a non-empty name`, "INVALID_ADAPTER");
				unique.add(provider);
				const retryPolicy = adapter.providerRetryPolicy(provider) ?? resolveRetryPolicy(void 0, `llm: provider "${provider}" retryPolicy`);
				registrations.push({
					adapter,
					provider: {
						id: info.id,
						name: info.name
					},
					retryPolicy
				});
			}
			return registrations;
		}
		/**
		* Swap this registration's routes for the prepared ones in one synchronous
		* section, so no observer can see the registry between the release and the
		* re-registration. The route set's one mutation point is also where
		* `llm/adapters-updated` is published, so a `replace` announces itself
		* exactly like a first registration.
		*/
		commitRoutes(owned, registrations) {
			for (const provider of owned) this.adapters.delete(provider);
			owned.clear();
			for (const registration of registrations) {
				this.adapters.set(registration.provider.id, registration);
				owned.add(registration.provider.id);
			}
			this.emitAdaptersUpdated();
		}
		/**
		* Describe provider routes with a registered adapter.
		* @returns detached provider metadata in registration order.
		*/
		listProviders() {
			return [...this.adapters.values()].map(({ provider }) => ({ ...provider }));
		}
		/**
		* Declare provider routes an adapter plugin can activate through
		* configuration. Registration is all-or-nothing: an empty list, invalid
		* entry, or a provider already declared by any registration throws
		* `LlmError` without registering the rest. Disposed with the fiber.
		* @param entries - every configurable provider this plugin owns.
		* @returns a handle that withdraws all of them, and can atomically replace them.
		*/
		registerConfigurableProviders(entries) {
			let held = [];
			let disposed = false;
			/**
			* Validate a candidate set in full against everything this registration
			* does not already hold, then publish it. Nothing is written until the
			* whole set passes, so a refused candidate leaves the current entries in
			* place — the property that makes `replace` a swap rather than a
			* delete-then-add that can strand the directory empty.
			*/
			const commit = (candidates) => {
				const detached = [];
				const own = new Set(held.map((entry) => entry.provider));
				for (const entry of candidates) {
					if (entry.provider.length === 0 || entry.displayName.length === 0 || entry.settingsNs.length === 0) throw new LlmError("configurable providers need a non-empty provider, displayName, and settingsNs", "INVALID_DIRECTORY");
					if (entry.settingsPath.some((segment) => segment.length === 0)) throw new LlmError(`configurable provider "${entry.provider}" has an empty settingsPath segment`, "INVALID_DIRECTORY");
					if (this.directory.has(entry.provider) && !own.has(entry.provider) || detached.some((seen) => seen.provider === entry.provider)) throw new LlmError(`configurable provider "${entry.provider}" is already declared`, "DUPLICATE_DIRECTORY");
					detached.push({
						...entry,
						settingsPath: [...entry.settingsPath]
					});
				}
				for (const entry of held) this.directory.delete(entry.provider);
				for (const entry of detached) this.directory.set(entry.provider, entry);
				held = detached;
				this.emitAdaptersUpdated();
			};
			const dispose = this.ctx.effect(function* () {
				if (entries.length === 0) throw new LlmError("a configurable-provider registration must declare at least one provider", "INVALID_DIRECTORY");
				commit(entries);
				yield () => {
					disposed = true;
					for (const entry of held) this.directory.delete(entry.provider);
					held = [];
					this.emitAdaptersUpdated();
				};
			}.bind(this), "llm.registerConfigurableProviders()");
			const handle = (() => void dispose());
			handle.replace = (next) => {
				if (disposed) throw new LlmError("this configurable-provider registration was disposed", "REGISTRATION_DISPOSED");
				commit(next);
			};
			return handle;
		}
		/**
		* List every declared configurable provider, registered or dormant.
		* @returns detached directory entries in declaration order.
		*/
		listConfigurableProviders() {
			return [...this.directory.values()].map((entry) => ({
				...entry,
				settingsPath: [...entry.settingsPath]
			}));
		}
		/**
		* Offer to interrogate provider endpoints on behalf of the settings
		* namespace this plugin owns. The namespace is the key because that is what
		* a configuration surface already holds from the configurable-provider
		* directory, and because a provider being *added* has no route to name yet.
		* Disposed with the fiber.
		* @param settingsNs - the namespace whose profiles this discovery serves.
		* @param discover - interrogates one endpoint and must honor the supplied signal.
		* @returns the disposer that withdraws the offer.
		*/
		registerModelDiscovery(settingsNs, discover) {
			const dispose = this.ctx.effect(function* () {
				if (settingsNs.length === 0) throw new LlmError("model discovery needs a non-empty settings namespace", "INVALID_DISCOVERY");
				if (this.discoveries.has(settingsNs)) throw new LlmError(`model discovery for "${settingsNs}" is already registered`, "DUPLICATE_DISCOVERY");
				this.discoveries.set(settingsNs, discover);
				yield () => {
					this.discoveries.delete(settingsNs);
				};
			}.bind(this), "llm.registerModelDiscovery()");
			return () => void dispose();
		}
		/**
		* Interrogate one provider endpoint for the models it advertises. The
		* request describes a draft, not a stored route, so nothing here reads or
		* writes settings or credentials — the caller owns both, and the reply is
		* candidate metadata a surface may offer for adoption.
		* @param settingsNs - namespace whose registered discovery serves this draft.
		* @param request - the endpoint, protocol, and one-shot credential to use.
		* @param signal - caller cancellation.
		* @returns the advertised models, deduplicated in endpoint order.
		*/
		async discoverModels(settingsNs, request, signal) {
			const discover = this.discoveries.get(settingsNs);
			if (discover === void 0) throw new LlmError(`no model discovery is registered for "${settingsNs}"`, "NO_DISCOVERY");
			if ((request.provider ?? "").length === 0 && (request.baseURL ?? "").length === 0) throw new LlmError("model discovery needs a provider route or a baseURL", "INVALID_DISCOVERY");
			const discovered = signal === void 0 ? await discover(request) : await discover(request, signal);
			const seen = /* @__PURE__ */ new Set();
			const models = [];
			for (const model of discovered) {
				if (typeof model.id !== "string" || model.id.length === 0 || seen.has(model.id)) continue;
				seen.add(model.id);
				models.push({
					id: model.id,
					...model.name === void 0 ? {} : { name: model.name },
					...model.contextWindow === void 0 ? {} : { contextWindow: model.contextWindow },
					...model.maxTokens === void 0 ? {} : { maxTokens: model.maxTokens },
					...model.inputModalities === void 0 ? {} : { inputModalities: [...model.inputModalities] }
				});
			}
			return models;
		}
		/**
		* Remote adapter for one draft provider interrogation.
		* @param settingsNs - namespace whose registered discovery serves this draft.
		* @param request - endpoint, protocol, and one-shot credential to use.
		* @param signal - caller cancellation supplied by the Remote carrier.
		* @returns advertised models in endpoint order.
		* @throws RemoteError with `llm/model-discovery-rejected` when discovery refuses or fails.
		*/
		async remoteDiscoverModels(settingsNs, request, signal) {
			try {
				return await this.discoverModels(settingsNs, request, signal);
			} catch (error) {
				throw new RemoteError("llm/model-discovery-rejected", error instanceof Error ? error.message : String(error), {
					settingsNs,
					...request.baseURL === void 0 ? {} : { baseURL: request.baseURL }
				}, { cause: error });
			}
		}
		/**
		* Resolve the retry policy captured when one provider route was registered.
		* @param provider - registered provider route to inspect.
		* @returns the provider-owned policy, with normal defaults already resolved.
		*/
		providerRetryPolicy(provider) {
			return this.registration(provider).retryPolicy;
		}
		/**
		* Resolve provider-side request-image pricing for one exact route, or
		* `undefined` when the provider is unregistered or declares none. Unknown
		* providers degrade to `undefined` rather than throwing because callers
		* price durable history whose route may no longer be mounted.
		* @param provider - provider route named by a request header.
		* @param model - exact model id named by the same header.
		* @returns the owning adapter's image pricing for the route, when declared.
		*/
		imageRequestPricing(provider, model) {
			return this.adapters.get(provider)?.adapter.imageRequestPricing(provider, model);
		}
		/**
		* Resolve the exact text one durable file occurrence contributes to every
		* provider request in the current execution environment.
		* @param ref - durable verbatim file reference from model history.
		* @returns the same deterministic handle text used at adapter dispatch.
		*/
		fileRequestText(ref) {
			return fileHandleText(ref, this.fileReadPath(ref));
		}
		/** Detach typed adapter-owned modality metadata. */
		detachedModalities(modalities) {
			return modalities === void 0 ? void 0 : [...modalities];
		}
		/**
		* Discover models advertised by one registered provider. Catalog membership
		* does not constrain core routing. Catalog-driven entry points may restrict
		* selection and submission to the advertised models.
		* @param provider - registered provider route to inspect.
		* @returns detached model metadata in adapter-preferred order.
		*/
		async listModels(provider) {
			const models = await this.registration(provider).adapter.listModels(provider);
			const seen = /* @__PURE__ */ new Set();
			return models.map((model) => {
				if (typeof model.provider !== "string" || model.provider !== provider || typeof model.id !== "string" || model.id.length === 0 || typeof model.name !== "string" || model.name.length === 0 || model.description !== void 0 && typeof model.description !== "string" || seen.has(model.id)) throw new LlmError(`adapter returned invalid or duplicate model metadata for provider "${provider}"`, "INVALID_CATALOG");
				seen.add(model.id);
				const inputModalities = this.detachedModalities(model.inputModalities);
				return {
					provider: model.provider,
					id: model.id,
					name: model.name,
					...model.description === void 0 ? {} : { description: model.description },
					...inputModalities === void 0 ? {} : { inputModalities }
				};
			});
		}
		/**
		* Resolve and validate all metadata from the adapter that owns one exact
		* route. The result is detached from adapter-owned objects; catalog
		* membership remains advisory and does not control request routing.
		* @param provider - registered provider route to inspect.
		* @param model - exact model id passed to the adapter.
		* @param signal - optional cancellation for adapter-owned asynchronous lookup.
		* @returns exact model identity plus available context and reasoning metadata.
		*/
		async resolveModelInfo(provider, model, signal) {
			return this.resolveModelInfoFor(this.registration(provider), model, signal);
		}
		async resolveModelInfoFor(registration, model, signal) {
			const resolved = await registration.adapter.resolveModel(registration.provider.id, model, signal);
			return this.normalizeModelInfo(registration, model, resolved);
		}
		/** Validate and detach one adapter-returned exact model result. */
		normalizeModelInfo(registration, model, resolved) {
			const provider = registration.provider.id;
			if (typeof resolved.provider !== "string" || resolved.provider !== provider || typeof resolved.id !== "string" || resolved.id !== model || typeof resolved.name !== "string" || resolved.name.length === 0 || resolved.description !== void 0 && typeof resolved.description !== "string") throw new LlmError(`adapter returned invalid exact model metadata for provider "${provider}" model "${model}"`, "INVALID_MODEL_INFO");
			const context = resolved.context;
			if (context !== void 0 && (!Number.isInteger(context.contextWindow) || context.contextWindow <= 0)) throw new LlmError(`adapter returned invalid context metadata for provider "${provider}" model "${model}"`, "INVALID_MODEL_CONTEXT");
			const inputModalities = this.detachedModalities(resolved.inputModalities);
			const systemPromptUpdate = resolved.systemPromptUpdate;
			if (systemPromptUpdate !== void 0 && systemPromptUpdate !== "in-history") throw new LlmError(`adapter returned invalid system prompt update mode for provider "${provider}" model "${model}"`, "INVALID_MODEL_INFO");
			const toolUpdate = resolved.toolUpdate;
			if (toolUpdate !== void 0 && toolUpdate !== "in-history" && toolUpdate !== "addition-only") throw new LlmError(`adapter returned invalid tool update mode for provider "${provider}" model "${model}"`, "INVALID_MODEL_INFO");
			const defaultMaxTokens = resolved.defaultMaxTokens;
			if (defaultMaxTokens !== void 0 && (!Number.isSafeInteger(defaultMaxTokens) || defaultMaxTokens <= 0)) throw new LlmError(`adapter returned invalid default maxTokens for provider "${provider}" model "${model}"`, "INVALID_MODEL_MAX_TOKENS");
			const info = {
				provider,
				id: model,
				name: resolved.name,
				...resolved.description === void 0 ? {} : { description: resolved.description },
				...inputModalities === void 0 ? {} : { inputModalities },
				...context === void 0 ? {} : { context: { contextWindow: context.contextWindow } },
				...defaultMaxTokens === void 0 ? {} : { defaultMaxTokens },
				...resolved.systemPromptUpdate === void 0 ? {} : { systemPromptUpdate: resolved.systemPromptUpdate },
				...resolved.toolUpdate === void 0 ? {} : { toolUpdate: resolved.toolUpdate }
			};
			const reasoning = resolved.reasoning;
			if (reasoning === void 0) return info;
			if (reasoning.efforts.length === 0) throw new LlmError(`adapter returned invalid reasoning metadata for provider "${provider}" model "${model}"`, "INVALID_MODEL_REASONING");
			const seen = /* @__PURE__ */ new Set();
			const efforts = reasoning.efforts.map((effort) => {
				if (typeof effort.id !== "string" || effort.id.length === 0 || typeof effort.name !== "string" || effort.name.length === 0 || effort.description !== void 0 && typeof effort.description !== "string" || seen.has(effort.id)) throw new LlmError(`adapter returned invalid or duplicate reasoning effort metadata for provider "${provider}" model "${model}"`, "INVALID_MODEL_REASONING");
				seen.add(effort.id);
				return {
					id: effort.id,
					name: effort.name,
					...effort.description === void 0 ? {} : { description: effort.description }
				};
			});
			if (reasoning.defaultEffort !== void 0 && !seen.has(reasoning.defaultEffort)) throw new LlmError(`adapter returned an unknown default reasoning effort for provider "${provider}" model "${model}"`, "INVALID_MODEL_REASONING");
			return {
				...info,
				reasoning: {
					efforts,
					...reasoning.defaultEffort === void 0 ? {} : { defaultEffort: reasoning.defaultEffort }
				}
			};
		}
		/**
		* Validate a conversation call config against its exact model capability and
		* materialize adapter-configured defaults. Unsupported explicit efforts
		* reject before provider I/O; no clamping or aliasing is performed. This
		* standalone query does not bind a later dispatch; use {@link prepareCall}
		* when logging and streaming must share one adapter registration.
		* @param config - provider/model route and optional request controls.
		* @param signal - optional cancellation for adapter-owned capability lookup.
		* @returns a detached config only when a default must be materialized.
		*/
		async resolveCallConfig(config, signal) {
			return (await this.resolveCallFor(this.registration(config.provider), config, signal)).config;
		}
		async resolveCallFor(registration, config, signal) {
			const info = await this.resolveModelInfoFor(registration, config.model, signal);
			return this.resolveCallWithInfo(config, info);
		}
		/** Validate request controls against one already-bound exact model result. */
		resolveCallWithInfo(config, info) {
			const defaulted = config.maxTokens === void 0 && info.defaultMaxTokens !== void 0 ? {
				...config,
				maxTokens: info.defaultMaxTokens
			} : config;
			const reasoning = info.reasoning;
			const requested = defaulted.reasoningEffort;
			let resolvedConfig = defaulted;
			if (reasoning === void 0) {
				if (requested !== void 0) throw new LlmError(`provider "${config.provider}" model "${config.model}" does not support reasoning effort "${requested}"`, "UNSUPPORTED_REASONING_EFFORT");
			} else {
				const effective = requested ?? reasoning.defaultEffort;
				if (effective !== void 0) {
					if (!reasoning.efforts.some((effort) => effort.id === effective)) throw new LlmError(`provider "${config.provider}" model "${config.model}" does not support reasoning effort "${effective}"`, "UNSUPPORTED_REASONING_EFFORT");
					if (requested !== effective) resolvedConfig = {
						...defaulted,
						reasoningEffort: effective
					};
				}
			}
			return {
				config: resolvedConfig,
				...info.context === void 0 ? {} : { context: info.context },
				modelInfo: info
			};
		}
		/**
		* Resolve one call under its current adapter registration. The returned
		* one-shot handle keeps that registration across header logging and dispatch,
		* so HMR cannot combine one adapter's capability result with another adapter.
		* @param config - provider/model route and optional request controls.
		* @param signal - optional cancellation for adapter-owned capability lookup.
		* @returns a prepared config and its registration-bound stream entry point.
		*/
		async prepareCall(config, signal) {
			const registration = this.registration(config.provider);
			const adapterCall = await registration.adapter.prepareCall(config.provider, config.model, signal);
			const modelInfo = this.normalizeModelInfo(registration, config.model, adapterCall.model);
			const resolved = this.resolveCallWithInfo(config, modelInfo);
			const resolvedConfig = deepFreeze(structuredClone(resolved.config));
			const context = resolved.context === void 0 ? void 0 : deepFreeze(structuredClone(resolved.context));
			const adapterDefaults = deepFreeze({
				...config.reasoningEffort === void 0 && resolvedConfig.reasoningEffort !== void 0 ? { reasoningEffort: true } : {},
				...config.maxTokens === void 0 && resolvedConfig.maxTokens !== void 0 ? { maxTokens: true } : {}
			});
			let dispatched = false;
			return Object.freeze({
				config: resolvedConfig,
				retryPolicy: registration.retryPolicy,
				adapterDefaults,
				...context === void 0 ? {} : { context },
				...modelInfo.inputModalities === void 0 ? {} : { inputModalities: Object.freeze([...modelInfo.inputModalities]) },
				...modelInfo.systemPromptUpdate === void 0 ? {} : { systemPromptUpdate: modelInfo.systemPromptUpdate },
				...modelInfo.toolUpdate === void 0 ? {} : { toolUpdate: modelInfo.toolUpdate },
				stream: (options) => {
					if (dispatched) throw new LlmError("a prepared LLM call can only be dispatched once", "INVALID_PREPARED_CALL");
					if (!callConfigEquals(options, resolvedConfig)) throw new LlmError("prepared LLM call config changed before adapter dispatch", "INVALID_PREPARED_CALL");
					dispatched = true;
					return this.streamWithRegistration(options, {
						registration,
						config: resolvedConfig,
						modelInfo,
						dispatch: (options) => adapterCall.stream(options)
					});
				}
			});
		}
		registration(provider) {
			const registration = this.adapters.get(provider);
			if (!registration) throw new LlmError(`no adapter registered for provider "${provider}"`, "NO_ADAPTER");
			return registration;
		}
		/** Remove replay state whose historical route is owned by another adapter. */
		forAdapter(options, adapter) {
			const messages = options.messages.map((message) => {
				if (message.role !== "assistant") return message;
				const source = message.source;
				if (source.replayState === void 0) return message;
				if (this.adapters.get(source.provider)?.adapter === adapter) return message;
				return freezeMessage({
					...message,
					source: {
						kind: "model",
						provider: source.provider,
						model: source.model
					}
				});
			});
			if (messages.every((message, index) => message === options.messages[index])) return options;
			const filtered = {
				...options,
				messages
			};
			return Object.isFrozen(options) ? deepFreeze(filtered) : filtered;
		}
		/**
		* Resolve the current execution-world read path of one durable file
		* reference through the mounted attachment and filesystem providers.
		*/
		fileReadPath(ref) {
			let hostPath;
			try {
				hostPath = this.ctx.get("attachments")?.fileHostPath(ref);
			} catch {
				return;
			}
			if (hostPath === void 0) return void 0;
			return this.ctx.get("fs")?.processPathFromHostPath(hostPath);
		}
		/**
		* Final adapter boundary. Adapter selection, dispatch, iterator construction,
		* and iteration failures become one terminal failure chunk. Middleware and
		* downstream consumer failures remain thrown plugin or consumer errors.
		*/
		async *adapterStream(options, prepared) {
			let iterator;
			try {
				const registration = prepared?.registration ?? this.registration(options.provider);
				const adapter = registration.adapter;
				let modelInfo;
				let resolvedConfig;
				let dispatch;
				if (prepared === void 0) {
					const adapterCall = await adapter.prepareCall(options.provider, options.model, options.signal);
					modelInfo = this.normalizeModelInfo(registration, options.model, adapterCall.model);
					resolvedConfig = this.resolveCallWithInfo(options, modelInfo).config;
					dispatch = (options) => adapterCall.stream(options);
				} else {
					modelInfo = prepared.modelInfo;
					resolvedConfig = prepared.config;
					dispatch = prepared.dispatch;
				}
				if (prepared !== void 0 && !callConfigEquals(options, resolvedConfig)) throw new LlmError("prepared LLM call config changed before adapter dispatch", "INVALID_PREPARED_CALL");
				const resolvedOptions = callConfigEquals(options, resolvedConfig) ? options : Object.isFrozen(options) ? deepFreeze({
					...options,
					...resolvedConfig
				}) : {
					...options,
					...resolvedConfig
				};
				let projectedMessages = resolvedOptions.messages;
				if (projectedMessages.some((message) => contentHasFile(message.content))) projectedMessages = projectFilesToText(projectedMessages, (ref) => this.fileReadPath(ref));
				if (modelInfo.inputModalities !== void 0 && !modelInfo.inputModalities.includes("image") && projectedMessages.some((message) => contentHasImage(message.content))) projectedMessages = projectImagesForTextModel(projectedMessages);
				const projectedTools = projectToolUpdates(projectedMessages, resolvedOptions.tools, modelInfo.toolUpdate, resolvedOptions.toolHistory);
				projectedMessages = projectedTools.messages;
				let projectedOptions = resolvedOptions;
				if (projectedMessages !== resolvedOptions.messages || projectedTools.tools !== resolvedOptions.tools) {
					projectedOptions = {
						...resolvedOptions,
						messages: projectedMessages,
						...projectedTools.tools === void 0 ? {} : { tools: projectedTools.tools }
					};
					if (Object.isFrozen(resolvedOptions)) deepFreeze(projectedOptions);
				}
				iterator = dispatch(this.forAdapter(projectedOptions, adapter))[Symbol.asyncIterator]();
			} catch (error) {
				yield adapterFailureChunk(error, options.signal);
				return;
			}
			let completed = false;
			try {
				while (true) {
					let item;
					try {
						const next = await iterator.next();
						item = next.done ? { done: true } : {
							done: false,
							value: next.value
						};
					} catch (error) {
						completed = true;
						yield adapterFailureChunk(error, options.signal);
						return;
					}
					if (item.done) {
						completed = true;
						return;
					}
					yield item.value;
				}
			} finally {
				if (!completed) {
					const close = iterator.return?.bind(iterator);
					if (close) await close();
				}
			}
		}
		/**
		* Stream one model call as raw chunks (token-level deltas). Replay state is
		* retained only when the same adapter instance owns its historical provider
		* and the target provider. Final adapter selection remains fixed through
		* asynchronous exact-model resolution and dispatch. Adapter selection,
		* dispatch, and iteration failures become terminal `error` or `aborted`
		* finish chunks; middleware, nested-call, cleanup, and consumer failures
		* remain thrown.
		* @param options - the full request; `options.provider` selects the adapter.
		* @returns the chunk stream, possibly wrapped by `llm/stream` listeners.
		*/
		stream(options) {
			return this.streamWithRegistration(options);
		}
		streamWithRegistration(options, prepared) {
			return this.ctx.waterfall(this, "llm/stream", options, () => this.adapterStream(options, prepared));
		}
	};
})();
/** Convert one adapter throw into the stream protocol's terminal outcome. */
function adapterFailureChunk(error, signal) {
	const failure = normalizeLlmFailure(error);
	return {
		type: "finish",
		reason: signal?.aborted || failure.code === "ABORTED" ? {
			kind: "aborted",
			failure
		} : {
			kind: "error",
			failure
		}
	};
}
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
/** 拼接事件提醒文本（摘要化，2026-08-20 修复）。
*
* 此前（44a7b85）把每条 care 信号全文 JSON 附在提醒里 → 每次注入都携带完整
* payload，上下文重复膨胀。现改为摘要：只给类型+数量+事件 id，
* agent 需要详情时调 signal_pull（服务端仍是权威源）。
*/
function buildEventNoticeText(events) {
	const care = events.filter((e) => e.type.startsWith("care_"));
	const warn = events.filter((e) => e.type === "anomaly_warn");
	const other = events.filter((e) => !e.type.startsWith("care_") && e.type !== "anomaly_warn");
	const parts = [];
	if (care.length) parts.push(`关怀信号 ${care.length} 条`);
	if (warn.length) parts.push(`异常告警 ${warn.length} 条`);
	if (other.length) parts.push(`其他事件 ${other.length} 条`);
	const head = [
		"【SGME 事件提醒】",
		`有未处理事件（${parts.join("、")}）。`,
		"如需处理请调 signal_pull 拉取详情，按信号消费纪律处理：signal_claim 原子认领 → 主动关怀/处理 → signal_ack 回执。",
		"不阻塞当前任务，处理完即可。"
	].join("\n");
	const eventIds = events.slice(0, 5).map((e) => `${e.type}#${e.event_id}`);
	if (!eventIds.length) return head;
	return head + "\n【事件列表（最多5条，详情请 signal_pull）】\n" + eventIds.join("\n");
}
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
		if (decision.kind === "reject") return decision;
		const unnotified = config.eventSubscriber?.unnotifiedEvents() ?? [];
		if (unnotified.length && injected) {
			const evMsg = createUserMessage({
				content: [{
					type: "text",
					text: buildEventNoticeText(unnotified)
				}],
				source: { kind: "dsh-sgme" }
			});
			if (!decision.messages.some((m) => sameContextPayload(m, evMsg))) {
				ctx.logger.info(`[SGME 事件提醒] 注入 ${unnotified.length} 条事件提醒（step ${payload.step}）`);
				config.eventSubscriber?.markNotified(unnotified.map((e) => e.event_id));
				return {
					kind: "enter",
					messages: [evMsg, ...decision.messages]
				};
			}
		}
		if (injected) return decision;
		if (payload.step !== 1) return decision;
		const projectHint = config.projectHint || process.env.SGME_PROJECT_HINT || (payload.agent?.session?.header?.cwd ? payload.agent.session.header.cwd.split(/[\\/]/).filter(Boolean).pop() : void 0);
		let sceneInjected = false;
		const firstUserText = extractFirstUserText(payload.messages ?? []);
		if (firstUserText) try {
			const sceneSearch = await client.search({
				query: firstUserText,
				scopes: ["memory", "wiki"],
				limit: config.searchLimit
			});
			const scenes = (sceneSearch?.results ?? []).filter((r) => r.source === "wiki" || r.source === "scenes" || r.source === "wiki_scene");
			const memories = (sceneSearch?.results ?? []).filter((r) => r.source === "memory");
			if (scenes.length > 0) {
				const text = buildSceneInjectionText(scenes, memories);
				if (text) {
					profileCache = {
						text,
						ts: Date.now()
					};
					sceneInjected = true;
				}
			}
		} catch (e) {
			ctx.logger.warn(`[SGME 场景检索失败] ${e instanceof Error ? e.message : String(e)}`);
		}
		if (!sceneInjected) {
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
		}
		if (!profileCache) return decision;
		injected = true;
		const unnotifiedFirstTurn = config.eventSubscriber?.unnotifiedEvents() ?? [];
		const injectText = unnotifiedFirstTurn.length ? profileCache.text + "\n\n" + buildEventNoticeText(unnotifiedFirstTurn) : profileCache.text;
		if (unnotifiedFirstTurn.length) config.eventSubscriber?.markNotified(unnotifiedFirstTurn.map((e) => e.event_id));
		const desired = createUserMessage({
			content: [{
				type: "text",
				text: injectText
			}],
			source: { kind: "dsh-sgme" }
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
/** 提取会话首条用户消息文本（T-88 对话内容驱动 query）。
*
* 兼容 dsh 消息结构：content 为字符串或 [{type:'text',text}] 数组；
* 跳过插件注入消息（source.kind==='dsh-sgme'，避免把 SGME 画像当首句）；
* role 存在时仅接受 user。
*/
function extractFirstUserText(messages) {
	for (const m of messages) {
		if (!m || typeof m !== "object") continue;
		const msg = m;
		if (msg.source?.kind === "dsh-sgme") continue;
		const role = msg.role;
		if (role !== void 0 && role !== "user") continue;
		const text = extractMessageText(msg.content);
		if (text) return text.slice(0, 500);
	}
}
/** 从消息 content 提取文本（字符串或 [{type:'text',text}] 数组）。 */
function extractMessageText(content) {
	if (typeof content === "string") return content.trim() || void 0;
	if (Array.isArray(content)) {
		const parts = [];
		for (const c of content) if (c && typeof c === "object") {
			const cc = c;
			if (typeof cc.text === "string") parts.push(cc.text);
		}
		return parts.join(" ").trim() || void 0;
	}
}
/** 拼接场景注入文本（T-88：首句命中 L2 场景时优先注入场景 + 相关记忆）。 */
function buildSceneInjectionText(scenes, memories) {
	const parts = [];
	if (scenes.length > 0) {
		parts.push("--- SGME 相关场景 ---");
		for (const s of scenes.slice(0, 3)) {
			const title = s.title ? `[${s.title}] ` : "";
			const raw = s.content ?? "";
			const truncated = raw.length > 300 ? raw.slice(0, 300) + "…" : raw;
			parts.push(`- ${title}${truncated}`);
		}
	}
	if (memories.length > 0) {
		parts.push("--- 相关记忆 ---");
		parts.push(formatRelatedMemories(memories));
	}
	if (parts.length === 0) return "";
	parts.push("（以上为 SGME 注入的场景与记忆，可直接引用，不必重复询问用户）");
	return parts.join("\n");
}
/** 格式化相关记忆列表。 */
function formatRelatedMemories(results) {
	return results.map((r) => {
		const raw = r.content ?? "";
		const truncated = raw.length > 200 ? raw.slice(0, 200) + "…" : raw;
		return `${r.rank}. ${truncated}`;
	}).join("\n");
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
* - conversation_search：scopes=["sessions"]（T-207 ③：L0 原文正文 FTS 检索）
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
				description: "维度过滤（注册表 id，如 identity/status/focus/goals/ideas；projects/tasks 已裁剪不可用）"
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
* 创建 conversation_search 工具（检索 L0 会话原文，T-207 ③ 对齐 hermes 平级能力）。
*
* scopes=["sessions"]：FTS5 正文检索（raw_fts，命中带 matched_in=body + 命中片段）——
* 用于「那次会话里到底说了什么」的原文回溯（记忆已过期出池 / 需要上下文细节时）。
*/
function createConversationSearchTool(client, defaultLimit) {
	return defineTool({
		name: "conversation_search",
		description: [
			"检索 SGME 的 L0 会话原文（原始对话记录，非提炼记忆）。",
			"用于回溯\"那次会话具体怎么说的\"——记忆池查不到细节、或记忆已过期出池时用。",
			"与 memory_search 互补：memory 是提炼后的事实，conversation_search 是原始正文。"
		].join(" "),
		parameters: {
			query: {
				type: "string",
				required: true,
				description: "检索关键词（正文全文检索）"
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
				scopes: ["sessions"],
				limit: a.limit ?? defaultLimit,
				dimensions: null,
				match: "any"
			});
			if (!resp) return "[conversation_search 失败：SGME Gateway 不可达，稍后重试]";
			if (resp.results.length === 0) return `[conversation_search 无结果：query="${a.query}"]`;
			return formatSearchResults(resp.results);
		}
	});
}
/**
* 创建 wiki_search 工具（检索 wiki 知识库页面，执行通道）。
*
* 走 GET /v1/wiki/search（执行通道，exclude_skill=False），保留 skill 手册——
* 设计 D4/D5 语义「回忆通道不见手册，执行通道专找」。与统一搜索（/v1/search
* 的 wiki_pages 层滤 skill）区分开。
*/
function createWikiSearchTool(client, defaultLimit) {
	return defineTool({
		name: "wiki_search",
		description: [
			"检索 SGME wiki 知识库页面（wiki_pages，含 skill 技能手册——执行通道，不过滤 skill）。",
			"用于查找操作手册/技能手册/经验文档等 wiki 页面正文。",
			"配合 wiki_pages（按分类列目录）/ wiki_page（按 page_id 拉全文）使用。",
			"与 memory_search 互补：memory 是原始记忆，wiki_search 是 wiki 知识库页面。"
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
			const resp = await client.wikiSearch(a.query, a.limit ?? defaultLimit);
			if (!resp) return "[wiki_search 失败：SGME Gateway 不可达，稍后重试]";
			if (resp.results.length === 0) return `[wiki_search 无结果：query="${a.query}"]`;
			return formatWikiSearchResults(resp.results);
		}
	});
}
/** 格式化 /v1/wiki/search 结果（page_id/title/snippet/tags，tags 防御解析 JSON 字符串/数组）。 */
function formatWikiSearchResults(results) {
	return results.map((r, i) => {
		let tagsText = "";
		const tags = r.tags;
		if (Array.isArray(tags)) tagsText = tags.length > 0 ? ` [${tags.join(", ")}]` : "";
		else if (typeof tags === "string" && tags) try {
			const parsed = JSON.parse(tags);
			if (Array.isArray(parsed) && parsed.length > 0) tagsText = ` [${parsed.join(", ")}]`;
		} catch {}
		return `## ${i + 1}. ${r.title}${tagsText}\n${r.snippet}`;
	}).join("\n\n");
}
/**
* 创建 wiki_pages 工具（按分类列出知识库页面，轻量字段）。
*
* W5（方案 v0.3 §5.5）：L2 索引层——模型按 category 发现手册，
* 正文用 wiki_page 二次拉取（渐进式披露）。
*/
function createWikiPagesTool(client, defaultLimit) {
	return defineTool({
		name: "wiki_pages",
		description: [
			"列出 SGME 知识库页面（可按 category 分类过滤，如 skill/sgme 技能手册、design 设计文档）。",
			"返回轻量字段（标题/描述/分类/标签），正文用 wiki_page 按 page_id 拉取。",
			"渐进式披露：先列目录判断加载哪本，再拉全文，避免全量注入。"
		].join(" "),
		parameters: {
			category: {
				type: "string",
				description: "分类过滤（如 skill/sgme、design；省略列出全部）"
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
			const resp = await client.wikiListPages(a.category ?? null, a.limit ?? defaultLimit, 0);
			if (!resp) return "[wiki_pages 失败：SGME Gateway 不可达，稍后重试]";
			if (resp.pages.length === 0) return `[wiki_pages 无结果${a.category ? `：category="${a.category}"` : ""}]`;
			const lines = resp.pages.map((p, i) => {
				const cat = p.category ? ` [${p.category}]` : "";
				const desc = p.description ? ` — ${p.description}` : "";
				return `${i + 1}. ${p.title}${cat}（${p.page_id}）${desc}`;
			});
			return `共 ${resp.total} 页（显示 ${resp.pages.length}）：\n` + lines.join("\n");
		}
	});
}
/**
* 创建 wiki_page 工具（按 page_id 拉取知识库页面全文）。
*
* W5（方案 v0.3 §5.5）：L2 加载层——索引 skill 引导模型用本工具取手册正文执行。
*/
function createWikiPageTool(client) {
	return defineTool({
		name: "wiki_page",
		description: ["按 page_id 拉取 SGME 知识库页面全文（技能手册正文，含 frontmatter 与踩坑记录）。", "page_id 来自 wiki_pages / wiki_search 返回结果。"].join(" "),
		parameters: { page_id: {
			type: "string",
			required: true,
			description: "页面 id（wiki_pages 返回的 page_id）"
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
			const page = await client.wikiGetPage(a.page_id);
			if (!page) return `[wiki_page 失败：页面不存在或 Gateway 不可达（page_id="${a.page_id}"）]`;
			return [
				`# ${page.title}`,
				`page_id: ${page.page_id}`,
				`category: ${page.category ?? "-"}`,
				`tags: ${(page.tags ?? []).join(", ") || "-"}`
			].join("\n") + "\n\n" + (page.content ?? "");
		}
	});
}
/**
* 创建 wiki_page_update 工具（按 page_id 更新知识库页面）。
*
* W5（方案 v0.3 §5.5）：L2 写回层——模型修正手册或追加踩坑记录（PATCH append，默认追加）。
*/
function createWikiPageUpdateTool(client) {
	return defineTool({
		name: "wiki_page_update",
		description: [
			"按 page_id 更新 SGME 知识库页面（PATCH，默认 append=true 追加正文）。",
			"用于修正手册内容、追加踩坑记录或更新元数据（title/category/tags/description/author）。",
			"page_id 来自 wiki_pages / wiki_search 返回结果；append=false 时整体覆盖 content。"
		].join(" "),
		parameters: {
			page_id: {
				type: "string",
				required: true,
				description: "页面 id（wiki_pages 返回的 page_id）"
			},
			content: {
				type: "string",
				required: true,
				description: "要写入的正文内容（append=true 时追加到末尾）"
			},
			append: {
				type: "boolean",
				description: "默认 true 追加"
			},
			author: {
				type: "string",
				description: "作者标识（可选，如 agent 名）"
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
			const resp = await client.wikiUpdatePage(a.page_id, {
				content: a.content,
				append: a.append ?? true,
				author: a.author ?? null
			});
			if (!resp) return `[wiki_page_update 失败：页面不存在或 Gateway 不可达（page_id="${a.page_id}"）]`;
			return `[wiki_page_update 已更新：page_id=${resp.page_id} status=${resp.status}]`;
		}
	});
}
/**
* 创建 wiki_page_add 工具（写入新知识库页面，幂等 upsert）。
*
* W5（方案 v0.3 §5.5）：L2 写回层——模型直接建手册/经验页，
* 同 title+content 重复提交命中同一 page_id 更新（不重复建页）。
*/
function createWikiPageAddTool(client) {
	return defineTool({
		name: "wiki_page_add",
		description: [
			"创建 SGME 知识库页面（直接写入，不走 LLM 提炼；幂等 upsert）。",
			"title/content 必填；category 用 skill/<domain>（技能/手册）或 design（设计方案）。",
			"同 title+content 重复提交命中同一 page_id 更新，不重复建页；写入后立即可被 wiki_search 检索。"
		].join(" "),
		parameters: {
			title: {
				type: "string",
				required: true,
				description: "页面标题（如 \"XXX 操作手册\"）"
			},
			content: {
				type: "string",
				required: true,
				description: "页面正文（markdown）"
			},
			category: {
				type: "string",
				description: "分类（如 skill/sgme、design；可选）"
			},
			tags: {
				type: "string",
				description: "标签，逗号分隔（可选，如 \"sgme,运维,踩坑\"）"
			},
			description: {
				type: "string",
				description: "摘要（索引用，可选）"
			},
			author: {
				type: "string",
				description: "作者标识（可选，如 agent 名）"
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
			const resp = await client.wikiCreatePage({
				title: a.title,
				content: a.content,
				category: a.category ?? null,
				tags: a.tags ? a.tags.split(",").map((t) => t.trim()).filter(Boolean) : null,
				description: a.description ?? null,
				author: a.author ?? null
			});
			if (!resp) return `[wiki_page_add 失败：Gateway 不可达或写入失败（title="${a.title}"）]`;
			return `[wiki_page_add 已写入：page_id=${resp.page_id} status=${resp.status}]`;
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
		const raw = r.content ?? (r.name ? r.description ? `${r.name} — ${r.description}` : r.name : r.description ?? "");
		const content = raw.length > 500 ? raw.slice(0, 500) + "…" : raw;
		lines.push(`## ${r.rank}. [${r.source}]${titlePrefix}${routes}\n${content}`);
	}
	return lines.join("\n\n");
}
/**
* 向 dsh ctx 注册全部工具（检索 + 信号消费 + 三池登记 + 角色 + 记忆纠错 + 技能层 + 运维/写侧）。
*
* 调用方：index.ts apply() 内调用，传入 ctx 和 client。
*/
function registerTools(ctx, client, defaultLimit, eventSubscriber) {
	ctx.tools.register(createMemorySearchTool(client, defaultLimit));
	ctx.tools.register(createConversationSearchTool(client, defaultLimit));
	ctx.tools.register(createWikiSearchTool(client, defaultLimit));
	ctx.tools.register(createWikiPagesTool(client, defaultLimit));
	ctx.tools.register(createWikiPageTool(client));
	ctx.tools.register(createWikiPageUpdateTool(client));
	ctx.tools.register(createWikiPageAddTool(client));
	ctx.tools.register(createInjectTool(client));
	ctx.tools.register(createSignalPullTool(client));
	ctx.tools.register(createSignalClaimTool(client, eventSubscriber ?? null));
	ctx.tools.register(createSignalAckTool(client, eventSubscriber ?? null));
	ctx.tools.register(createIdeaAddTool(client));
	ctx.tools.register(createDemandCreateTool(client));
	ctx.tools.register(createProjectRegisterTool(client));
	ctx.tools.register(createRoleListTool(client));
	ctx.tools.register(createRoleAssembleTool(client));
	ctx.tools.register(createRoleActiveGetTool(client));
	ctx.tools.register(createRoleActiveSetTool(client));
	ctx.tools.register(createMemoryGetTool(client));
	ctx.tools.register(createMemoryRejectTool(client));
	ctx.tools.register(createSkillSearchTool(client, defaultLimit));
	ctx.tools.register(createSkillDigestTool(client));
	ctx.tools.register(createSkillGetTool(client));
	ctx.tools.register(createSkillListTool(client, defaultLimit));
	ctx.tools.register(createSkillColdstartTool(client));
	ctx.tools.register(createAnswerTool(client));
	ctx.tools.register(createHealthTool(client));
	ctx.tools.register(createStatsTool(client));
	ctx.tools.register(createMemoryUnrejectTool(client));
	ctx.tools.register(createSignalClearTool(client));
	ctx.tools.register(createWikiEvolveTriggerTool(client));
	ctx.tools.register(createConfigGetTool(client));
	ctx.tools.register(createConfigUpdateTool(client));
	ctx.tools.register(createRefineStatusTool(client));
	ctx.tools.register(createRefineTriggerTool(client));
	ctx.tools.register(createRefineBatchTool(client));
	ctx.tools.register(createSkillMaterializeTool(client));
	ctx.tools.register(createSkillPutTool(client));
	ctx.tools.register(createSkillDeleteTool(client));
	ctx.tools.register(createSkillRenameTool(client));
}
/** 创建 inject 工具（按场景模式拉取 SGME 画像，agent 主动注入）。 */
function createInjectTool(client) {
	return defineTool({
		name: "inject",
		description: [
			"按场景模式拉取 SGME 画像注入（POST /v1/inject，Agent Key）。",
			"场景模式：daily 日常画像 / coding 编码（技术栈/踩坑/工作方式）/ work 工作（目标/关系）/ full 全量。",
			"低频使用：切换模式会使当轮 DeepSeek 前缀缓存失效（该轮历史输入按未命中计费，约 ¥0.22/次），同一会话内请勿反复切换。"
		].join(" "),
		parameters: { mode: {
			type: "string",
			required: true,
			enum: [
				"daily",
				"full",
				"coding",
				"work"
			],
			description: "画像注入模式模板名"
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
			const profile = await client.inject({ mode: a.mode });
			if (!profile) return "[inject 失败：SGME Gateway 不可达或模式无效，稍后重试]";
			return buildInjectionText(profile, null);
		}
	});
}
/**
* 创建 skill_search 工具（技能检索，先搜后取的第一步）。
*
* 走 POST /v1/search scope=["skills"]（HTTP 侧唯一入口；服务端无 /v1/skills/search）。
* ⚠️ 只返回 name/description/category，**不含正文**——拿到 name 后必须再调
* skill_digest 审核或直接 skill_get 拉全文，不要把 description 当技能内容用。
*/
function createSkillSearchTool(client, defaultLimit) {
	return defineTool({
		name: "skill_search",
		description: [
			"检索 SGME 技能库（BM25+向量融合，全量技能）。需要某项专业能力但不确定 SGME 有没有时必用。",
			"只返回技能名与触发描述，**不含正文**——选定后调 skill_get 拉全文再执行。",
			"范式（SGME 1.1.0）：技能不预载，按需检索→拉全文→注入，不要凭空编造操作步骤。"
		].join(" "),
		parameters: {
			query: {
				type: "string",
				required: true,
				description: "检索关键词或能力描述（如 \"docker 部署\"、\"pdf 提取\"）"
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
			const hits = await client.skillSearch(a.query, a.limit ?? defaultLimit);
			if (!hits) return "[skill_search 失败：SGME Gateway 不可达或技能模块未启用，稍后重试]";
			if (hits.length === 0) return `[skill_search 无结果：query="${a.query}"（可换更宽泛的关键词重试）]`;
			return hits.map((s, i) => `## ${i + 1}. ${s.name}${s.category ? ` [${s.category}]` : ""}\n${s.description}`).join("\n\n") + "\n\n（调 skill_get 传 name 拉全文后执行）";
		}
	});
}
/** 创建 skill_digest 工具（L1 摘要：字段 + 正文骨架 + uses 依赖，审核用）。 */
function createSkillDigestTool(client) {
	return defineTool({
		name: "skill_digest",
		description: ["查看技能摘要（L1）：frontmatter 字段 + 正文小节骨架 + uses 依赖清单。", "用于执行前审核——先看骨架判断是否对症、有没有依赖要一并拉，再决定要不要 skill_get 全文。"].join(" "),
		parameters: { name: {
			type: "string",
			required: true,
			description: "技能名（skill_search 返回，kebab-case）"
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
			const d = await client.skillDigest(a.name);
			if (!d) return `[skill_digest 失败：技能不存在或 Gateway 不可达（name="${a.name}"，先 skill_search 确认）]`;
			const deps = d.uses.length > 0 ? `\n依赖 uses: ${d.uses.join(", ")}` : "";
			const sections = d.sections.length > 0 ? `\n正文骨架:\n${d.sections.map((s) => "  " + s).join("\n")}` : "";
			return [
				`# ${d.name}${d.category ? ` [${d.category}]` : ""}${d.version ? ` v${d.version}` : ""}`,
				d.description,
				deps,
				sections
			].filter(Boolean).join("\n");
		}
	});
}
/** 创建 skill_get 工具（L2 全文：显式注入上下文；section 可只取一节省 token）。 */
function createSkillGetTool(client) {
	return defineTool({
		name: "skill_get",
		description: ["拉取技能全文（L2）并注入上下文——拿到后按其步骤执行，不要凭空编造。", "正文较长时传 section 只取该标题下的内容，省 token（节名不对会 404，先 skill_digest 看骨架确认）。"].join(" "),
		parameters: {
			name: {
				type: "string",
				required: true,
				description: "技能名（skill_search / skill_digest 返回）"
			},
			section: {
				type: "string",
				description: "只取该小节，传纯标题如 \"前置条件\"；带 # 前缀的骨架行会自动剥离，两种写法都行"
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
			const d = await client.skillGet(a.name, a.section ?? null);
			if (!d) return `[skill_get 失败：技能不存在或 Gateway 不可达（name="${a.name}"，先 skill_search 确认）]`;
			const tag = d.truncated_by_section ? `（已按 section="${d.section}" 截取）` : "";
			return `<!-- skill: ${d.name}${tag} -->\n${d.content}`;
		}
	});
}
/** 创建 skill_list 工具（L0 索引列表，分页浏览全量）。 */
function createSkillListTool(client, defaultLimit) {
	return defineTool({
		name: "skill_list",
		description: ["列出 SGME 技能库索引（L0：name/description/category，分页浏览全量）。", "不确定有没有某个技能时用 skill_search 检索更精准；本工具适合浏览摸底。"].join(" "),
		parameters: {
			limit: {
				type: "number",
				description: `返回条数上限（默认 ${defaultLimit}）`
			},
			offset: {
				type: "number",
				description: "分页偏移（默认 0）"
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
			const resp = await client.skillList(a.limit ?? defaultLimit, a.offset ?? 0);
			if (!resp) return "[skill_list 失败：SGME Gateway 不可达或技能模块未启用，稍后重试]";
			if (resp.skills.length === 0) return `[skill_list 无技能（offset=${resp.offset}）]`;
			const lines = resp.skills.map((s) => `- ${s.name}${s.category ? ` [${s.category}]` : ""} — ${s.description.slice(0, 120)}`);
			return [`技能库共 ${resp.total} 个，本次返回 ${resp.returned} 个（offset=${resp.offset}${resp.budget ? `，默认窗口 ${resp.budget}` : ""}）`, ...lines].join("\n");
		}
	});
}
/**
* 创建 skill_coldstart 工具（冷启动包）。
*
* SGME 1.1.0 范式：只返回 1 个《技能检索协议》+ SGME 操作手册，**全量技能不预载**。
* 会话开始调一次即知道「要用技能时先检索、再拉全文」的正确姿势。
*/
function createSkillColdstartTool(client) {
	return defineTool({
		name: "skill_coldstart",
		description: [
			"拉取技能冷启动包（SGME 1.1.0 范式）——仅注入 1 个《技能检索协议》+ SGME 操作手册。",
			"会话开始调一次：协议教你怎么按需检索技能，操作手册讲 SGME 自身怎么用。",
			"全量技能不预载，需要时用 skill_search 检索、skill_get 拉全文。"
		].join(" "),
		parameters: {},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(_args, _exec) {
			const resp = await client.skillColdstart();
			if (!resp) return "[skill_coldstart 失败：SGME Gateway 不可达或技能模块未启用，稍后重试]";
			const parts = [];
			const items = resp.index?.items ?? [];
			for (const s of items) {
				const body = s.content ?? s.description;
				parts.push(`<!-- skill: ${s.name} -->\n${body}`);
			}
			if (resp.manual?.content) parts.push(`<!-- sgme-manual: ${resp.manual.title} -->\n${resp.manual.content}`);
			if (parts.length === 0) return "[skill_coldstart 冷启动包为空]";
			const hotNote = resp.hotset && resp.hotset.length > 0 ? `\n\n（常驻热集：${resp.hotset.map((s) => s.name).join(", ")}）` : "";
			return parts.join("\n\n") + hotNote;
		}
	});
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
function createSignalClaimTool(client, eventSubscriber) {
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
			eventSubscriber?.markConsumed([a.event_id]);
			return claimed ? `[signal_claim 认领成功：event_id=${a.event_id}，请主动关怀用户后调 signal_ack 回执]` : `[signal_claim 已被消费：event_id=${a.event_id}，跳过]`;
		}
	});
}
/** 创建 signal_ack 工具（写消费回执）。 */
function createSignalAckTool(client, eventSubscriber) {
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
			const ok = await client.ackSignal(a.event_id, a.status, a.result);
			if (ok) eventSubscriber?.markConsumed([a.event_id]);
			return ok ? `[signal_ack 已回执：event_id=${a.event_id} status=${a.status}]` : "[signal_ack 失败]";
		}
	});
}
/** 创建 idea_add 工具（创意池：用户主动提出才记录）。 */
function createIdeaAddTool(client) {
	return defineTool({
		name: "idea_add",
		description: ["添加创意到 SGME 创意池（仅当用户主动提出创意时才记录——不要自行发散）。", "创意长期保存（无 TTL）；删除/升格由用户在 WebUI 操作。"].join(" "),
		parameters: {
			content: {
				type: "string",
				required: true,
				description: "创意内容（一句话概括）"
			},
			priority: {
				type: "number",
				description: "优先级 0-100（默认 50）"
			},
			source_ref: {
				type: "string",
				description: "溯源标识（可选，如会话主题）"
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
			const resp = await client.ideaAdd({
				content: a.content,
				priority: a.priority ?? null,
				source_ref: a.source_ref ?? null
			});
			if (!resp) return "[idea_add 失败：SGME Gateway 不可达或无 Admin Key，稍后重试]";
			const id = String(resp.idea?.memory_id ?? "");
			return `[idea_add 已登记${id ? `：memory_id=${id}` : ""}（创意池，长期保存）]`;
		}
	});
}
/** 创建 demand_create 工具（待办池：跨项目统一待办，agent 主动登记）。 */
function createDemandCreateTool(client) {
	return defineTool({
		name: "demand_create",
		description: [
			"登记待办到 SGME 待办池（跨项目统一待办——不管哪个项目的事都收进来）。",
			"会话中遇到用户要办的事/项目任务/后续跟进事项，主动调用本工具登记，不要只留在对话里。",
			"project_id 是自由标记（未登记项目也允许）；完成时由用户在 WebUI 或后续操作标 done。"
		].join(" "),
		parameters: {
			title: {
				type: "string",
				required: true,
				description: "待办标题（一句概括）"
			},
			content: {
				type: "string",
				description: "详情（可选）"
			},
			priority: {
				type: "number",
				description: "优先级 0-100（默认 50）"
			},
			project_id: {
				type: "string",
				description: "关联项目 id（自由标记，可选）"
			},
			source_ref: {
				type: "string",
				description: "溯源标识（可选）"
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
			const resp = await client.demandCreate({
				title: a.title,
				content: a.content ?? null,
				priority: a.priority ?? null,
				project_id: a.project_id ?? null,
				source_ref: a.source_ref ?? null
			});
			if (!resp) return "[demand_create 失败：SGME Gateway 不可达或无 Admin Key，稍后重试]";
			const warn = resp.warnings && resp.warnings.length > 0 ? `（警告：${resp.warnings.join("；")}）` : "";
			return `[demand_create 已登记：demand_id=${resp.demand_id} status=${resp.status}${warn}]`;
		}
	});
}
/** 创建 project_register 工具（项目池：用户主动立项才登记）。 */
function createProjectRegisterTool(client) {
	return defineTool({
		name: "project_register",
		description: ["登记/创建项目到 SGME 项目池（仅当用户主动提出立项/创建时调用；upsert，二次登记=更新）。", "project_id 用纯英文且一律大写；新建时 path 必填。"].join(" "),
		parameters: {
			project_id: {
				type: "string",
				required: true,
				description: "项目 id（纯英文，一律大写，如 DHVS）"
			},
			path: {
				type: "string",
				description: "项目本地路径（新建时必填）"
			},
			name: {
				type: "string",
				description: "项目显示名（可选）"
			},
			git_repo: {
				type: "string",
				description: "git 仓库地址（可选）"
			},
			milestone: {
				type: "string",
				description: "当前里程碑（可选）"
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
			const resp = await client.projectRegister({
				project_id: a.project_id,
				path: a.path ?? null,
				name: a.name ?? null,
				git_repo: a.git_repo ?? null,
				milestone: a.milestone ?? null
			});
			if (!resp) return "[project_register 失败：SGME Gateway 不可达或无 Admin Key，稍后重试]";
			return `[project_register 已登记：project_id=${resp.project_id}]`;
		}
	});
}
/** 创建 role_list 工具（列出可用角色）。 */
function createRoleListTool(client) {
	return defineTool({
		name: "role_list",
		description: ["列出 SGME 可用角色模板（管家/伴侣/朋友/导师，含人设摘要）。", "会话开始（或用户指定角色）时调用；选定后调 role_assemble 拿人设——换皮不换芯，记忆池不动。"].join(" "),
		parameters: {},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(_args, _exec) {
			const resp = await client.roleList();
			if (!resp) return "[role_list 失败：SGME Gateway 不可达，稍后重试]";
			if (resp.roles.length === 0) return "[role_list 无可用角色]";
			const activeId = (await client.roleActiveGet())?.role_id ?? null;
			const lines = resp.roles.map((r, i) => {
				const mark = r.role_id === activeId ? " ←当前" : "";
				const desc = r.description ? ` — ${r.description}` : "";
				return `${i + 1}. ${r.name}（${r.role_id}）${mark}${desc}`;
			});
			return `共 ${resp.total} 个角色${activeId ? `（当前：${activeId}）` : "（未设置）"}：\n` + lines.join("\n");
		}
	});
}
/** 创建 role_assemble 工具（装配角色沟通提示词）。 */
function createRoleAssembleTool(client) {
	return defineTool({
		name: "role_assemble",
		description: ["装配角色沟通提示词（角色卡 system_prompt + persona + 关怀策略 + 可选画像）。", "role_id 来自 role_list；产物直接作为 system prompt 风格指引使用——按角色语气说话，但记忆与事实以记忆池为准。"].join(" "),
		parameters: {
			role_id: {
				type: "string",
				required: true,
				description: "角色 id（role_list 返回）"
			},
			inject_mode: {
				type: "string",
				description: "画像注入模式（可选：daily/full/coding/work；省略不带画像）"
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
			const resp = await client.roleAssemble(a.role_id, a.inject_mode ?? null);
			if (!resp) return `[role_assemble 失败：角色不存在或 Gateway 不可达（role_id="${a.role_id}"，先 role_list 确认）]`;
			return JSON.stringify(resp, null, 2);
		}
	});
}
/** 创建 role_active_get 工具（读取当前角色）。 */
function createRoleActiveGetTool(client) {
	return defineTool({
		name: "role_active_get",
		description: "读取当前沟通角色（未设置返回 role_id=null）。",
		parameters: {},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(_args, _exec) {
			const resp = await client.roleActiveGet();
			if (!resp) return "[role_active_get 失败：SGME Gateway 不可达，稍后重试]";
			return resp.role_id ? `[当前角色：${resp.role_id}${resp.status ? `（${resp.status}）` : ""}]` : "[未设置沟通角色]";
		}
	});
}
/** 创建 role_active_set 工具（设置当前角色）。 */
function createRoleActiveSetTool(client) {
	return defineTool({
		name: "role_active_set",
		description: ["设置当前沟通角色（换皮不换芯：只换沟通外皮，记忆池不动）。", "role_id 必须存在（role_list 可见）；用户要求切换角色时调用。"].join(" "),
		parameters: { role_id: {
			type: "string",
			required: true,
			description: "角色 id（role_list 返回）"
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
			const resp = await client.roleActiveSet(a.role_id);
			if (!resp) return `[role_active_set 失败：角色不存在或 Gateway 不可达（role_id="${a.role_id}"）]`;
			return `[已切换角色：${resp.role_id}]`;
		}
	});
}
/** 创建 memory_get 工具（单条记忆详情）。 */
function createMemoryGetTool(client) {
	return defineTool({
		name: "memory_get",
		description: ["查询单条 SGME 记忆详情（内容/维度/状态 + 溯源 + 归档链）。", "memory_id 来自 memory_search 结果；用于核实记忆准确性。"].join(" "),
		parameters: { memory_id: {
			type: "string",
			required: true,
			description: "记忆 id（memory_search 返回）"
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
			const resp = await client.memoryGet(a.memory_id);
			if (!resp) return `[memory_get 失败：记忆不存在或 Gateway 不可达（memory_id="${a.memory_id}"）]`;
			return JSON.stringify(resp, null, 2);
		}
	});
}
/** 创建 memory_reject 工具（标记记忆不采用）。 */
function createMemoryRejectTool(client) {
	return defineTool({
		name: "memory_reject",
		description: ["标记记忆「不采用」（用户发现记忆有误时调用；不删除、可恢复，之后不再注入/检索）。", "需带纠错理由；幂等（重复调用更新理由）。"].join(" "),
		parameters: {
			memory_id: {
				type: "string",
				required: true,
				description: "记忆 id（memory_search / memory_get 返回）"
			},
			reason: {
				type: "string",
				description: "纠错理由（用户说明的错误原因）"
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
			const resp = await client.memoryReject(a.memory_id, a.reason ?? null);
			if (!resp) return `[memory_reject 失败：记忆不存在或 Gateway 不可达（memory_id="${a.memory_id}"）]`;
			return `[memory_reject 已标记不采用：memory_id=${resp.memory_id}（理由：${resp.reject_reason ?? "用户纠错"}）]`;
		}
	});
}
/**
* 创建 answer 工具（T-149 聚合答案：跨会话计数/列举/时序推理）。
*
* 与 memory_search 的分工：search 返回候选条目让模型自己读；answer 多走一步
* LLM 答案合成，适合「我一共提过几次 X」「什么时候改的 Y」这类聚合问题。
*/
function createAnswerTool(client) {
	return defineTool({
		name: "answer",
		description: [
			"向 SGME 提聚合型问题并直接拿答案（跨会话计数/列举/时序推理）。",
			"适用：「我一共提过几次 X」「Y 是什么时候改的」「列出所有做过 Z 的项目」。",
			"比 memory_search 多一步 LLM 答案合成——纯检索用 memory_search，要结论用本工具。",
			"会消耗一次 LLM 调用；LLM 全链不可用时返回失败提示。"
		].join(" "),
		parameters: {
			query: {
				type: "string",
				required: true,
				description: "自然语言问题"
			},
			question_type: {
				type: "string",
				enum: [
					"temporal",
					"aggregate",
					"generic"
				],
				description: "题型（temporal=时序 / aggregate=计数列举 / generic=通用；省略自动分派）"
			},
			limit: {
				type: "number",
				description: "检索候选条数（默认 8；服务端上限 20）"
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
			const resp = await client.answer(a.query, a.question_type ?? null, a.limit);
			if (!resp) return "[answer 失败：SGME Gateway 不可达、LLM 全链不可用，或模块未启用，稍后重试]";
			const meta = [
				resp.question_type ? `题型=${resp.question_type}` : "",
				resp.candidates_used !== void 0 ? `候选=${resp.candidates_used}` : "",
				resp.provider ? `模型=${resp.provider}` : ""
			].filter(Boolean).join(" ");
			const evidence = Array.isArray(resp.evidence) && resp.evidence.length > 0 ? `\n\n依据（${resp.evidence.length} 条）：\n` + resp.evidence.slice(0, 5).map((e, i) => {
				const c = e.content;
				return `${i + 1}. ${c ? String(c).slice(0, 200) : JSON.stringify(e).slice(0, 200)}`;
			}).join("\n") : "";
			return `${resp.answer ?? "(服务端未返回答案)"}${meta ? `\n\n[${meta}]` : ""}${evidence}`;
		}
	});
}
/** 创建 health 工具（连接/版本/LLM/提炼水位/向量水位自检）。 */
function createHealthTool(client) {
	return defineTool({
		name: "health",
		description: ["SGME 健康自检：服务版本、LLM 可用性、提炼水位与是否停摆、向量库水位。", "排查「记忆检索没结果」「刚说的话没进记忆」时先跑本工具定位是哪一环断了。"].join(" "),
		parameters: {},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(_args, _exec) {
			const h = await client.health();
			if (!h) return "[health 失败：SGME Gateway 不可达——本插件是桥接插件，请确认 SGME 本体在运行]";
			return [
				`status=${h.status} version=${h.version ?? "?"}`,
				`LLM: ${h.llm?.available ? "可用" : "不可用"}（${h.llm?.provider ?? "?"}/${h.llm?.model ?? "?"}）`,
				`提炼: 水位 ${h.refinement?.watermark_age_sec ?? "?"}s 队列 ${h.refinement?.queue_depth ?? "?"} ${h.refinement?.stalled ? "⚠️ 疑似停摆" : "正常"}`,
				`向量: ${h.vector?.available ? "可用" : "不可用"}（记忆 ${h.vector?.memory_vectors ?? "?"} / 场景 ${h.vector?.scene_vectors ?? "?"}）`
			].join("\n");
		}
	});
}
/** 创建 stats 工具（记忆/原始层计数 + 维度分布 + 水位 + 注册 Agent）。 */
function createStatsTool(client) {
	return defineTool({
		name: "stats",
		description: ["SGME 统计概览：记忆总数/归档数、原始文件各状态计数、维度分布、提炼水位、已注册 agent。", "用户问「记忆库现在多少条」「哪些维度用得最多」时用；需 Admin Key。"].join(" "),
		parameters: {},
		output: {
			schema: { type: "string" },
			render: (_args, value) => [{
				type: "text",
				text: value
			}]
		},
		async execute(_args, _exec) {
			const s = await client.stats();
			if (!s) return "[stats 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]";
			const dims = Object.entries(s.dimension_distribution ?? {}).sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 10).map(([k, v]) => `${k}=${v}`).join(", ");
			return [
				`记忆: ${s.memories?.total ?? "?"} 条（归档 ${s.memories?.archived ?? "?"}）`,
				`原始文件: 共 ${s.raw_files?.total ?? "?"}（new ${s.raw_files?.new ?? "?"} / refined ${s.raw_files?.refined ?? "?"} / error ${s.raw_files?.error ?? "?"}）`,
				`提炼水位: ${s.refinement?.last_refined_at ?? "?"}（${s.refinement?.watermark_age_sec ?? "?"}s 前，队列 ${s.refinement?.queue_depth ?? "?"}）`,
				`维度分布: ${dims || "-"}`,
				`已注册 agent: ${(s.agents ?? []).map((a) => `${a.agent_id}(${a.role})`).join(", ") || "-"}`
			].join("\n");
		}
	});
}
/** 创建 memory_unreject 工具（撤销「不采用」，T-163 补齐）。 */
function createMemoryUnrejectTool(client) {
	return defineTool({
		name: "memory_unreject",
		description: ["撤销记忆的「不采用」标记，恢复为 active（重新参与注入与检索）。", "用于 memory_reject 误操作后的恢复；memory_id 来自 memory_search 结果。"].join(" "),
		parameters: { memory_id: {
			type: "string",
			required: true,
			description: "记忆 id（memory_search / memory_get 返回）"
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
			const resp = await client.memoryUnreject(a.memory_id);
			if (!resp) return `[memory_unreject 失败：记忆不存在或 Gateway 不可达（memory_id="${a.memory_id}"）]`;
			return `[memory_unreject 已恢复：memory_id=${resp.memory_id} status=${resp.status}]`;
		}
	});
}
/** 创建 signal_clear 工具（批量清空未消费信号，T-87）。 */
function createSignalClearTool(client) {
	return defineTool({
		name: "signal_clear",
		description: [
			"批量清空 SGME 未消费信号（全部标记已消费，幂等；二次调用 consumed=0）。",
			"用于信号堆积（如历史 anomaly_warn/memory_updated）时的一次性清理。",
			"⚠️ 清空后 pull/SSE 不再推送这些信号——仅在用户明确要求清理时调用。需 Admin Key。"
		].join(" "),
		parameters: {
			signal_type: {
				type: "string",
				description: "只清空该类型（如 anomaly_warn / care_daily）；省略=全部类型"
			},
			subscriber_id: {
				type: "string",
				description: "同步推进该订阅者的持久游标（如 dsh）；省略则不推进"
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
			const resp = await client.signalClear(a.signal_type ?? null, a.subscriber_id ?? null);
			if (!resp) return "[signal_clear 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]";
			return `[signal_clear 已完成：消费 ${resp.consumed} 条（type=${resp.type ?? "全部"}，subscriber=${resp.subscriber_id ?? "-"}）]`;
		}
	});
}
/**
* 创建 wiki_evolve_trigger 工具（自进化 W4）。
*
* session-sync 在 turn/end 后已自动触发（evolveEnabled 默认 true）；
* 本工具用于手动补触发——例如某轮没触发到、或想对指定会话立即提炼经验。
*/
function createWikiEvolveTriggerTool(client) {
	return defineTool({
		name: "wiki_evolve_trigger",
		description: [
			"手动触发 SGME 自进化（会话经验 → 写回 wiki 手册）。",
			"插件每轮结束已自动触发，本工具用于手动补触发（如指定某会话立即提炼）。",
			"服务端有费用门禁与规则闸门兜底（消息块不足会跳过），但仍会计入 LLM 调用。"
		].join(" "),
		parameters: {
			session_key: {
				type: "string",
				description: "指定会话（如 dsh-<会话id>）；省略则由服务端按游标处理"
			},
			min_rounds: {
				type: "number",
				description: "费用门禁：会话消息块下限（默认 5）"
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
			const resp = await client.evolveTrigger(a.session_key ?? null, a.min_rounds ?? 5);
			if (!resp) return "[wiki_evolve_trigger 失败：SGME Gateway 不可达或自进化模块未启用，稍后重试]";
			return `[wiki_evolve_trigger 已触发：status=${resp.status}]`;
		}
	});
}
/** 创建 config_get 工具（读服务端运行时配置）。 */
function createConfigGetTool(client) {
	return defineTool({
		name: "config_get",
		description: ["读取 SGME 服务端运行时配置（整体读或按段读：l1/l2/refine/search/backup 等）。", "用于核实服务端实际生效的配置值（如提炼开关、检索参数）。需 Admin Key。"].join(" "),
		parameters: { section: {
			type: "string",
			description: "配置段名（l1/l2/refine/search/backup）；省略返回全部配置"
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
			const resp = await client.configGet(a.section ?? null);
			if (!resp) return `[config_get 失败：SGME Gateway 不可达、未配置 Admin Key，或段名不存在${a.section ? `（section="${a.section}"）` : ""}]`;
			const writable = resp.writable_sections?.length ? `\n\n可写段：${resp.writable_sections.join(", ")}` : "";
			return JSON.stringify(resp.config ?? resp, null, 2) + writable;
		}
	});
}
/**
* 创建 config_update 工具（改服务端运行时配置）。
*
* ⚠️ 破坏面最大的工具：热生效 + 落盘，改错会直接改变记忆引擎的运行行为。
* 描述里显式加护栏，且要求 section 必填（避免整段误覆盖）。
*/
function createConfigUpdateTool(client) {
	return defineTool({
		name: "config_update",
		description: [
			"更新 SGME 服务端配置段（部分更新，合并后落盘并热生效）。",
			"⚠️ 会改变记忆引擎的实际运行行为（如提炼开关、检索参数）且立即生效。",
			"仅在用户明确要求修改服务端配置时调用；不确定当前值先用 config_get 读。需 Admin Key。"
		].join(" "),
		parameters: {
			section: {
				type: "string",
				required: true,
				description: "要更新的配置段名（l1/l2/refine/search/backup）"
			},
			values: {
				type: "object",
				required: true,
				additionalProperties: true,
				description: "该段的键值对（只传要改的键，未传的保留）"
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
			const resp = await client.configUpdate(a.section, a.values ?? {});
			if (!resp) return `[config_update 失败：SGME Gateway 不可达、未配置 Admin Key，或段名/取值非法（section="${a.section}"）]`;
			return `[config_update 已生效：section=${resp.section ?? a.section} status=${resp.status}]`;
		}
	});
}
/**
* 创建 refine_status 工具（提炼监控）。
*
* 服务端 refine_status 只有 MCP 侧（无 HTTP 端点），故此处以
* GET /v1/admin/refine_runs + 提炼水位组合近似——结论等价，
* 待服务端补 GET /v1/admin/refine/status 后可收敛为单次调用（已登记待办）。
*/
function createRefineStatusTool(client) {
	return defineTool({
		name: "refine_status",
		description: ["查看 SGME 提炼状态：最近提炼批次记录（含 error/running）+ 待提炼队列与水位。", "用于排查「会话入库了但没变成记忆」——看队列是否堆积、最近批次是否报错。需 Admin Key。"].join(" "),
		parameters: {
			limit: {
				type: "number",
				description: "返回最近批次条数（默认 10）"
			},
			status: {
				type: "string",
				enum: [
					"running",
					"ok",
					"error"
				],
				description: "只看该状态的批次（省略=全部，含 error/running）"
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
			const runs = await client.refineRuns({
				limit: a.limit ?? 10,
				status: a.status ?? null
			});
			if (!runs) return "[refine_status 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]";
			const lines = (runs.items ?? []).map((r, i) => {
				const fileId = String(r.file_id ?? "?");
				const status = String(r.status ?? "?");
				const stage = String(r.stage ?? "-");
				const started = String(r.started_at ?? "?");
				return `${i + 1}. [${status}] ${stage} ${fileId}（${started}）`;
			});
			return `提炼记录：共 ${runs.total} 条，本次返回 ${runs.count} 条` + (lines.length > 0 ? "\n" + lines.join("\n") : "\n（无记录）");
		}
	});
}
/**
* 创建 refine_trigger 工具（同步触发提炼）。
*
* ⚠️ 同步阻塞且真实消耗 LLM 额度：批量场景应走 refine_batch（异步排队即返）。
*/
function createRefineTriggerTool(client) {
	return defineTool({
		name: "refine_trigger",
		description: [
			"同步触发提炼：指定 file_id 提炼单个会话原文，或扫 status=new 批量提炼。",
			"⚠️ 同步阻塞直到完成，且真实消耗 LLM 额度；批量任务优先用 refine_batch（异步）。",
			"仅在用户明确要求立即提炼时调用。需 Admin Key。"
		].join(" "),
		parameters: {
			file_id: {
				type: "string",
				description: "单个会话原文 id；省略则批量扫 status=new"
			},
			limit: {
				type: "number",
				description: "批量上限（默认 100）"
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
			const resp = await client.refineTriggerSync(a.file_id ?? null, a.limit);
			if (!resp) return `[refine_trigger 失败：SGME Gateway 不可达、未配置 Admin Key，或 file_id 不存在${a.file_id ? `（"${a.file_id}"）` : ""}]`;
			if (resp.triggered === "file") return `[refine_trigger 单文件完成：file_id=${resp.file_id} status=${resp.status ?? "?"} 记忆 ${resp.memories_count ?? "?"} 条${resp.error ? ` 错误=${resp.error}` : ""}]`;
			return `[refine_trigger 批量完成：处理 ${resp.processed ?? "?"} 个文件，共产出记忆 ${resp.total_memories ?? "?"} 条]`;
		}
	});
}
/** 创建 refine_batch 工具（异步批量提炼，排队即返）。 */
function createRefineBatchTool(client) {
	return defineTool({
		name: "refine_batch",
		description: ["异步批量提炼：后台线程执行，立即返回排队结果（不阻塞对话）。", "⚠️ 会真实消耗 LLM 额度；仅在用户明确要求补提炼时调用。失败由服务端批扫兜底。需 Admin Key。"].join(" "),
		parameters: {
			file_id: {
				type: "string",
				description: "只提炼该文件；省略则批量扫 status=new"
			},
			limit: {
				type: "number",
				description: "批量上限（默认 100）"
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
			const resp = await client.triggerRefine({
				file_id: a.file_id ?? null,
				...a.limit !== void 0 ? { limit: a.limit } : {}
			});
			if (!resp) return "[refine_batch 失败：SGME Gateway 不可达或未配置 Admin Key，稍后重试]";
			return `[refine_batch 已排队：status=${resp.status}（后台执行，可用 refine_status 查看进度）]`;
		}
	});
}
/** 创建 skill_materialize 工具（L3：字节保真落盘成真文件）。 */
function createSkillMaterializeTool(client) {
	return defineTool({
		name: "skill_materialize",
		description: [
			"把 SGME 技能物化成真文件：字节保真写盘 dest_dir/<name>/SKILL.md，返回路径与 sha256。",
			"⚠️ 落盘发生在 SGME **服务端**：dest_dir 是服务端可写路径、返回的 path 也是服务端路径。",
			"SGME 与 agent 同机部署时可直接读该文件；跨机（如 agent 在 PC、SGME 在 NAS 容器）时",
			"agent 本地拿不到产物，需两端共享挂载该目录才能访问——此时请改用 skill_get 取正文。"
		].join(" "),
		parameters: {
			name: {
				type: "string",
				required: true,
				description: "技能名（skill_search / skill_list 返回，kebab-case）"
			},
			dest_dir: {
				type: "string",
				required: true,
				description: "目标目录（技能会写到 <dest_dir>/<name>/SKILL.md）"
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
			const resp = await client.skillMaterialize(a.name, a.dest_dir);
			if (!resp) return `[skill_materialize 失败：技能不存在、dest_dir 非法或 Gateway 不可达（name="${a.name}"）]`;
			return `[skill_materialize 已落盘（服务端路径）：${resp.path}\nsha256=${resp.sha256}]`;
		}
	});
}
/**
* 创建 skill_put 工具（写入/覆盖技能）。
*
* ⚠️ 服务端会走 lint 门禁 + 三层查重，通过后落盘并 git commit 到技能源仓——
* 属写侧治理动作，护栏写进描述。
*/
function createSkillPutTool(client) {
	return defineTool({
		name: "skill_put",
		description: [
			"写入/覆盖 SGME 技能（content 传 SKILL.md 全文，服务端自动解析 frontmatter）。",
			"⚠️ 服务端过 lint 门禁 + 三层查重后落盘并提交技能源仓（同名同内容/同内容异名会 409 拒绝）。",
			"仅在用户明确要求沉淀技能时调用；写入前建议先 skill_search 查重。需 Admin Key。",
			"正文有 8K 上限（超限会被 lint 拦截，历史存量入库可传 skip_limits）。"
		].join(" "),
		parameters: {
			name: {
				type: "string",
				required: true,
				description: "技能名（kebab-case）"
			},
			content: {
				type: "string",
				required: true,
				description: "SKILL.md 全文（含 frontmatter）"
			},
			skip_limits: {
				type: "boolean",
				description: "超限从拒绝降为警告（仅历史存量整体入库用，默认 false）"
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
			if (!await client.skillPut(a.name, a.content, a.skip_limits ?? false)) return `[skill_put 失败：lint 门禁拦截 / 查重拒绝 / 未配置 Admin Key / Gateway 不可达（name="${a.name}"）]`;
			return `[skill_put 已写入：name=${a.name}（落盘并提交技能源仓）]`;
		}
	});
}
/** 创建 skill_delete 工具（删除技能，默认软删）。 */
function createSkillDeleteTool(client) {
	return defineTool({
		name: "skill_delete",
		description: [
			"删除 SGME 技能：默认软删（标记 deprecated，可恢复）；hard=true 物理删目录。",
			"⚠️ 有入向 uses 引用时服务端会 409 拒绝，需 force=true 强制——属破坏性操作。",
			"仅在用户明确要求删除时才调用。需 Admin Key。"
		].join(" "),
		parameters: {
			name: {
				type: "string",
				required: true,
				description: "技能名"
			},
			hard: {
				type: "boolean",
				description: "物理删除（默认 false=软删标记 deprecated）"
			},
			force: {
				type: "boolean",
				description: "强制清理入向引用后删除（默认 false）"
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
			if (!await client.skillDelete(a.name, a.hard ?? false, a.force ?? false)) return `[skill_delete 失败：技能不存在、存在入向引用且未 force、未配置 Admin Key 或 Gateway 不可达（name="${a.name}"）]`;
			return `[skill_delete 已完成：name=${a.name}（${a.hard ? "物理删除" : "软删 deprecated"}）]`;
		}
	});
}
/** 创建 skill_rename 工具（墓碑制改名）。 */
function createSkillRenameTool(client) {
	return defineTool({
		name: "skill_rename",
		description: [
			"技能改名（墓碑制：写新名副本 + 旧位置留 superseded_by 墓碑 + 登记 tombstones.json，永不原地改名）。",
			"⚠️ 需服务端 skills.source_dirs 指向 git 技能仓；新名已占用或过不了门禁会被拒。",
			"仅在用户明确要求改名时调用。需 Admin Key。"
		].join(" "),
		parameters: {
			name: {
				type: "string",
				required: true,
				description: "旧技能名"
			},
			new_name: {
				type: "string",
				required: true,
				description: "新技能名（kebab-case）"
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
			if (!await client.skillRename(a.name, a.new_name)) return `[skill_rename 失败：旧名不存在 / 新名被占用 / 未配置 source_dirs / Gateway 不可达（"${a.name}" → "${a.new_name}"）]`;
			return `[skill_rename 已完成：${a.name} → ${a.new_name}（旧位置留墓碑）]`;
		}
	});
}
//#endregion
//#region src/commands.ts
/**
* 构建 /sgme status 状态报告（连接自检：health + key 配置 + 记忆水位）。
*
* 不可达时给出桥接插件定位与本体安装指引（防止「装了插件没记忆功能」困惑）。
*/
async function buildStatusReport(client, config) {
	const health = await client.health();
	if (!health) return {
		kind: "error",
		text: [
			"[/sgme status] SGME Gateway 不可达",
			"",
			"baseUrl: " + (config.baseUrl ?? "(未知)"),
			"agent key: " + (config.agentKeySet ? "已配置" : "未配置"),
			"admin key: " + (config.adminKeySet ? "已配置" : "未配置"),
			"",
			"本插件是桥接插件，依赖 SGME 本体（Python 服务 :9910）才能工作，没有本体是空壳。",
			"安装指引见插件 README「前置条件」：https://github.com/freehul/sgme"
		].join("\n")
	};
	return {
		kind: "success",
		text: [
			"[/sgme status]",
			"- 连接: 正常" + (health.version ? "（v" + health.version + "）" : ""),
			"- baseUrl: " + (config.baseUrl ?? "?"),
			"- agent key: " + (config.agentKeySet ? "已配置" : "未配置"),
			"- LLM: " + (health.llm?.model ?? "未知") + "（" + (health.llm?.available ? "可用" : "不可用") + "）",
			"- 提炼: 水位 " + (health.refinement?.watermark_age_sec ?? "?") + "s / 队列 " + (health.refinement?.queue_depth ?? "?") + (health.refinement?.stalled ? "（停摆!）" : ""),
			"- 记忆向量: " + (health.vector?.memory_vectors ?? "?")
		].join("\n")
	};
}
/**
* 执行 /sgme 检索，返回 dsh CommandResult。
*
* @param query 用户输入的检索关键词（/sgme 后的参数）
*/
async function executeSgmeCommand(client, config, query) {
	const trimmed = query.trim();
	if (!trimmed || trimmed === "status") return buildStatusReport(client, config);
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
		const raw = r.content ?? (r.name ? r.description ? `${r.name} — ${r.description}` : r.name : r.description ?? "");
		const content = raw.length > 500 ? raw.slice(0, 500) + "…" : raw;
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
		description: "SGME 状态/检索（无参数或 status = 连接自检；<关键词> = 记忆+知识库检索）",
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
		if (config.evolveEnabled !== false) {
			const evolveResp = await client.evolveTrigger(sessionKey, config.evolveMinRounds ?? 5);
			if (!evolveResp) ctx.logger.warn(`[SGME session-sync] 自进化触发失败：session=${sessionKey}`);
			else ctx.logger.info(`[SGME session-sync] 自进化已触发：${sessionKey} status=${evolveResp.status}`);
		}
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
//#region src/events.ts
/**
* events.ts — SGME 事件流订阅（SSE 长连，2026-08-18 用户选方案 2）
*
* 目标：DSH 常驻时实时接收 SGME 主动事件（care_* 关怀 / anomaly_warn 异常 /
* memory_updated），缓存到内存 + 本地文件，下一轮对话由 context.ts 注入提醒，
* agent 再调 signal_pull 消费（claim → 关怀 → ack）。
*
* 可靠性：
* - 断线重连：指数退避 1s → 30s 封顶；重连请求带 Last-Event-ID（SGME 断线补偿）
* - 事件持久化：~/.sgme/event-queue-<agentId>.json（进程重启不丢）
* - 故障隔离：任何异常只 log，绝不阻塞 dsh 主循环
*/
/** 重连退避参数（毫秒）。 */
const RETRY_BASE_MS = 1e3;
const RETRY_MAX_MS = 3e4;
/**
* SSE 订阅器。
*
* - start()：建立长连（fetch + ReadableStream 解析 SSE），断线自动重连
* - pendingEvents()：未消费事件（内存队列 + 文件恢复）
* - markConsumed(ids)：事件消费后标记（agent 调 signal_pull 消费时同步）
* - stop()：断开（插件卸载时调用）
*/
var SgmeEventSubscriber = class {
	config;
	aborter = null;
	retryTimer = null;
	retryMs = RETRY_BASE_MS;
	stopped = false;
	lastEventId = "";
	queue = [];
	consumedIds = /* @__PURE__ */ new Set();
	notifiedIds = /* @__PURE__ */ new Set();
	queuePath;
	constructor(config) {
		this.config = config;
		this.queuePath = join(homedir(), ".sgme", `event-queue-${config.agentId}.json`);
		this.restoreQueue();
	}
	/** 启动订阅（幂等；已在连则忽略）。 */
	start() {
		if (this.aborter || this.stopped) return;
		this.stopped = false;
		this.connect();
	}
	/** 停止订阅（插件卸载）。 */
	stop() {
		this.stopped = true;
		if (this.aborter) this.aborter.abort();
		this.aborter = null;
		if (this.retryTimer) {
			clearTimeout(this.retryTimer);
			this.retryTimer = null;
		}
	}
	/** 未消费事件（供 context.ts 注入提醒）。 */
	pendingEvents() {
		return this.queue.filter((e) => !this.consumedIds.has(e.event_id));
	}
	/** 未消费且未提醒过的事件（context.ts 注入提醒的唯一来源）。
	*
	* 2026-08-20 修复（上下文爆增根因）：此前 context 用 pendingEvents() 判断，
	* 未消费事件每轮重复注入 → 上下文持续膨胀。引入 notifiedIds：
	* 同一事件只提醒一次，之后即使未消费也不再重复注入。
	*/
	unnotifiedEvents() {
		return this.queue.filter((e) => !this.consumedIds.has(e.event_id) && !this.notifiedIds.has(e.event_id));
	}
	/** 标记事件已提醒（防重复注入）。 */
	markNotified(eventIds) {
		for (const id of eventIds) this.notifiedIds.add(id);
		this.persistQueue();
	}
	/** 标记事件已消费（agent 消费后调用，防重复提醒）。 */
	markConsumed(eventIds) {
		for (const id of eventIds) this.consumedIds.add(id);
		this.persistQueue();
	}
	/** 建立 SSE 连接（一次）；断线/错误时按退避重连。 */
	async connect() {
		if (this.stopped) return;
		const { baseUrl, agentKey, agentId } = this.config;
		const url = `${baseUrl.replace(/\/$/, "")}/v1/events/stream?subscriber_id=${encodeURIComponent(agentId)}`;
		const headers = {};
		if (agentKey) headers["X-API-Key"] = agentKey;
		if (this.lastEventId) headers["Last-Event-ID"] = this.lastEventId;
		const aborter = new AbortController();
		this.aborter = aborter;
		try {
			const resp = await fetch(url, {
				headers,
				signal: aborter.signal
			});
			if (!resp.ok) throw new Error(`SSE HTTP ${resp.status}`);
			this.retryMs = RETRY_BASE_MS;
			if (!resp.body) throw new Error("SSE 无响应体");
			const reader = resp.body.getReader();
			const decoder = new TextDecoder();
			let buffer = "";
			for (;;) {
				const { done, value } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				let nl;
				while ((nl = buffer.indexOf("\n")) >= 0) {
					const line = buffer.slice(0, nl).trim();
					buffer = buffer.slice(nl + 1);
					if (line.startsWith("data:")) {
						const data = line.slice(5).trim();
						if (data) this.handleEvent(data);
					} else if (line.startsWith("id:")) this.lastEventId = line.slice(3).trim();
				}
			}
			throw new Error("SSE 流结束");
		} catch (err) {
			if (this.stopped) return;
			const msg = err instanceof Error ? err.message : String(err);
			this.retryTimer = setTimeout(() => {
				this.retryMs = Math.min(this.retryMs * 2, RETRY_MAX_MS);
				this.connect();
			}, this.retryMs);
			console.warn(`[dsh-sgme] 事件流断开（${msg}），${this.retryMs / 1e3}s 后重连`);
		} finally {
			if (this.aborter === aborter) this.aborter = null;
		}
	}
	/** 处理一条 SSE 事件（入队 + 持久化）。 */
	handleEvent(data) {
		try {
			const ev = JSON.parse(data);
			if (!ev || !ev.event_id || !ev.type) return;
			if (this.queue.some((e) => e.event_id === ev.event_id)) return;
			this.queue.push(ev);
			this.persistQueue();
		} catch {}
	}
	/** 队列持久化（~/.sgme/event-queue-<agentId>.json）。 */
	persistQueue() {
		try {
			const dir = join(homedir(), ".sgme");
			mkdirSync(dir, { recursive: true });
			writeFileSync(this.queuePath, JSON.stringify({
				queue: this.queue.slice(-200),
				consumedIds: [...this.consumedIds].slice(-500),
				notifiedIds: [...this.notifiedIds].slice(-500)
			}), "utf-8");
		} catch (err) {
			console.warn("[dsh-sgme] 事件队列持久化失败:", err instanceof Error ? err.message : err);
		}
	}
	/** 进程启动时从文件恢复队列。 */
	restoreQueue() {
		try {
			if (!existsSync(this.queuePath)) return;
			const data = JSON.parse(readFileSync(this.queuePath, "utf-8"));
			if (Array.isArray(data.queue)) this.queue = data.queue;
			if (Array.isArray(data.consumedIds)) this.consumedIds = new Set(data.consumedIds);
			if (Array.isArray(data.notifiedIds)) this.notifiedIds = new Set(data.notifiedIds);
		} catch {}
	}
};
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
	baseUrl: Schema.string().default("http://localhost:9910").description("SGME Gateway 地址（本机部署默认 localhost；SGME 在其他机器请改成对应 IP）"),
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
	turnBatchSize: Schema.number().default(1).description("入库攒批大小（v1=1 即每 turn 即 append）"),
	evolveEnabled: Schema.boolean().default(true).description("自进化自动触发（W4：turn/end 后调 /v1/wiki/evolve/trigger，evolve 侧幂等+费用门禁兜底）"),
	evolveMinRounds: Schema.number().default(5).description("自进化费用门禁：会话消息块下限"),
	eventSubscribe: Schema.boolean().default(true).description("SGME 事件流订阅（SSE 长连，实时接收 care_*/anomaly_warn，注入提醒）")
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
	const eventSubscriber = config.eventSubscribe !== false ? new SgmeEventSubscriber({
		baseUrl: config.baseUrl,
		agentKey: config.agentKey,
		agentId: config.agentId
	}) : null;
	if (eventSubscriber) {
		eventSubscriber.start();
		ctx.effect(() => () => {
			eventSubscriber.stop();
		}, "sgme-event-subscribe");
		logger.info(`SGME 事件订阅已启动（SSE: ${config.baseUrl}/v1/events/stream）`);
	}
	registerTools({ tools: ctx.tools }, client, config.searchLimit, eventSubscriber);
	logger.info("工具已注册（39）：memory_search/answer/memory_get/memory_reject/memory_unreject, wiki_search/pages/page/page_add/page_update, inject, signal_pull/claim/ack/clear, idea_add/demand_create/project_register, role_list/assemble/active, skill_search/digest/get/list/coldstart/materialize/put/delete/rename, health/stats/config_get/config_update/refine_status/refine_trigger/refine_batch/wiki_evolve_trigger");
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
		eventSubscriber,
		...projectHint ? { projectHint } : {}
	});
	ctx.effect(() => disposeContext, "sgme-context-injection");
	registerSgmeCommand({ commands: ctx.commands }, client, {
		searchLimit: config.searchLimit,
		baseUrl: config.baseUrl,
		agentKeySet: !!config.agentKey,
		adminKeySet: !!config.adminKey
	});
	logger.info("命令已注册：/sgme（status 自检 + 检索）");
	const disposeSync = registerSessionSync({
		on: ctx.on,
		logger: {
			info: logger.info,
			warn: logger.warn
		}
	}, client, {
		agentId: config.agentId,
		syncOnTurnEnd: config.syncOnTurnEnd,
		turnBatchSize: config.turnBatchSize,
		...config.evolveEnabled !== void 0 ? { evolveEnabled: config.evolveEnabled } : {},
		...config.evolveMinRounds !== void 0 ? { evolveMinRounds: config.evolveMinRounds } : {}
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
	client.health().then((h) => {
		if (h) logger.info("SGME 连接正常: v" + (h.version ?? "?") + " llm=" + (h.llm?.model ?? "?") + " 记忆向量=" + (h.vector?.memory_vectors ?? "?"));
		else logger.warn("[dsh-sgme] SGME Gateway 不可达（baseUrl=" + config.baseUrl + "）——本插件是桥接插件，依赖 SGME 本体（Python 服务 :9910），没有本体是空壳。安装指引见 README 前置条件：https://github.com/freehul/sgme");
	}).catch((e) => {
		const msg = e instanceof Error ? e.message : String(e);
		logger.warn("[dsh-sgme] 启动连接探测异常: " + msg);
	});
	logger.info("SGME bridge 全部能力已注册（画像注入 + 工具 + 命令 + 会话同步 + dsg-rules + 连接探测）");
}
//#endregion
export { Config, apply, inject, name };
