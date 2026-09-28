import Schema from "schemastery";
import { ToolDefinition } from "@deepseek-ai/dsh-tools";
//#region src/context.d.ts
/** 注入模式（对应 templates/{mode}.yaml）。 */
type InjectMode = 'daily' | 'full' | 'coding' | 'work';
//#endregion
//#region src/commands.d.ts
/** dsh 命令结果（对齐 dsh-commands CommandResult）。 */
type CommandResult = {
  kind: 'success';
  text: string;
} | {
  kind: 'error';
  text: string;
};
/** dsh 命令 invocation（执行上下文）。 */
interface CommandInvocation {
  readonly rawInput: string;
  readonly signal: AbortSignal;
}
//#endregion
//#region src/index.d.ts
declare const name = "dsh-sgme";
declare const inject: string[];
interface CordisContext {
  logger: (name: string) => {
    info: (msg: string) => void;
    warn: (msg: string) => void;
  };
  effect: (fn: () => () => void, label?: string) => void;
  on: (event: string, handler: (...args: any[]) => any) => () => void;
  systemPrompt: {
    section: (section: {
      name: string;
      order: number;
      text: string | ((context: Record<string, unknown>) => string);
    }) => () => void;
  };
  tools: {
    register: (tool: ToolDefinition) => () => void;
  };
  commands: {
    register: (definition: {
      name: string;
      description: string;
      handler: (invocation: CommandInvocation) => Promise<CommandResult>;
    }) => () => void;
  };
}
/** 插件配置（由 cordis.yml 的 config 注入，install.py 写入 .env 后 dsh 加载）。 */
interface Config {
  baseUrl: string;
  agentKey: string;
  adminKey: string;
  agentId: string;
  injectMode: InjectMode;
  injectMaxTokens: number;
  searchLimit: number;
  projectHint?: string;
  rulesPath?: string;
  syncOnTurnEnd: boolean;
  turnBatchSize: number;
  evolveEnabled?: boolean;
  evolveMinRounds?: number;
  eventSubscribe?: boolean;
}
declare const Config: Schema<Config>;
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
declare function apply(ctx: CordisContext, config: Config): void;
//#endregion
export { Config, apply, inject, name };