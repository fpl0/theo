/**
 * Contradiction detection — split into decision + effect handlers.
 *
 * The pipeline has three stages, following the `*_requested` / `*_classified`
 * decision/effect pattern from foundation §7.4:
 *
 * 1. Decision: on `memory.node.created`, find up to N same-kind similar
 *    candidates and emit one `contradiction.requested` event per candidate.
 *    Deterministic over the graph state → replay-safe.
 *
 * 2. Effect: on `contradiction.requested`, call the LLM classifier, then emit
 *    `contradiction.classified` with the verdict. Rate-limited to 10 calls per
 *    minute to bound cost. Runs only in live mode — replay skips.
 *
 * 3. Decision: on `contradiction.classified`, if the classifier said
 *    `contradicts = true`, adjust both nodes' confidence down by 0.2 and
 *    create a `contradicts` edge, then emit
 *    `memory.contradiction.detected` with the explanation. Replay-safe.
 *
 * The outside world's answer is captured as a durable event, so replay rebuilds
 * the graph exactly without re-calling the LLM.
 */

import { describeError } from "../errors.ts";
import type { EventBus } from "../events/bus.ts";
import type { ContradictionClassifiedData, EventOfType } from "../events/types.ts";
import type { EdgeRepository } from "./graph/edges.ts";
import type { NodeRepository } from "./graph/nodes.ts";
import { asNodeId, type NodeId } from "./graph/types.ts";
import { cheapQuery } from "./llm.ts";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/** Cosine similarity cutoff for candidates. */
const SIMILARITY_THRESHOLD = 0.8;

/** Max candidates per newly-created node. Caps fan-out. */
const MAX_CANDIDATES = 5;

/** Rolling window for rate limiting. */
const RATE_LIMIT_WINDOW_MS = 60_000;

/** Max LLM classifications per rolling window. */
export const MAX_CALLS_PER_MINUTE = 10;

/** Confidence penalty applied to both nodes when contradiction is confirmed. */
const CONFIDENCE_PENALTY = 0.2;

// ---------------------------------------------------------------------------
// Classifier seam — tests stub this; production uses the SDK.
// ---------------------------------------------------------------------------

/** Verdict returned by the classifier. */
export interface ContradictionVerdict {
	readonly contradicts: boolean;
	readonly explanation: string;
}

/** Function that decides whether two node bodies contradict each other. */
export type ContradictionClassifier = (
	aBody: string,
	bBody: string,
) => Promise<ContradictionVerdict>;

/**
 * Default classifier using the Claude Agent SDK `query()` with JSON schema
 * structured output and `haiku` (cheapest tier). Each call consumes the full
 * async generator; the `SDKResultSuccess.structured_output` field carries the
 * verdict.
 */
export async function defaultContradictionClassifier(
	aBody: string,
	bBody: string,
): Promise<ContradictionVerdict> {
	const { structured } = await cheapQuery({
		prompt: `Do these two statements contradict each other?\n\nA: "${aBody}"\nB: "${bBody}"`,
		schema: {
			type: "object",
			properties: {
				contradicts: { type: "boolean" },
				explanation: { type: "string" },
			},
			required: ["contradicts", "explanation"],
		},
	});
	if (isVerdict(structured)) return structured;
	return { contradicts: false, explanation: "classification failed" };
}

function isVerdict(value: unknown): value is ContradictionVerdict {
	if (typeof value !== "object" || value === null) return false;
	const v = value as Record<string, unknown>;
	return typeof v["contradicts"] === "boolean" && typeof v["explanation"] === "string";
}

// ---------------------------------------------------------------------------
// Rate limiter (simple rolling window)
// ---------------------------------------------------------------------------

/** Injectable clock for tests. */
export type Clock = () => number;

/** Rolling-window counter. Thread-safe for single-process bun runtime. */
export class RateLimiter {
	private readonly timestamps: number[] = [];

	constructor(
		private readonly maxCalls: number,
		private readonly windowMs: number,
		private readonly now: Clock = Date.now,
	) {}

	/** Try to consume one call slot. Returns true on success, false if over-limit. */
	tryAcquire(): boolean {
		const t = this.now();
		const cutoff = t - this.windowMs;
		// Drop expired timestamps from the head. Array-shift is O(n) but n <= maxCalls,
		// which is 10 for this limiter — negligible.
		while (this.timestamps.length > 0 && (this.timestamps[0] ?? 0) < cutoff) {
			this.timestamps.shift();
		}
		if (this.timestamps.length >= this.maxCalls) return false;
		this.timestamps.push(t);
		return true;
	}
}

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

export interface ContradictionDeps {
	readonly bus: EventBus;
	readonly nodes: NodeRepository;
	readonly edges: EdgeRepository;
	readonly classifier?: ContradictionClassifier;
	readonly rateLimiter?: RateLimiter;
}

// ---------------------------------------------------------------------------
// Handler registration
// ---------------------------------------------------------------------------

/**
 * Wire the three contradiction-detection handlers onto the bus. Idempotent
 * across restarts thanks to the handler checkpoint.
 *
 * - `contradiction-requester`: decision handler on `memory.node.created`.
 * - `contradiction-classifier`: effect handler on `contradiction.requested`.
 * - `contradiction-applier`: decision handler on `contradiction.classified`.
 */
