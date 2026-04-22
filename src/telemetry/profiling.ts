/**
 * Continuous profiling client.
 *
 * Plan §Continuous profiling (Pyroscope) specifies `@pyroscope/nodejs` —
 * but that package pulls in `@datadog/pprof`, whose native prebuild
 * (`darwin-arm64/node-137.node`) segfaults Bun at load time on 1.3.13.
 *
 * Two paths considered:
 *
 *   1. Ship Pyroscope anyway and guard the `import` behind a try/catch.
 *      Rejected: the binding crashes the process; guarding the import does
 *      not survive the native dlopen panic.
 *   2. No-op client with a `THEO_PYROSCOPE_ENABLED` env guard and a
 *      documented upgrade path. Chosen: this keeps the surface area for a
 *      later SDK swap minimal while not regressing stability.
 *
 * When Pyroscope (or an alternative continuous-profiling SDK that loads
 * cleanly on Bun) becomes available, replace the body of `startProfiling`
 * with the real init call. The shape of this module does not change.
 */

import type { TheoLogger } from "./logger.ts";

export interface ProfilingConfig {
	/** Master switch — env `THEO_PYROSCOPE_ENABLED=true`. */
	readonly enabled: boolean;
	/** Pyroscope server URL — ignored when disabled. */
	readonly serverAddress: string;
	/** Application name tag. */
	readonly appName: string;
	/** Additional tags (e.g., git sha, instance). */
	readonly tags: Readonly<Record<string, string>>;
}

export const DEFAULT_PROFILING_CONFIG: ProfilingConfig = {
	enabled: false,
	serverAddress: "http://localhost:4040",
	appName: "theo",
	tags: {},
};

/** A handle to stop profiling on shutdown. */
export interface ProfilingHandle {
	readonly stop: () => Promise<void>;
}

/**
 * Start continuous profiling. Always returns a handle — a no-op when
 * disabled or when the SDK is not loadable on the current runtime.
 */
export function startProfiling(config: ProfilingConfig, logger?: TheoLogger): ProfilingHandle {
	if (!config.enabled) {
		return { stop: async (): Promise<void> => Promise.resolve() };
	}

	// Pyroscope's native `@datadog/pprof` prebuilt segfaults Bun — emit a
	// one-line warning so operators see why their Pyroscope panels stay empty.
	logger?.warn("profiling skipped — Pyroscope is disabled on Bun (native pprof incompatibility)", {
		runtime: "bun",
		serverAddress: config.serverAddress,
		appName: config.appName,
	});
	return { stop: async (): Promise<void> => Promise.resolve() };
}

/** Read profiling config from env. Safe to call at startup. */
export function loadProfilingConfig(
	env: Record<string, string | undefined> = process.env,
	extraTags: Readonly<Record<string, string>> = {},
): ProfilingConfig {
	const enabled = env["THEO_PYROSCOPE_ENABLED"] === "true";
	return {
		...DEFAULT_PROFILING_CONFIG,
		enabled,
		serverAddress: env["THEO_PYROSCOPE_URL"] ?? DEFAULT_PROFILING_CONFIG.serverAddress,
		tags: { ...extraTags },
	};
}
