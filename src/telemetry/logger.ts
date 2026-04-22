/**
 * TheoLogger — structured JSON logger, module-internal.
 *
 * Writes one JSON line per entry to stdout and optionally to a rotating
 * file under `~/Theo/logs/theo-YYYY-MM-DD.log`. Daily rotation; older
 * files can be gzipped by the surrounding ops tooling.
 *
 * The logger is NOT exported from `src/telemetry/index.ts` — business code
 * doesn't call it. The projector and the tracer do. If a domain emits a
 * `.warn`-level fact, it should emit a domain event; the projector's
 * handler for that event is allowed to log.
 */

import * as os from "node:os";
import * as path from "node:path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

export interface LogEntry {
	readonly timestamp: string;
	readonly level: LogLevel;
	readonly message: string;
	readonly component: string;
	readonly traceId?: string;
	readonly spanId?: string;
	readonly attributes: Record<string, unknown>;
}

export interface LoggerConfig {
	readonly level?: LogLevel;
	/** Optional log directory; when unset only stdout receives entries. */
	readonly logDir?: string;
	/** Called instead of `console.log` in tests so output is capturable. */
	readonly stdoutSink?: (line: string) => void;
	/** Called instead of actually appending to disk in tests. */
	readonly fileSink?: (filePath: string, line: string) => Promise<void>;
	/** Override clock for deterministic test output. */
	readonly now?: () => Date;
	/**
	 * When set, the logger consults this callback to attach the current
	 * trace/span context to every entry. `initTelemetry` wires it to the
	 * tracer's active-context getter.
	 */
	readonly activeContext?: () => { readonly traceId: string; readonly spanId: string } | null;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

export class TheoLogger {
	private readonly level: LogLevel;
	private readonly logDir: string | undefined;
	private readonly stdoutSink: (line: string) => void;
	private readonly fileSink: (filePath: string, line: string) => Promise<void>;
	private readonly now: () => Date;
	private readonly activeContext: (() => { traceId: string; spanId: string } | null) | undefined;

	constructor(config: LoggerConfig = {}) {
		this.level = config.level ?? "info";
		this.logDir = config.logDir;
		this.stdoutSink = config.stdoutSink ?? ((line: string): void => console.log(line));
		this.fileSink = config.fileSink ?? defaultFileSink;
		this.now = config.now ?? ((): Date => new Date());
		this.activeContext = config.activeContext;
	}

	debug(message: string, attributes?: Record<string, unknown>): void {
		this.emit("debug", "theo", message, attributes ?? {});
	}
	info(message: string, attributes?: Record<string, unknown>): void {
		this.emit("info", "theo", message, attributes ?? {});
	}
	warn(message: string, attributes?: Record<string, unknown>): void {
		this.emit("warn", "theo", message, attributes ?? {});
	}
	error(message: string, attributes?: Record<string, unknown>): void {
		this.emit("error", "theo", message, attributes ?? {});
	}

	/** Emit with an explicit component label. */
	log(
		level: LogLevel,
		component: string,
		message: string,
		attributes?: Record<string, unknown>,
	): void {
		this.emit(level, component, message, attributes ?? {});
	}

	private emit(
		level: LogLevel,
		component: string,
		message: string,
		attributes: Record<string, unknown>,
	): void {
		if (LEVEL_ORDER[level] < LEVEL_ORDER[this.level]) return;
		const ts = this.now();
		const ctx = this.activeContext?.() ?? null;
		const entry: LogEntry = {
			timestamp: ts.toISOString(),
			level,
			message,
			component,
			attributes,
			...(ctx !== null ? { traceId: ctx.traceId, spanId: ctx.spanId } : {}),
		};
		const line = JSON.stringify(entry);
		this.stdoutSink(line);
		if (this.logDir !== undefined) {
			void this.fileSink(fileFor(this.logDir, ts), line).catch(() => {
				// File write failures are reported to stdout only — we never throw
				// from the logger.
				this.stdoutSink(JSON.stringify({ ...entry, attributes: { logger: "file_write_failed" } }));
			});
		}
	}
}

// ---------------------------------------------------------------------------
// Filesystem sink — append to a daily-rotated file
// ---------------------------------------------------------------------------

function fileFor(logDir: string, when: Date): string {
	const y = String(when.getUTCFullYear()).padStart(4, "0");
	const m = String(when.getUTCMonth() + 1).padStart(2, "0");
	const d = String(when.getUTCDate()).padStart(2, "0");
	return path.join(logDir, `theo-${y}-${m}-${d}.log`);
}

async function defaultFileSink(filePath: string, line: string): Promise<void> {
	const file = Bun.file(filePath);
	const existing = (await file.exists()) ? await file.text() : "";
	await Bun.write(filePath, `${existing}${line}\n`);
}

/** Expose the path-for-date helper so tests can assert file names without mocks. */
export function logFilePath(logDir: string, when: Date): string {
	return fileFor(logDir, when);
}

/** Expose level comparison for tests. */
export function shouldEmit(configured: LogLevel, candidate: LogLevel): boolean {
	return LEVEL_ORDER[candidate] >= LEVEL_ORDER[configured];
}

/** Convenience: expand `~` in a log-dir path. */
export function expandHome(p: string): string {
	if (p === "~") return os.homedir();
	if (p.startsWith("~/")) return path.join(os.homedir(), p.slice(2));
	return p;
}