export function registerContradictionHandlers(deps: ContradictionDeps): void {
	const classifier = deps.classifier ?? defaultContradictionClassifier;
	const limiter = deps.rateLimiter ?? new RateLimiter(MAX_CALLS_PER_MINUTE, RATE_LIMIT_WINDOW_MS);

	deps.bus.on(
		"memory.node.created",
		async (event) => {
			await requestContradictionChecks(event, deps);
		},
		{ id: "contradiction-requester", mode: "decision" },
	);

	deps.bus.on(
		"contradiction.requested",
		async (event) => {
			await runContradictionClassification(event, deps, classifier, limiter);
		},
		{ id: "contradiction-classifier", mode: "effect" },
	);

	deps.bus.on(
		"contradiction.classified",
		async (event) => {
			await applyContradictionVerdict(event, deps);
		},
		{ id: "contradiction-applier", mode: "decision" },
	);
}

// ---------------------------------------------------------------------------
// Stage 1: request classifications
// ---------------------------------------------------------------------------

export async function requestContradictionChecks(
	event: EventOfType<"memory.node.created">,
	deps: ContradictionDeps,
): Promise<void> {
	const nodeId = asNodeId(event.data.nodeId);
	// The handler dispatches from an in-memory queue that may wake up BEFORE
	// the node's creating transaction has committed (bus.emit enqueues
	// synchronously inside the caller's tx). Retry a small number of times
	// if the row isn't yet visible to the handler's connection.
	const node = await getNodeWhenVisible(nodeId, deps);
	if (node === null || node.embedding === null) return;

	const similar = await deps.nodes.findSimilar(
		node.embedding,
		SIMILARITY_THRESHOLD,
		MAX_CANDIDATES + 1,
	);
	const candidates = similar.filter((n) => n.id !== nodeId && n.kind === node.kind);
	if (candidates.length === 0) return;

	for (const candidate of candidates.slice(0, MAX_CANDIDATES)) {
		await deps.bus.emit({
			type: "contradiction.requested",
			version: 1,
			actor: "system",
			data: { nodeId, candidateId: candidate.id },
			metadata: { causeId: event.id },
		});
	}
}

const VISIBILITY_RETRY_ATTEMPTS = 10;
const VISIBILITY_RETRY_DELAY_MS = 20;

/**
 * Look up a node that was just announced via `memory.node.created`.
 *
 * `bus.emit` inside a SQL transaction enqueues the event synchronously, but
 * the enclosing transaction has not yet committed when the handler queue
 * dequeues the event. The handler's own SQL connection therefore may not
 * see the newly-inserted row on its first read. A small, bounded retry loop
 * (~200 ms total) absorbs this short visibility gap without pulling the
 * read into the caller's transaction — which would defeat the async
 * handler model.
 */
async function getNodeWhenVisible(nodeId: NodeId, deps: ContradictionDeps) {
	for (let attempt = 0; attempt < VISIBILITY_RETRY_ATTEMPTS; attempt++) {
		const node = await deps.nodes.getById(nodeId);
		if (node !== null) return node;
		await new Promise<void>((resolve) => {
			setTimeout(resolve, VISIBILITY_RETRY_DELAY_MS);
		});
	}
	return null;
}

// ---------------------------------------------------------------------------
// Stage 2: run the LLM classifier (effect)
// ---------------------------------------------------------------------------

export async function runContradictionClassification(
	event: EventOfType<"contradiction.requested">,
	deps: ContradictionDeps,
	classifier: ContradictionClassifier,
	limiter: RateLimiter,
): Promise<void> {
	// Rate limit first — skip silently when over quota. The event stays in the
	// log; operators can resurface it manually if needed.
	if (!limiter.tryAcquire()) return;

	const [a, b] = await Promise.all([
		deps.nodes.getById(asNodeId(event.data.nodeId)),
		deps.nodes.getById(asNodeId(event.data.candidateId)),
	]);
	if (a === null || b === null) return;

	let verdict: ContradictionVerdict;
	try {
		verdict = await classifier(a.body, b.body);
	} catch (error: unknown) {
		// Classifier failures are treated as non-contradiction so the decision
		// handler can close the loop deterministically. The failure reason is
		// logged for debugging.
		const message = describeError(error);
		console.warn(`Contradiction classifier failed: ${message}`);
		verdict = { contradicts: false, explanation: `classifier error: ${message}` };
	}

	const payload: ContradictionClassifiedData = {
		nodeId: event.data.nodeId,
		candidateId: event.data.candidateId,
		contradicts: verdict.contradicts,
		explanation: verdict.explanation,
	};

	await deps.bus.emit({
		type: "contradiction.classified",
		version: 1,
		actor: "system",
		data: payload,
		metadata: { causeId: event.id },
	});
}

// ---------------------------------------------------------------------------
// Stage 3: apply the verdict (decision)
// ---------------------------------------------------------------------------

export async function applyContradictionVerdict(
	event: EventOfType<"contradiction.classified">,
	deps: ContradictionDeps,
): Promise<void> {
	if (!event.data.contradicts) return;

	const nodeId: NodeId = asNodeId(event.data.nodeId);
	const candidateId: NodeId = asNodeId(event.data.candidateId);

	// Confidence adjustment uses its own transaction (NodeRepository owns the
	// atomic SQL + event emit). Running these sequentially keeps the graph
	// mutations well-ordered in the event log.
	await deps.nodes.adjustConfidence(nodeId, -CONFIDENCE_PENALTY, "system");
	await deps.nodes.adjustConfidence(candidateId, -CONFIDENCE_PENALTY, "system");

	await deps.edges.create({
		sourceId: nodeId,
		targetId: candidateId,
		label: "contradicts",
		weight: 1.0,
		actor: "system",
	});

	await deps.bus.emit({
		type: "memory.contradiction.detected",
		version: 1,
		actor: "system",
		data: {
			nodeId: event.data.nodeId,
			conflictId: event.data.candidateId,
			explanation: event.data.explanation,
		},
		metadata: { causeId: event.id },
	});
}
