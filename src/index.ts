/**
 * Theo runtime entrypoint.
 *
 * Wires the core subsystems together and hands them to the `Engine`, which
 * owns the startup/shutdown sequence. Signal handlers (SIGINT/SIGTERM)
 * trigger a graceful stop through `Engine.stop()` exactly once — the engine
 * is idempotent under concurrent signals.
 */

import { ChatEngine } from "./chat/engine.ts";
import { SessionManager } from "./chat/session.ts";
import { buildSdkAgentsMap, SUBAGENTS } from "./chat/subagents.ts";
import { loadConfig } from "./config.ts";
import { createPool } from "./db/pool.ts";
import { Engine, installSignalHandlers } from "./engine.ts";
import { describeError } from "./errors.ts";
import { createEventBus } from "./events/bus.ts";
import { createEventLog } from "./events/log.ts";
import { createUpcasterRegistry } from "./events/upcasters.ts";
import { CliGate } from "./gates/cli/gate.ts";
import { CoreMemoryRepository } from "./memory/core.ts";
import { HuggingFaceEmbeddingService } from "./memory/embeddings.ts";
import { EpisodicRepository } from "./memory/episodic.ts";
import { EdgeRepository } from "./memory/graph/edges.ts";
import { NodeRepository } from "./memory/graph/nodes.ts";
import { RetrievalService } from "./memory/retrieval.ts";
import { createSkillRepository } from "./memory/skills.ts";
import { createMemoryServer } from "./memory/tools.ts";
import { createUserModelRepository } from "./memory/user_model.ts";

// ---------------------------------------------------------------------------
// Config + pool
// ---------------------------------------------------------------------------

const configResult = loadConfig();
if (!configResult.ok) {
	const { error } = configResult;
	console.error("Configuration error:", error.message);
	if (error.code === "CONFIG_INVALID") {
		for (const issue of error.issues) {
			console.error(`  ${issue.path}: ${issue.message}`);
		}
	}
	process.exit(1);
}
const config = configResult.value;

// `onnotice` silences postgres NOTICE messages that otherwise land on stdout.
// The Ink TUI renders on stdout, so any unscoped stdout write bleeds through
// the UI as garbled text.
const pool = createPool(config, { onnotice: () => {} });
const connectResult = await pool.connect();
if (!connectResult.ok) {
	console.error("Database connection failed:", connectResult.error.message);
	await pool.end();
	process.exit(1);
}
console.info("Connected to PostgreSQL.");

// ---------------------------------------------------------------------------
// Event bus (log + upcasters)
// ---------------------------------------------------------------------------

const upcasters = createUpcasterRegistry();
const eventLog = createEventLog(pool.sql, upcasters);
const bus = createEventBus(eventLog, pool.sql);

// ---------------------------------------------------------------------------
// Memory layer
// ---------------------------------------------------------------------------

const embeddings = new HuggingFaceEmbeddingService();
const nodes = new NodeRepository(pool.sql, bus, embeddings);
const edges = new EdgeRepository(pool.sql, bus);
const coreMemory = new CoreMemoryRepository(pool.sql, bus);
const retrieval = new RetrievalService(pool.sql, embeddings, nodes);
const userModel = createUserModelRepository(pool.sql, bus);
const skills = createSkillRepository(pool.sql, embeddings, bus);
const episodic = new EpisodicRepository(pool.sql, bus, embeddings);

const memoryServer = createMemoryServer({
	nodes,
	edges,
	coreMemory,
	retrieval,
	userModel,
	skills,
	sql: pool.sql,
});

// ---------------------------------------------------------------------------
// Chat engine
// ---------------------------------------------------------------------------

const sessions = new SessionManager(embeddings);

const chatEngine = new ChatEngine({
	bus,
	sessions,
	memoryServer,
	coreMemory,
	episodic,
	context: {
		coreMemory,
		userModel,
		retrieval,
		skills,
		embeddings,
	},
	agents: buildSdkAgentsMap(SUBAGENTS),
});

// ---------------------------------------------------------------------------
// Gate
// ---------------------------------------------------------------------------

const gate = new CliGate(chatEngine, bus);

// ---------------------------------------------------------------------------
// Engine lifecycle
// ---------------------------------------------------------------------------

const engine = new Engine({
	pool,
	bus,
	chatEngine,
	gate,
});

installSignalHandlers(engine);

try {
	await engine.start();
	console.info("Theo is running.");
} catch (error) {
	console.error(`Engine failed to start: ${describeError(error)}`);
	await engine.stop("startup_failed").catch(() => {});
	process.exit(1);
}

await engine.awaitStopped();
