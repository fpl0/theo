/**
 * Self-update rollback.
 *
 * When the startup health check fails, the engine invokes
 * `rollbackToHealthy()`. The workspace's `healthy_commit` file is the
 * source of truth — a missing file aborts with an error (first-run seeds
 * it; an operator-induced missing file is not safe to silently paper over).
 *
 * On success, emits `system.rollback` with the old → new commit transition,
 * updates the DB singleton, and returns. `launchd` restarts the process
 * into the rolled-back commit.
 */

import type { Sql } from "postgres";
import type { EventBus } from "../events/bus.ts";
import { readHealthyCommit } from "./healthcheck.ts";

export interface RollbackDeps {
	readonly workspace: string;
	readonly bus: EventBus;
	readonly sql?: Sql;
	readonly reason?: string;
	/** Override git rev-parse HEAD (tests). */
	readonly currentCommit?: () => Promise<string>;
	/** Override git reset --hard (tests). */
	readonly gitReset?: (commit: string) => Promise<void>;
	/** Override filesystem read (tests). */
	readonly readHealthy?: () => Promise<string | null>;
}

export interface RollbackResult {
	readonly from: string;
	readonly to: string;
}

async function gitResetHard(commit: string): Promise<void> {
	const result = await Bun.$`git reset --hard ${commit}`.quiet().nothrow();
	if (result.exitCode !== 0) {
		throw new Error(`git reset --hard ${commit} failed: ${result.stderr.toString()}`);
	}
}

async function currentCommit(): Promise<string> {
	const result = await Bun.$`git rev-parse HEAD`.quiet().nothrow();
	if (result.exitCode !== 0) {
		throw new Error(`git rev-parse HEAD failed: ${result.stderr.toString()}`);
	}
	return result.stdout.toString().trim();
}

/**
 * Reset the working tree to the healthy commit and emit `system.rollback`.
 * Throws when no healthy commit is recorded — the caller must treat this
 * as a fatal startup failure rather than continuing on a broken tree.
 */
export async function rollbackToHealthy(deps: RollbackDeps): Promise<RollbackResult> {
	const read =
		deps.readHealthy ?? ((): Promise<string | null> => readHealthyCommit(deps.workspace));
	const healthy = await read();
	if (healthy === null) {
		throw new Error("rollbackToHealthy: no healthy commit recorded; cannot roll back");
	}
	const from = await (deps.currentCommit ?? currentCommit)();
	await (deps.gitReset ?? gitResetHard)(healthy);

	await deps.bus.emit({
		type: "system.rollback",
		version: 1,
		actor: "system",
		data: {
			fromCommit: from,
			toCommit: healthy,
			reason: deps.reason ?? "healthcheck_failed",
		},
		metadata: {},
	});

	if (deps.sql !== undefined) {
		await deps.sql`
			UPDATE self_update_state
			SET last_rollback_at = now(), last_rollback_to = ${healthy}, updated_at = now()
			WHERE id = 'singleton'
		`;
	}

	return { from, to: healthy };
}
