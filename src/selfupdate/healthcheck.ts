/**
 * Self-update health check.
 *
 * Runs `just check` after startup (or after a self-update pull) and tracks
 * the commit that last passed. On boot, if the check fails, the rollback
 * path resets the repo to the tracked commit and `launchd` restarts us
 * into a known-good state.
 *
 * The tracked `healthy_commit` lives at `<workspace>/data/healthy_commit`
 * so filesystem corruption doesn't leave the value mid-update in the DB.
 * The DB's `self_update_state` singleton mirrors the FS value for
 * observability queries.
 */

import { mkdir } from "node:fs/promises";
import * as path from "node:path";
import type { Sql } from "postgres";

export interface HealthCheckResult {
	readonly ok: boolean;
	readonly commit: string;
	readonly healthyCommit: string | null;
	readonly errors?: readonly string[];
}

export interface HealthCheckDeps {
	readonly workspace: string;
	readonly sql?: Sql;
	/** Override `git rev-parse HEAD`. Tests inject a fixed sha. */
	readonly currentCommit?: () => Promise<string>;
	/** Override the check command (default: `just check`). */
	readonly runCheck?: () => Promise<{ readonly ok: boolean; readonly stderr: string }>;
	/** Override filesystem reads/writes for testing. */
	readonly readHealthy?: () => Promise<string | null>;
	readonly writeHealthy?: (commit: string) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Filesystem helpers
// ---------------------------------------------------------------------------

function healthyCommitPath(workspace: string): string {
	return path.join(workspace, "data", "healthy_commit");
}

/** Read the healthy commit file. Returns null when the file is absent. */
export async function readHealthyCommit(workspace: string): Promise<string | null> {
	const p = healthyCommitPath(workspace);
	const file = Bun.file(p);
	if (!(await file.exists())) return null;
	const text = (await file.text()).trim();
	return text.length > 0 ? text : null;
}

/** Write the healthy commit file. Creates the data directory on demand. */
export async function writeHealthyCommit(workspace: string, commit: string): Promise<void> {
	const p = healthyCommitPath(workspace);
	await mkdir(path.dirname(p), { recursive: true });
	await Bun.write(p, `${commit}\n`);
}

async function currentCommit(): Promise<string> {
	const result = await Bun.$`git rev-parse HEAD`.quiet().nothrow();
	if (result.exitCode !== 0)
		throw new Error(`git rev-parse HEAD failed: ${result.stderr.toString()}`);
	return result.stdout.toString().trim();
}

async function runJustCheck(): Promise<{ ok: boolean; stderr: string }> {
	const result = await Bun.$`just check`.quiet().nothrow();
	return { ok: result.exitCode === 0, stderr: result.stderr.toString() };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Run the health check and, on success, update `healthy_commit`. On
 * failure, return the stored commit so the caller can roll back.
 *
 * Idempotent: running twice on a healthy repo reports the same result
 * without duplicating side effects.
 */
export async function runHealthCheck(deps: HealthCheckDeps): Promise<HealthCheckResult> {
	const current = await (deps.currentCommit ?? currentCommit)();
	const read =
		deps.readHealthy ?? ((): Promise<string | null> => readHealthyCommit(deps.workspace));
	const write =
		deps.writeHealthy ??
		((commit: string): Promise<void> => writeHealthyCommit(deps.workspace, commit));
	const healthy = await read();
	const check = await (deps.runCheck ?? runJustCheck)();

	if (check.ok) {
		await write(current);
		if (deps.sql !== undefined) {
			await deps.sql`
				UPDATE self_update_state
				SET healthy_commit = ${current}, updated_at = now()
				WHERE id = 'singleton'
			`;
		}
		return { ok: true, commit: current, healthyCommit: current };
	}
	return {
		ok: false,
		commit: current,
		healthyCommit: healthy,
		errors: [check.stderr],
	};
}
