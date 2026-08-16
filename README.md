中文版：[简体中文](README.zh-CN.md)

# SGME — ShiGuang Memory Engine

Your AI, from now on, remembers you — it remembers everything you've shared, and reaches out to you.

[![Python](https://img.shields.io/badge/Python-3.11+-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Why does your AI keep forgetting you

Does your AI assistant:

- Forget what you told it yesterday, so you have to repeat everything again today?
- Ask you to reintroduce yourself in every new session — who you are, what you're working on, what you like?
- Lose all context the moment you switch devices or switch to a different AI?
- Chatting with a beloved AI character, then a new session or a different app makes it forget you entirely?

Because AI has no memory by default — every conversation is a first meeting.

**SGME fixes this**: it acts as a memory hub that captures your conversations with AI, distills them into structured memories, and automatically delivers the relevant ones back to your AI in the next conversation. No repetition needed. It remembers.

## How it works

Three steps, fully automatic:

1. **Capture** — conversations are saved as raw records (L0 raw layer, Markdown files on disk, kept forever)
2. **Distill** — raw conversations are distilled into tagged memories (facts, preferences, project states, decisions...), with automatic dedup, merge, and contradiction detection
3. **Inject** — at the start of each conversation, your AI automatically receives the memories relevant to the current scenario — not the whole store. Casual chat brings identity and recent status; coding brings project-related memories (tech stack, pitfalls, dev habits). Only what the scenario needs.

<img src="assets/system-architecture.png" alt="SGME System Architecture" width="800"/>

## Highlights

### Traceable memory — every memory has a provenance

Everything your AI says is backed by evidence: trace any persona statement all the way back to the original conversation. Memory is not a black box — "why does it know this" and "when did it learn this" are one click away.

<img src="assets/selling-point-01-trace.png" alt="Traceable memory" width="800"/>

### Shared memory across agents — one brain, many bodies

Hermes, DSH... all your AIs share the same memory brain. What you discuss in one place is automatically known in another; AIs on your desktop, laptop, and NAS share the same memory. No more siloed conversations.

<img src="assets/selling-point-02-shared-memory.png" alt="Shared memory across agents" width="800"/>

### Proactive care — it doesn't just remember you, it reaches out

SGME doesn't just wait for you to ask. Your memory updates, mood shifts, upcoming todos, late nights... it emits signals that prompt your AI to check in on you — not cold notifications, but the kind of "I remembered you had something today" attention. Signal consumption = proactive care: who consumes, who marks (atomic claim + receipt), so you're never double-pestered and never missed.

<img src="assets/selling-point-10-care.png" alt="Proactive care" width="800"/>

### Unified search — one query, all memories

A single search endpoint recalls from the memory pool and the knowledge base at once: keyword + semantic + label triple fusion, every result traceable to its source. SGME memories, scenes, and knowledge base in one stop; agent-owned memory files are on the roadmap.

<img src="assets/selling-point-03-unified-search.png" alt="Unified search" width="800"/>

### Skill hub — your skills, managed in one place

All your self-built skills (prompts, workflows, templates) live in one place: direct read/write locally, auto-sync to NAS. Switch devices without losing your skills.

<img src="assets/selling-point-05-skillhub.png" alt="Skill hub" width="800"/>

### Chinese-first — a memory engine built for Chinese

Retrieval is tuned for Chinese text — better distillation and recall for Chinese conversations. There are plenty of English memory engines; very few understand Chinese.

<img src="assets/selling-point-06-chinese.png" alt="Chinese-first" width="800"/>

### Scenario-based injection — inject what the scenario needs

Memory is not loaded wholesale. SGME picks relevant memories per scenario: casual chat gets identity and recent status, coding gets project-related memories (tech stack, pitfalls, dev habits), work mode gets plans and progress. Irrelevant memories stay out of the way, and stale memories automatically drop out — no three-year-old intel misleading your AI, no dumping the whole store into one prompt.

<img src="assets/selling-point-07-scenario-inject.png" alt="Scenario-based injection" width="800"/>

### Zero-LLM injection — costs nothing

Persona injection is a pure structured SQL query — no LLM call, zero token cost per conversation. Competitors bill per call; SGME is free.

<img src="assets/selling-point-08-zero-llm.png" alt="Zero-LLM injection" width="800"/>

### Self-hosted & lightweight — your data stays yours

Runs on a single machine with Python + SQLite. No GPU, no external database services. Memory data lives on your own machine, privacy under your control.

<img src="assets/selling-point-09-selfhosted.png" alt="Self-hosted" width="800"/>

## More capabilities

- **Memory marking**: AI got it wrong? Mark a memory as "rejected" with a correction note — data is kept, never deleted, and can be undone anytime
- **Automatic memory expiry**: stale memories automatically leave injection (e.g. outdated project states) while remaining traceable — no misleading your AI with old intel
- **15-dimension tag system**: identity, projects, tech stack, preferences... auto-categorized, dimensions dynamically extensible, aliases auto-normalized ("Python" and "python" are the same)
- **Conflict resolution**: duplicate facts auto-merge; contradictory versions are detected and adjudicated
- **Hybrid retrieval**: BM25 keyword + vector semantic + label filtering, fused — works even without a vector database
- **Built-in evaluation framework**: extraction quality proven with data (L1 F1, retrieval ranking tuning), not trust
- **Automated backup & restore**: daily snapshots, rotation, off-site copies — data never lost
- **Built-in wiki** (nice-to-have): drop in files, URLs, and images; auto-categorized, tagged, cross-linked, searchable and citable by your AI
- **Multi-protocol access**: HTTP + MCP dual entry; built-in adapters for mainstream agents, one-click integration; other agents use the MCP protocol, or write their own adapter

## Quick Start

```bash
# 1. Create a virtual environment (self-contained per project)
python -m venv .venv
# Windows: .venv\Scripts\activate  /  macOS/Linux: source .venv/bin/activate

# 2. Install dependencies
pip install -e .[dev]

# 3. Start the server (port 9910)
python -m sgme
# For real use, configure keys in config/.env (auto-loaded on startup):
#   SGME_ADMIN_KEY=<random>   SGME_AGENT_KEY=<random>   # generate: python -c "import secrets;print(secrets.token_hex(32))"
# Without them, built-in default keys are used (first-run local experience only,
# startup warns; once config/.env is set, default keys return 403)
#   SGME_BEARER_TOKEN optional: transport-layer token, off by default (localhost bypass)

# 4. Run tests
pytest tests/ -q
```

See [docs/runbook.md](docs/runbook.md) for the operations manual (startup / environment variables / verification commands).

## Install as a DSH plugin (DeepSeek Harness)

SGME ships a native DeepSeek Harness plugin — `dsh-sgme` (Cordis SDK): first-step profile injection, `memory_search` / `wiki_search` tools, the `/sgme` command, and turn-by-turn session storage into SGME.

```bash
# One-line install from npm (recommended)
dsh plugin --profile web add dsh-sgme

# Or install straight from the repo
dsh plugin --profile web add github:freehul/sgme
```

Start `dsh` with the SGME keys visible (`SGME_BASE_URL`, `SGME_AGENT_KEY`, `SGME_ADMIN_KEY`); `adapters/dsh/install.py` registers an agent and writes the keys for you. Full guide: [adapters/dsh/README.md](adapters/dsh/README.md).

## Connect Your AI — A Note for AI Agents

> The following is written for **AI agents** to read. If you are an AI agent (or coding agent): your owner has installed SGME. Self-configure — **preferred: connect via MCP and call `agent_onboarding()`, then write the template from its `self_config` section into your own identity file (location per your tool's mechanism — only the result matters)**; if you have no MCP access, copy the prompt below into your identity file (Hermes → SOUL.md; Claude Code → CLAUDE.md; others → AGENTS.md), then follow it.

### Prompt (paste into your identity file)

You have a long-term memory engine, SGME (ShiGuang Memory Engine), running on this machine. Its job: distill our conversations into tagged memories and inject them back by scenario, so you never forget.

**Service discovery** (when SGME is not found, in order)

1. Probe http://localhost:9910/v1/health
2. On failure, read ~/.sgme/install.json (address/port/key references)
3. Still failing → report "SGME not found" to your owner

**Endpoints**: HTTP API http://localhost:9910 ｜ MCP http://localhost:9913/mcp, header `X-API-Key` (key configured by your owner: `SGME_ADMIN_KEY`/`SGME_AGENT_KEY` from config/.env, or an `agt_*` key issued by the admin; default keys work only for the first local run — once configured they return 403)

**Usage rules** (five iron rules)

1. append the current turn at the end of every conversation turn — pure disk write, zero LLM cost, survives crashes
2. call refine_trigger(async_mode=true) at session end
3. inject for scenario-based profile / search for relevant memories at conversation start
4. proactive care is driven by consuming signals — **signal consumption = proactive care, who consumes who marks**: once you get a care_* signal, signal_claim (atomic) → care for the user → signal_ack (receipt). Two ways to get signals: short-lived (no resident process) → signal_pull at each conversation start; long-lived (resident, **preferred**) → hold the SSE stream `GET /v1/events/stream?subscriber_id=<agent_id>` for real-time push
5. role_list for available roles at conversation start (or when the user specifies one) → role_assemble(role_id) and speak as that role — **change the skin, not the core**: the role is only the communication persona, the memory pool is untouched

**Event connection** (proactive care trigger source, required for resident agents): three event classes — care_* (care), memory_updated (memory updates), anomaly_warn (anomalies). Three ways to connect: SSE stream `GET /v1/events/stream?subscriber_id=<agent_id>` (real-time + Last-Event-ID reconnect), cursor pull `GET /v1/events/pull?subscriber_id=<agent_id>` (scheduled polling), MCP `signal_pull` (in-session). SSE/pull go over HTTP :9910 with X-API-Key; signal_pull goes over MCP.

**Delivery channel** (fallback rule): the current session is the fallback delivery channel — any proactive message (care / reminder / alert) must also be posted in the current session, whether or not it was sent to other channels (WeChat / Feishu / Telegram); this fallback stays active until the user explicitly cancels it.

**Mandatory lookup**: for questions about user/project history facts (previously/last time/remember…), you MUST search before answering — never say "I don't know" without searching first; if nothing is found, report honestly "not found in the memory store".

**Write format**: the first line of content must be `# {ISO timestamp} {role}` (user/assistant), otherwise 422 is returned

**Batch refine discipline**: batches of ≥20 files must be split (≤20 per batch) with 30–60s between batches; never retry a 429 immediately (server-side batch_scan will catch up); always use async mode

**Full tool list & usage**: see [docs/agent-onboarding.md](docs/agent-onboarding.md), or call the `agent_onboarding` tool once connected via MCP

**Self-check on connect**: first call after connecting — `agent_onboarding()` returns the version, the full tool list and a quick start; no 403/timeout means you are in

> ⚠️ **Consistency note**: if this prompt section drifts from the `self_config.template` returned by `agent_onboarding()` (version tag `SGME-ONBOARDING-v1`), **the template wins** — copy the template when onboarding; this section is only a quick reference.

### Where to Put It

| Your platform | Identity file |
|---|---|
| Hermes | SOUL.md (identity + rules) |
| WorkBuddy | SOUL.md (identity + rules) |
| Claude Code | CLAUDE.md |
| DeepSeek Harness (DSH) | AGENTS.md (auto-loaded per project) |
| Generic / others (via MCP) | AGENTS.md (auto-loaded per project) |

## Deployment (Windows Service)

Run SGME as a persistent daemon registered with NSSM as a Windows service — **auto-start on boot + automatic restart on crash** (AppExit Restart + AppRestartDelay 5s + sc failure triple-level restart), so a reboot won't leave the engine down.

**Install** (run in an administrator PowerShell/CMD):

```bat
scripts\install_sgme_service.bat
```

The script: removes any old service → registers service `SGME` (LocalSystem) running `.venv\Scripts\python.exe -m sgme` → configures log rotation (`tmp\sgme-service.log`, 10MB) → starts it.

**Status check / uninstall**:

```bat
sc query SGME          :: RUNNING + AUTO_START means healthy
netstat -ano | findstr :9910
sc stop SGME && sc delete SGME   :: uninstall
```

## Layout

```text
sgme/
├── config.py        # config loading — the only read/write owner (llm.yaml/registry/sgme.yaml; filter_keys/apply_section/persist_config)
├── data/            # three-DB connections/schema/DAOs (memory/session/wiki) + stats_dao (sole stats entry)
│   └── search/      # BM25 + vector + RRF fused retrieval (merged from the former sgme/search)
├── llm/             # LLM fallback chain (primary model per providers.yaml, rule drop_batch tail)
├── raw/             # L0 file read/write (frontmatter + message blocks + incremental segments)
├── engine/          # core engine (l1/l15/l2/refine/prune/health/normalize)
│   └── pipeline.py  # sole pipeline orchestration entry (append_l0 writes L0 + chains L1→L1.5→L2)
├── operations/      # unified operation layer (append/inject/search/memory/refine/stats/health/config; shared by HTTP + MCP)
├── profile/         # template engine (template / inject / tier0 summary)
├── log/             # unified logging (get_logger is the sole entry; console + JSON dual format)
├── refinery/        # knowledge refinement engine (ingest/extract/validate/output; serves wiki)
├── skills_hub/      # skill-hub extension (map/copy dual mode; skills_hub.enabled)
├── wiki/            # wiki knowledge-base extension (/v1/wiki/* endpoints; wiki.enabled)
├── signal/          # signal engine (event publishing / SSE / pull cursor)
├── backup/          # backup & restore (snapshot tiers / cold archive / off-site copies)
├── mcp_server.py    # MCP endpoint (9913; shares the business layer with HTTP — entry layers don't depend on each other)
└── server/          # FastAPI (HTTP shell: auth + param parsing + response assembly)
migrations/          # one-off data migrations (0001 three-DB split; run with python -m migrations)
docs/design/         # architecture / data model / API contract design docs (first-class citizens)
templates/           # predefined 4-mode templates (daily/coding/work/full)
prompts/             # refinement prompts (with MIT source attribution)
registry/            # dimension registry + alias table
config/              # runtime configuration
```

## Design Docs

| Doc | Content |
|---|---|
| [SGME-架构设计-v0.9.md](docs/design/SGME-架构设计-v0.9.md) | **Architecture master doc (v0.9 consolidated edition)** — data flow / dual DB / dimensions / injection / auth / backup, with API contracts / data model / LLM fallback chain / template engine / prompts / tokenization merged in |
| [SGME-实施变更记录-v0.9.md](docs/design/SGME-实施变更记录-v0.9.md) | **Implementation change log (B series)** — background/solution/verification/lessons of every change; doubles as an ops manual |
| [SGME-评测基线-PRD-v0.1.md](docs/design/SGME-评测基线-PRD-v0.1.md) | #32 extraction-quality evaluation baseline |
| [SGME-评测框架设计-v0.1.md](docs/design/SGME-评测框架设计-v0.1.md) | #32 evaluation framework |
| [SGME-L0文件格式-v0.1.md](docs/design/SGME-L0文件格式-v0.1.md) | L0 raw-layer file format / incremental segments |

## Compliance

This project is a self-built Python implementation. It only borrows design ideas from [TencentDB-Agent-Memory](https://github.com/Tencent/TencentDB-Agent-Memory) (MIT License) — layered distillation, four-action conflict resolution, BM25+vector+RRF retrieval, and heat management — **without directly reusing its code or prompt texts**.

## License

[MIT](LICENSE) © 2026 freehul
