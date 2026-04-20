/**
 * SessionManager: short-lived working memory for a chat session.
 *
 * A "session" is one contiguous conversation — the SDK's `session_id` is
 * durable (compaction & context preserved) but expensive to keep open
 * indefinitely. The manager decides when to continue an existing session
 * versus start fresh.
 *
 * Signals that shape the decision:
 *   - inactivity timeout (wall-clock gap)
 *   - core memory hash (Theo's identity changed — reassemble the prompt)
 *   - topic continuity (embedding similarity above threshold — keep session)
 *   - session depth (>50 turns — extend the effective timeout so deep focus
 *     isn't broken by minor pauses)
 *
 * Each decision is recorded as a prediction in the `session_management`
 * self-model domain. User corrections (explicit `/resume` or `/reset`) record
 * outcomes via `recordCorrection()`. Over time the heuristic calibrates.
 */

import { ulid } from "ulid";
import type { EmbeddingService } from "../memory/embeddings.ts";
import type { SelfModelRepository } from "../memory/self_model.ts";

// ---------------------------------------------------------------------------
// Configuration constants
// ---------------------------------------------------------------------------

/** Default inactivity timeout: 15 minutes. */
const DEFAULT_INACTIVITY_TIMEOUT_MS = 15 * 60 * 1000;

/**
 * Default cosine similarity threshold for topic continuity. Two embeddings
 * above this are considered the same topic — the session is extended even
 * past the inactivity timeout.
 */
const DEFAULT_TOPIC_CONTINUITY_THRESHOLD = 0.7;

/**
 * Default depth threshold: sessions with ≥50 turns get their timeout extended
 * by DEEP_SESSION_TIMEOUT_MULTIPLIER. Deep sessions represent focused work;
 * breaking them on a short coffee break is worse than carrying a bit of noise.
 */
const DEFAULT_DEEP_SESSION_THRESHOLD = 50;

/** Multiplier applied to the inactivity timeout for deep sessions. */
const DEEP_SESSION_TIMEOUT_MULTIPLIER = 3;

/** Self-model domain tracking session decision accuracy. */
const SELF_MODEL_DOMAIN = "session_management";

// ---------------------------------------------------------------------------
// Repository abstraction — only hash() is needed from core memory
// ---------------------------------------------------------------------------

/** Minimal interface the session manager needs from core memory. */
export interface CoreMemoryHasher {
	hash(): Promise<string>;
}

// ---------------------------------------------------------------------------
// SessionManager
// ---------------------------------------------------------------------------

/** Configuration for the session manager. */
export interface SessionManagerConfig {
	readonly inactivityTimeoutMs?: number;
	readonly topicContinuityThreshold?: number;
	readonly deepSessionThreshold?: number;
	/** Optional clock override for deterministic tests. */
	readonly now?: () => number;
}

/**
 * The outcome of a `shouldStartNewSession` decision. Returned as a discriminated
 * union so callers can log the reason and the manager can record the prediction.
 */
export type SessionDecision =
	| { readonly continue: true; readonly reason: "active" | "topic_continuity" | "deep_session" }
	| {
			readonly continue: false;
			readonly reason:
				| "no_active_session"
				| "core_memory_changed"
				| "inactivity_timeout"
				| "topic_discontinuity";
	  };

export class SessionManager {
	private activeSessionId: string | null = null;
	private lastActivityAt: number | null = null;
	private coreMemoryHash: string | null = null;
	private lastMessageEmbedding: Float32Array | null = null;
	private turnCount = 0;

	private readonly inactivityTimeoutMs: number;
	private readonly topicContinuityThreshold: number;
	private readonly deepSessionThreshold: number;
	private readonly now: () => number;

	constructor(
		private readonly embeddings: EmbeddingService,
		private readonly selfModel: SelfModelRepository,
		config?: SessionManagerConfig,
	) {
		this.inactivityTimeoutMs = config?.inactivityTimeoutMs ?? DEFAULT_INACTIVITY_TIMEOUT_MS;
		this.topicContinuityThreshold =
			config?.topicContinuityThreshold ?? DEFAULT_TOPIC_CONTINUITY_THRESHOLD;
		this.deepSessionThreshold = config?.deepSessionThreshold ?? DEFAULT_DEEP_SESSION_THRESHOLD;
		this.now = config?.now ?? Date.now;
	}

	/**
	 * Decide whether to continue the active session or start a new one.
	 * Also records the decision as a prediction against the self-model domain
	 * so calibration can be tracked over time.
	 *
	 * @param userMessage - the incoming message (used for topic continuity)
	 * @param core - core memory hasher (to detect identity changes)
	 */
	async decide(userMessage: string, core: CoreMemoryHasher): Promise<SessionDecision> {
		const decision = await this.compute(userMessage, core);

		// Every decision is a prediction — future user corrections calibrate
		// the self-model domain. recordCorrection() records the outcome.
		await this.selfModel.recordPrediction(SELF_MODEL_DOMAIN, "system");

		return decision;
	}

