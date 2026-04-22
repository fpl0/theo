/**
 * Theo runtime entrypoint.
 *
 * Wires the core subsystems together and hands them to the `Engine`, which
 * owns the startup/shutdown sequence. Signal handlers (SIGINT/SIGTERM)
 * trigger a graceful stop through `Engine.stop()` exactly once — the engine
 * is idempotent under concurrent signals.
 *
 * This entrypoint stays deliberately linear: each subsystem's constructor
 * is called in order, then the Engine is started. Wiring complexity lives
 * here so the Engine keeps a narrow interface (start/stop/pause/resume).
 */

import { ChatEngine } from "./chat/engine.ts";
import { SessionManager } from "./chat/session.ts";
import { buildSchedulerSubagents, buildSdkAgentsMap, SUBAGENTS } from "./chat/subagents.ts";
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
import { createSelfModelRepository } from "./memory/self_model.ts";
import { createSkillRepository } from "./memory/skills.ts";
import { createMemoryServer } from "./memory/tools.ts";
import { createUserModelRepository } from "./memory/user_model.ts";
import { BUILTIN_JOBS } from "./scheduler/builtin.ts";
import { Scheduler } from "./scheduler/runner.ts";
import { createJobStore } from "./scheduler/store.ts";
import { initTelemetry } from "./telemetry/index.ts";

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

const pool = createPool(config);
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
// Telemetry — wires the projector to the bus BEFORE handlers run
// ---------------------------------------------------------------------------

const telemetry = await initTelemetry(
	{
		environment: (process.env["THEO_ENV"] as "prod" | "dev" | "test") ?? "dev",
		logger: {
			...(process.env["THEO_WORKSPACE"] !== undefined
				? { logDir: `${process.env["THEO_WORKSPACE"]}/logs` }
				: {}),
			level: (process.env["THEO_LOG_LEVEL"] as "debug" | "info" | "warn" | "error") ?? "info",
		},
	},
	bus,
	pool.sql,
);

// ---------------------------------------------------------------------------
// Memory layer
// ---------------------------------------------------------------------------

const embeddings = new HuggingFaceEmbeddingService();
const nodes = new NodeRepository(pool.sql, bus, embeddings);
const edges = new EdgeRepository(pool.sql, bus);
const coreMemory = new CoreMemoryRepository(pool.sql, bus);
const retrieval = new RetrievalService(pool.sql, embeddings, nodes);
const userModel = createUserModelRepository(pool.sql, bus);
const selfModel = createSelfModelRepository(pool.sql, bus);
const skills = createSkillRepository(pool.sql, embeddings, bus);
const episodic = new EpisodicRepository(pool.sql, bus, embeddings);

const memoryServer = createMemoryServer({
	nodes,
	edges,
	coreMemory,
	retrieval,
	userModel,
	selfModel,
	skills,
});

// ---------------------------------------------------------------------------
// Chat engine
// ---------------------------------------------------------------------------

const sessions = new SessionManager(embeddings, selfModel);

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
// Scheduler
// ---------------------------------------------------------------------------

const jobStore = createJobStore(pool.sql);
const scheduler = new Scheduler({
	store: jobStore,
	bus,
	memoryServer,
	subagents: buildSchedulerSubagents(SUBAGENTS),
	builtins: BUILTIN_JOBS,
});

// ---------------------------------------------------------------------------
// Gate
// ---------------------------------------------------------------------------

const gate = new CliGate(chatEngine, bus, { sql: pool.sql, bus });

// ---------------------------------------------------------------------------
// Engine lifecycle
// ---------------------------------------------------------------------------

const engine = new Engine({
	pool,
	bus,
	scheduler,
	chatEngine,
	gate,
	telemetry,
	...(process.env["THEO_WORKSPACE"] !== undefined
		? { selfUpdateWorkspace: process.env["THEO_WORKSPACE"] }
		: {}),
});

installSignalHandlers(engine);

try {
	await engine.start();
	console.info("Theo is running.");
} catch (error) {
	console.error(`Engine failed to start: ${describeError(error)}`);
	await engine.stop("startup_failed").catch(() => {
		// stop() already logs individual subsystem errors.
	});
	process.exit(1);
}
