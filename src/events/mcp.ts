/**
 * Event-log MCP tools — `read_events` and `count_events`.
 *
 * Without these, Theo has no way to introspect its own event log. The
 * assistant correctly distinguishes knowledge-graph nodes (served by
 * `search_memory`) from the durable event stream, but answering "what
 * happened yesterday?" or "how many turns have I had?" requires a direct
 * read.
 *
 * Trust-gated to `owner`/`owner_confirmed` because the event stream can
 * contain arbitrary message bodies, goal text, and memory payloads — a
 * lower-tier turn should never see raw event data.
 */

import { tool } from "@anthropic-ai/claude-agent-sdk";
import type { Sql } from "postgres";
import { z } from "zod";
import { errorResult } from "../mcp/tool-helpers.ts";
import type { TrustTier } from "../memory/graph/types.ts";

export interface EventToolDeps {
	readonly sql: Sql;
	readonly resolveTrust: (extra: unknown) => TrustTier;
}

function canReadEvents(tier: TrustTier): boolean {
	return tier === "owner" || tier === "owner_confirmed";
}

/** Trim a JSON payload to a short, scannable preview. */
function previewData(data: unknown, max: number): string {
	const raw = JSON.stringify(data);
	if (raw === undefined) return "";
	return raw.length <= max ? raw : `${raw.slice(0, max)}…`;
}

export function readEventsTool(deps: EventToolDeps) {
	return tool(
		"read_events",
		"Read recent entries from the event log — Theo's immutable record of " +
			"everything that has happened (chat turns, memory mutations, scheduler " +
			"ticks, goal transitions, system lifecycle). Returns id, type, actor, " +
			"and timestamp in reverse-chronological order. Set `includeData=true` " +
			"to also include a short JSON preview of each event's payload. Use " +
			"`types` to filter by exact event type names (e.g. `turn.completed`, " +
			"`memory.node.created`). Only owner-tier turns may call this tool.",
		{
			limit: z.number().int().min(1).max(100).default(20),
			types: z.array(z.string().min(1)).optional(),
			sinceIso: z.string().min(1).optional(),
			includeData: z.boolean().default(false),
		},
		async ({ limit, types, sinceIso, includeData }, extra) => {
			try {
				const trust = deps.resolveTrust(extra);
				if (!canReadEvents(trust)) {
					return {
						content: [
							{
								type: "text",
								text:
									`Refused: read_events requires trust tier owner or owner_confirmed; ` +
									`this turn runs at ${trust}.`,
							},
						],
					};
				}
				const typeFilter =
					types !== undefined && types.length > 0
						? deps.sql`AND type = ANY(${types as string[]})`
						: deps.sql``;
				const sinceFilter =
					sinceIso !== undefined ? deps.sql`AND timestamp >= ${new Date(sinceIso)}` : deps.sql``;
				const rows = await deps.sql<
					Array<{ id: string; type: string; actor: string; timestamp: Date; data: unknown }>
				>`
					SELECT id, type, actor, timestamp, data
					FROM events
					WHERE 1=1
					${typeFilter}
					${sinceFilter}
					ORDER BY id DESC
					LIMIT ${limit}
				`;
				if (rows.length === 0) {
					return { content: [{ type: "text", text: "No events matched." }] };
				}
				const body = rows
					.map((r) => {
						const head = `[${r.timestamp.toISOString()}] ${r.type}  actor=${r.actor}  id=${r.id}`;
						return includeData ? `${head}\n  ${previewData(r.data, 240)}` : head;
					})
					.join("\n");
				return { content: [{ type: "text", text: body }] };
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function countEventsTool(deps: EventToolDeps) {
	return tool(
		"count_events",
		"Count entries in the event log, optionally grouped by type or actor, " +
			"and optionally scoped to events after `sinceIso`. Returns a total " +
			"count plus per-group counts when `groupBy` is set. Use this to " +
			"answer questions like 'how many turns this week?' or 'what kinds " +
			"of events happened today?'. Only owner-tier turns may call this tool.",
		{
			groupBy: z.enum(["type", "actor"]).optional(),
			sinceIso: z.string().min(1).optional(),
		},
		async ({ groupBy, sinceIso }, extra) => {
			try {
				const trust = deps.resolveTrust(extra);
				if (!canReadEvents(trust)) {
					return {
						content: [
							{
								type: "text",
								text:
									`Refused: count_events requires trust tier owner or owner_confirmed; ` +
									`this turn runs at ${trust}.`,
							},
						],
					};
				}
				const sinceFilter =
					sinceIso !== undefined ? deps.sql`AND timestamp >= ${new Date(sinceIso)}` : deps.sql``;
				const totalRow = await deps.sql<Array<{ n: string }>>`
					SELECT count(*)::text AS n FROM events WHERE 1=1 ${sinceFilter}
				`;
				const total = Number(totalRow[0]?.n ?? 0);
				if (groupBy === undefined) {
					return { content: [{ type: "text", text: `${String(total)} events total.` }] };
				}
				const groupCol = groupBy === "type" ? deps.sql`type` : deps.sql`actor`;
				const groupRows = await deps.sql<Array<{ k: string; n: string }>>`
					SELECT ${groupCol} AS k, count(*)::text AS n
					FROM events
					WHERE 1=1 ${sinceFilter}
					GROUP BY ${groupCol}
					ORDER BY count(*) DESC
					LIMIT 40
				`;
				const lines = groupRows.map((r) => `  ${r.k}: ${r.n}`).join("\n");
				return {
					content: [
						{
							type: "text",
							text: `${String(total)} events total, by ${groupBy}:\n${lines}`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}