	/**
	 * Compute the decision without side effects (for unit testing the
	 * heuristic itself). The public `decide()` wraps this and records a
	 * prediction.
	 */
	private async compute(userMessage: string, core: CoreMemoryHasher): Promise<SessionDecision> {
		// 1. No active session: always start fresh.
		if (this.activeSessionId === null) {
			return { continue: false, reason: "no_active_session" };
		}

		// 2. Core memory changed: the prompt is stale, start fresh.
		const currentHash = await core.hash();
		if (this.coreMemoryHash !== null && currentHash !== this.coreMemoryHash) {
			return { continue: false, reason: "core_memory_changed" };
		}

		// 3. Within the normal timeout: continue without checking further.
		if (!this.isTimedOut()) {
			return { continue: true, reason: "active" };
		}

		// 4. Timed out but the session is deep: the effective timeout is longer.
		if (this.turnCount >= this.deepSessionThreshold && !this.isTimedOutDeep()) {
			return { continue: true, reason: "deep_session" };
		}

		// 5. Timed out: check topic continuity.
		if (this.lastMessageEmbedding !== null) {
			const embedding = await this.embeddings.embed(userMessage);
			const similarity = cosineSimilarity(embedding, this.lastMessageEmbedding);
			if (similarity >= this.topicContinuityThreshold) {
				return { continue: true, reason: "topic_continuity" };
			}
			return { continue: false, reason: "topic_discontinuity" };
		}

		// No last message embedding to compare against — default to fresh.
		return { continue: false, reason: "inactivity_timeout" };
	}

	/**
	 * Start a new session. Resets depth and activity trackers. Returns the
	 * new session ID (ULID — sortable, timestamp-embedded).
	 */
	async startSession(core: CoreMemoryHasher): Promise<string> {
		this.activeSessionId = ulid();
		this.lastActivityAt = this.now();
		this.coreMemoryHash = await core.hash();
		this.lastMessageEmbedding = null;
		this.turnCount = 0;
		return this.activeSessionId;
	}

	/** Current session ID, or null if no active session. */
	getActiveSessionId(): string | null {
		return this.activeSessionId;
	}

	/** Current turn count within the active session. */
	getTurnCount(): number {
		return this.turnCount;
	}

	/**
	 * Record a turn of activity. Updates the clock, increments depth, and
	 * caches the embedding of the user message for future topic-continuity
	 * checks.
	 */
	async recordTurn(userMessage: string): Promise<void> {
		this.lastActivityAt = this.now();
		this.turnCount += 1;
		this.lastMessageEmbedding = await this.embeddings.embed(userMessage);
	}

	/**
	 * Release the active session. Returns the released ID (for logging) or
	 * null if no session was active. Clears all per-session state.
	 */
	releaseSession(): string | null {
		const released = this.activeSessionId;
		this.activeSessionId = null;
		this.lastActivityAt = null;
		this.coreMemoryHash = null;
		this.lastMessageEmbedding = null;
		this.turnCount = 0;
		return released;
	}

	/**
	 * Record a user correction as the outcome of the previous session
	 * decision. `correct = true` when the decision matched user intent.
	 * The self-model domain's calibration adjusts accordingly.
	 */
	async recordCorrection(correct: boolean): Promise<void> {
		await this.selfModel.recordOutcome(SELF_MODEL_DOMAIN, correct, "user");
	}

	// -------------------------------------------------------------------------
	// Internal helpers
	// -------------------------------------------------------------------------

	private isTimedOut(): boolean {
		if (this.lastActivityAt === null) return true;
		return this.now() - this.lastActivityAt > this.inactivityTimeoutMs;
	}

	private isTimedOutDeep(): boolean {
		if (this.lastActivityAt === null) return true;
		return (
			this.now() - this.lastActivityAt > this.inactivityTimeoutMs * DEEP_SESSION_TIMEOUT_MULTIPLIER
		);
	}
}

// ---------------------------------------------------------------------------
// Cosine similarity
// ---------------------------------------------------------------------------

/**
 * Cosine similarity of two equal-length vectors.
 *
 * Embeddings from the production service are L2-normalized, so this reduces
 * to a dot product in the normal case. The explicit norm division covers
 * test fixtures that may not be normalized.
 */
function cosineSimilarity(a: Float32Array, b: Float32Array): number {
	if (a.length !== b.length) {
		throw new Error(
			`Cannot compute cosine similarity: vector lengths differ (${String(a.length)} vs ${String(b.length)})`,
		);
	}
	let dot = 0;
	let normA = 0;
	let normB = 0;
	for (let i = 0; i < a.length; i++) {
		const ai = a[i] ?? 0;
		const bi = b[i] ?? 0;
		dot += ai * bi;
		normA += ai * ai;
		normB += bi * bi;
	}
	const denom = Math.sqrt(normA) * Math.sqrt(normB);
	if (denom === 0) return 0;
	return dot / denom;
}
