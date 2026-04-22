/**
 * Resource attribute builder — attached to every span/metric/log.
 *
 * The SDK is bootstrapped once with a `Resource` derived from:
 *   - `service.name` (constant "theo")
 *   - `service.version` (git sha — the self-update path changes this)
 *   - `service.instance.id` (hostname)
 *   - `deployment.environment` ("prod" | "dev" | "test")
 *   - runtime + host attributes
 *
 * The shape is a plain object — kept SDK-agnostic so the no-op tracer and
 * any future OTLP SDK both consume the same data.
 */

import { hostname } from "node:os";
import * as Sem from "./semconv.ts";

export type Environment = "prod" | "dev" | "test";

export interface ResourceConfig {
	readonly environment: Environment;
	/**
	 * Git SHA to use as `service.version`. When omitted, the builder runs
	 * `git rev-parse HEAD` at construction time. Tests inject a fixed value
	 * so assertions don't depend on the current commit.
	 */
	readonly gitSha?: string;
	readonly instanceId?: string;
}

/** Snapshot of every resource attribute. */
export type ResourceAttributes = Readonly<Record<string, string>>;

/**
 * Build the resource attributes. Reads git SHA via `Bun.$` when not
 * supplied; falls back to `"unknown"` when the command fails (test
 * harnesses, non-git checkouts).
 */
export async function buildResource(config: ResourceConfig): Promise<ResourceAttributes> {
	const gitSha = config.gitSha ?? (await resolveGitSha());
	const instance = config.instanceId ?? hostname();
	return {
		[Sem.ATTR_SERVICE_NAME]: "theo",
		[Sem.ATTR_SERVICE_VERSION]: gitSha,
		[Sem.ATTR_SERVICE_INSTANCE_ID]: instance,
		[Sem.ATTR_DEPLOYMENT_ENVIRONMENT]: config.environment,
		[Sem.ATTR_PROCESS_RUNTIME_NAME]: "bun",
		[Sem.ATTR_PROCESS_RUNTIME_VERSION]: Bun.version,
		[Sem.ATTR_HOST_OS_TYPE]: process.platform,
		[Sem.ATTR_HOST_ARCH]: process.arch,
	};
}

async function resolveGitSha(): Promise<string> {
	try {
		const result = await Bun.$`git rev-parse HEAD`.quiet().nothrow();
		if (result.exitCode !== 0) return "unknown";
		return result.stdout.toString().trim();
	} catch {
		return "unknown";
	}
}
