/**
 * Proposal workspace discipline — branch naming + env scrubbing.
 *
 * Every proposal that requires an external artifact (a branch, a draft PR,
 * a Gmail draft) goes through this module so the naming convention and the
 * env scrub stay consistent across proposal kinds. The actual git / Gmail
 * operations are effect-side (Phase 15 operational wiring); this module
 * provides the pure helpers that the effect layer must use.
 */

// ---------------------------------------------------------------------------
// Branch naming
// ---------------------------------------------------------------------------

const SLUG_CLEAN = /[^a-z0-9-]+/g;
const SLUG_TRIM_DASHES = /^-+|-+$/g;
const MAX_SLUG_LEN = 40;
/**
 * Alphanumeric + hyphen only. Refuses `.`, `/`, and whitespace so a
 * malicious caller cannot path-traverse out of the workspace branch
 * namespace (`theo/proposal/<id>/...`). ULIDs always pass.
 */
const SAFE_ID_RE = /^[A-Za-z0-9-]+$/;

/**
 * Build a branch name of the form `theo/proposal/<id>/<slug>`. Draft PRs
 * only, never auto-merged; the workspace GC uses this prefix to sweep
 * expired branches.
 */
export function proposalBranchName(proposalId: string, summary: string): string {
	if (!SAFE_ID_RE.test(proposalId)) {
		throw new Error("proposalBranchName: invalid proposalId (path-unsafe characters)");
	}
	const slug = summary
		.toLowerCase()
		.replace(SLUG_CLEAN, "-")
		.replace(SLUG_TRIM_DASHES, "")
		.slice(0, MAX_SLUG_LEN);
	return `theo/proposal/${proposalId}/${slug || "item"}`;
}

// ---------------------------------------------------------------------------
// Env scrubbing (§7 — subagent dispatch env isolation)
// ---------------------------------------------------------------------------

/**
 * Patterns to strip from the environment before spawning a subagent. We
 * never let the subagent see the owner's API keys, database URL, secret
 * webhooks, or anything tagged `*_KEY` / `*_SECRET` / `*_TOKEN`.
 */
export const SCRUB_PATTERNS: readonly RegExp[] = [
	/^ANTHROPIC_/,
	/^TELEGRAM_/,
	/^DATABASE_URL/,
	/^WEBHOOK_SECRET_/,
	/^AWS_/,
	/^GITHUB_TOKEN/,
	/^OPENAI_/,
	/_KEY$/,
	/_SECRET$/,
	/_TOKEN$/,
] as const;

/**
 * Produce a scrubbed env object suitable for spawning a subprocess. Pure,
 * deterministic — the same input yields the same output.
 */
export function scrubEnv(env: Record<string, string | undefined>): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [k, v] of Object.entries(env)) {
		if (v === undefined) continue;
		if (SCRUB_PATTERNS.some((p) => p.test(k))) continue;
		out[k] = v;
	}
	return out;
}

// ---------------------------------------------------------------------------
// PR body template (`foundation.md §7.7`)
// ---------------------------------------------------------------------------

export interface ProposalPrMeta {
	readonly proposalId: string;
	readonly sourceCauseId: string;
	readonly originEventId: string;
	readonly reasoning: string;
}

/**
 * Build the PR description body. Embeds proposal id + causation + full
 * reasoning so squash merges don't lose the audit trail.
 */
export function buildPrBody(meta: ProposalPrMeta): string {
	return [
		`## Theo proposal ${meta.proposalId}`,
		"",
		"Draft PR — owner review required before merge.",
		"",
		"### Causation",
		`- source cause: ${meta.sourceCauseId}`,
		`- origin event: ${meta.originEventId}`,
		"",
		"### Reasoning",
		meta.reasoning,
	].join("\n");
}
