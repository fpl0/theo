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
import { registerGoalHandlers } from "./goals/handlers.ts";
import { GoalRepository } from "./goals/repository.ts";
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
import { wrapHandlerWithSpan } from "./telemetry/spans/bus.ts";
import { instrumentSql } from "./telemetry/spans/db.ts";
import { SyntheticProbeScheduler } from "./telemetry/synthetic.ts";

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

// Install the bus-dispatch span wrapper AFTER telemetry init — subsequent
// handler registrations see the wrapper. Calling this before `bus.start()`
// means every registered durable handler is instrumented.
bus.setDurableHandlerWrapper(
	wrapHandlerWithSpan(telemetry.internals.tracer, telemetry.internals.metrics),
);

// Install the pg query-timing hook. Wraps `pool.sql` in a Proxy so every
// downstream repository and handler records `theo.db.query_duration_ms`.
// The Proxy is transparent: every property access forwards to the original,
// so transaction helpers (`sql.begin`), array helpers, and unsafe queries
// pass through unchanged.
pool.sql = instrumentSql(pool.sql, telemetry.internals.metrics);

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
const goals = new GoalRepository(pool.sql, bus, nodes);

// Register goal projection handlers + poison-quarantine circuit breaker so
// `goal.created` and friends project into the `goal_state` table. Without
// this, GoalRepository.create() emits the event but the projection never
// runs and `readState` returns null — the repository then throws, so every
// goal-capture SDK call fails.
registerGoalHandlers({ sql: pool.sql, bus, goals });

const memoryServer = createMemoryServer({
	nodes,
	edges,
	coreMemory,
	retrieval,
	userModel,
	selfModel,
	skills,
	goals,
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
// Synthetic prober — issues a canary "ping" every 5 minutes via the engine's
// chat surface so `theo.synthetic.probe_*` populates for the SyntheticProbeFailing
// alert. The scheduler is SDK-light; probe turns hit the interactive path so
// they exercise the full assembly pipeline.
// ---------------------------------------------------------------------------

const syntheticProbe = new SyntheticProbeScheduler({
	chat: {
		handleMessage: (body, gate) => chatEngine.handleMessage(body, gate),
	},
	bus,
	metrics: telemetry.internals.metrics,
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
	syntheticProbe,
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

// Daemon: stay alive until `stop()` resolves (signal handlers or self-update).
// Without this, a gate that throws synchronously (e.g., Ink render without a
// TTY) lets `runGate` swallow the error and `main` returns, letting Bun drain
// the event loop and exit silently with code 0.
await engine.awaitStopped();
