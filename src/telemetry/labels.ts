/**
 * Closed-set label enums for every metric that carries a label.
 *
 * Cardinality discipline: a label value must come from an enum here, be
 * guarded by `assertLabel`, and the metric emission site must reference the
 * enum. Unknown values are replaced with `"unknown"` and counted via
 * `theo.telemetry.cardinality_rejections_total`.
 *
 * This file is the single place that changes when a new reason / gate /
 * model is introduced — a grep for `assertLabel` proves no free-form label
 * escapes the perimeter.
 */

// ---------------------------------------------------------------------------
// Enum sets
// ---------------------------------------------------------------------------

export const GATES = [
	"cli.owner",
	"telegram.owner",
	"webhook.reflex",
	"internal.scheduler",
	"unknown",
] as const;
export type Gate = (typeof GATES)[number];

export const MODELS = [
	"claude-opus-4-7",
	"claude-sonnet-4-6",
	"claude-haiku-4-5",
	"unknown",
] as const;
export type Model = (typeof MODELS)[number];

/**
 * Execution role for one SDK iteration inside a turn.
 *
 *   - `executor`  — the main executor call (counts against executor rate)
 *   - `advisor`   — an `advisor_message` iteration (counts against advisor rate)
 *   - `ideation`  — an ideation subagent iteration
 */
export const ROLES = ["executor", "advisor", "ideation", "unknown"] as const;
export type Role = (typeof ROLES)[number];

export const TURN_STATUSES = ["ok", "failed", "aborted", "unknown"] as const;
export type TurnStatus = (typeof TURN_STATUSES)[number];

export const HANDLER_ERROR_REASONS = [
	"db_error",
	"validation_error",
	"upcaster_error",
	"timeout",
	"unknown",
] as const;
export type HandlerErrorReason = (typeof HANDLER_ERROR_REASONS)[number];

export const REFLEX_REJECT_REASONS = [
	"signature_invalid",
	"source_denied",
	"rate_limited",
	"schema_invalid",
	"stale",
	"unknown",
] as const;
export type ReflexRejectReason = (typeof REFLEX_REJECT_REASONS)[number];

export const AUTONOMY_DOMAINS = [
	"git_write",
	"github_api",
	"cloud_api",
	"filesystem",
	"network",
	"shell",
	"memory_write",
	"unknown",
] as const;
export type AutonomyDomain = (typeof AUTONOMY_DOMAINS)[number];

export const PROPOSAL_KINDS = [
	"new_goal",
	"goal_plan",
	"memory_write",
	"code_change",
	"message_draft",
	"calendar_hold",
	"workflow_change",
	"unknown",
] as const;
export type ProposalKindLabel = (typeof PROPOSAL_KINDS)[number];

export const TURN_CLASSES = ["interactive", "reflex", "executive", "ideation", "unknown"] as const;
export type TurnClassLabel = (typeof TURN_CLASSES)[number];

/** Exporter signal type — used as a label on `exporter_dropped_total`. */
export const EXPORTER_SIGNALS = ["span", "metric", "log", "unknown"] as const;
export type ExporterSignal = (typeof EXPORTER_SIGNALS)[number];

/** Probe failure bucket. */
export const PROBE_FAIL_REASONS = ["timeout", "not_ok", "exception", "unknown"] as const;
export type ProbeFailReason = (typeof PROBE_FAIL_REASONS)[number];

// ---------------------------------------------------------------------------
// assertLabel
// ---------------------------------------------------------------------------

/**
 * Registry hook the metrics module wires up so `assertLabel` can increment
 * `theo.telemetry.cardinality_rejections_total` when a value falls through
 * the guard. Kept as a setter to avoid a cyclic import on `metrics.ts`.
 */
type RejectFn = (metric: string, label: string) => void;
let onReject: RejectFn = (): void => {
	// default: drop silently. Registered by `initMetrics`.
};

/** Wire the rejection counter. Called once from `initMetrics`. */
export function registerCardinalityRejectSink(sink: RejectFn): void {
	onReject = sink;
}

/**
 * Normalize an observed label value against its closed-set enum. Returns
 * the value when it belongs to the set, otherwise `"unknown"` (and counts
 * the rejection).
 */
export function assertLabel<T extends readonly string[]>(
	enumSet: T,
	value: string,
	metric: string,
	label: string,
): T[number] {
	if ((enumSet as readonly string[]).includes(value)) return value as T[number];
	onReject(metric, label);
	return "unknown" as T[number];
}

// ---------------------------------------------------------------------------
// Helpers for metric emission sites
// ---------------------------------------------------------------------------

export const asGate = (v: string, metric: string): Gate =>
	assertLabel(GATES, v, metric, "gate") as Gate;
export const asModel = (v: string, metric: string): Model =>
	assertLabel(MODELS, v, metric, "model") as Model;
export const asRole = (v: string, metric: string): Role =>
	assertLabel(ROLES, v, metric, "role") as Role;
export const asTurnStatus = (v: string, metric: string): TurnStatus =>
	assertLabel(TURN_STATUSES, v, metric, "status") as TurnStatus;
export const asHandlerErrorReason = (v: string, metric: string): HandlerErrorReason =>
	assertLabel(HANDLER_ERROR_REASONS, v, metric, "reason") as HandlerErrorReason;
export const asAutonomyDomain = (v: string, metric: string): AutonomyDomain =>
	assertLabel(AUTONOMY_DOMAINS, v, metric, "domain") as AutonomyDomain;
export const asProposalKind = (v: string, metric: string): ProposalKindLabel =>
	assertLabel(PROPOSAL_KINDS, v, metric, "kind") as ProposalKindLabel;
export const asTurnClass = (v: string, metric: string): TurnClassLabel =>
	assertLabel(TURN_CLASSES, v, metric, "turn_class") as TurnClassLabel;
export const asReflexRejectReason = (v: string, metric: string): ReflexRejectReason =>
	assertLabel(REFLEX_REJECT_REASONS, v, metric, "reason") as ReflexRejectReason;
export const asExporterSignal = (v: string, metric: string): ExporterSignal =>
	assertLabel(EXPORTER_SIGNALS, v, metric, "signal") as ExporterSignal;
export const asProbeFailReason = (v: string, metric: string): ProbeFailReason =>
	assertLabel(PROBE_FAIL_REASONS, v, metric, "reason") as ProbeFailReason;
