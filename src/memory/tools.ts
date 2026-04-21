import {
	createSdkMcpServer,
	type McpSdkServerConfigWithInstance,
	tool,
} from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { errorResult } from "../mcp/tool-helpers.ts";
import type { CoreMemoryRepository } from "./core.ts";
import type { EdgeRepository } from "./graph/edges.ts";
import type { NodeRepository } from "./graph/nodes.ts";
import { asNodeId } from "./graph/types.ts";
import type { RetrievalService } from "./retrieval.ts";
import type { SelfModelRepository } from "./self_model.ts";
import type { SkillRepository } from "./skills.ts";
import type { CoreMemorySlot, JsonValue } from "./types.ts";
import type { UserModelRepository } from "./user_model.ts";

export interface MemoryDependencies {
	readonly nodes: NodeRepository;
	readonly edges: EdgeRepository;
	readonly coreMemory: CoreMemoryRepository;
	readonly retrieval: RetrievalService;
	readonly userModel: UserModelRepository;
	readonly selfModel: SelfModelRepository;
	readonly skills: SkillRepository;
}

const NODE_KINDS = [
	"fact",
	"preference",
	"observation",
	"belief",
	"goal",
	"person",
	"place",
	"event",
	"pattern",
	"principle",
] as const;

// `satisfies` keeps CORE_SLOTS in lockstep with CoreMemorySlot — adding a slot
// to the union without updating this array fails to compile.
const CORE_SLOTS = [
	"persona",
	"goals",
	"user_model",
	"context",
] as const satisfies readonly CoreMemorySlot[];

// Rejects undefined: JSON.stringify(undefined) returns undefined (not a string)
// and would corrupt a JSONB column if passed through.
const jsonValueSchema = z.custom<JsonValue>(
	(value) => {
		if (value === undefined) return false;
		try {
			return typeof JSON.stringify(value) === "string";
		} catch {
			return false;
		}
	},
	{ message: "Value must be JSON-serializable" },
);

// Tool factories use inferred return types — explicit `: SdkMcpToolDefinition`
// collapses the default generic to `{readonly [x: string]: never}`.

export function storeMemoryTool(deps: MemoryDependencies) {
	return tool(
		"store_memory",
		"Store a new memory in the knowledge graph. " +
			"Use this when you learn something worth remembering — facts about the user, " +
			"their preferences, observations, or beliefs. Choose trust level based on source: " +
			"use owner_confirmed when the user directly states something, inferred when you " +
			"derive it from context.",
		{
			kind: z.enum(NODE_KINDS),
			body: z.string().min(1).max(2000),
			sensitivity: z.enum(["none", "sensitive", "restricted"]).default("none"),
			trust: z.enum(["owner_confirmed", "inferred", "external", "untrusted"]).default("inferred"),
		},
		async ({ kind, body, sensitivity, trust }) => {
			try {
				const node = await deps.nodes.create({
					kind,
					body,
					sensitivity,
					trust,
					actor: "theo",
				});
				return {
					content: [
						{
							type: "text",
							text: `Stored memory #${String(node.id)}: ${body.slice(0, 100)}`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function searchMemoryTool(deps: MemoryDependencies) {
	return tool(
		"search_memory",
		"Search your memory for relevant knowledge. " +
			"Returns memories ranked by relevance using vector similarity, keyword matching, " +
			"and graph connections. Use this before answering questions that depend on what " +
			"you know about the user, or to check if you already know something before " +
			"storing a duplicate.",
		{
			query: z.string().min(1),
			limit: z.number().int().min(1).max(50).default(10),
			kinds: z.array(z.enum(NODE_KINDS)).optional(),
		},
		async ({ query, limit, kinds }) => {
			try {
				const results = await deps.retrieval.search(query, kinds ? { limit, kinds } : { limit });
				const text = results
					.map(
						(r) =>
							`[#${String(r.node.id)} ${r.node.kind}] ` +
							`(score: ${r.score.toFixed(3)}) ${r.node.body}`,
					)
					.join("\n\n");
				return {
					content: [{ type: "text", text: text || "No memories found." }],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function readCoreTool(deps: MemoryDependencies) {
	return tool(
		"read_core",
		"Read your core memory — persona, goals, user model summary, and current " +
			"context. This is your persistent identity and working state. Core memory is " +
			"assembled into your system prompt at session start, but use this tool to inspect " +
			"the raw values or check for staleness.",
		{},
		async () => {
			try {
				const core = await deps.coreMemory.read();
				return {
					content: [{ type: "text", text: JSON.stringify(core, null, 2) }],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function updateCoreTool(deps: MemoryDependencies) {
	return tool(
		"update_core",
		"Update a core memory slot. Use sparingly — these define your identity, " +
			"goals, and working context. Every change is permanent and changelogged. Prefer " +
			"store_memory for ordinary facts; reserve this for fundamental shifts in persona, " +
			"goals, user model summary, or current context.",
		{
			slot: z.enum(CORE_SLOTS),
			body: jsonValueSchema,
		},
		async ({ slot, body }) => {
			try {
				await deps.coreMemory.update(slot, body, "theo");
				return { content: [{ type: "text", text: `Updated core memory: ${slot}` }] };
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function linkMemoriesTool(deps: MemoryDependencies) {
	return tool(
		"link_memories",
		"Create a relationship between two memories. " +
			"Use to connect related concepts (relates_to), mark contradictions (contradicts), " +
			"build causal chains (caused_by), or note supersession (supersedes). Links " +
			"strengthen retrieval — connected memories surface together.",
		{
			sourceId: z.number().int(),
			targetId: z.number().int(),
			label: z.string().min(1),
			weight: z.number().min(0).max(5).default(1.0),
		},
		async ({ sourceId, targetId, label, weight }) => {
			try {
				await deps.edges.create({
					sourceId: asNodeId(sourceId),
					targetId: asNodeId(targetId),
					label,
					weight,
					actor: "theo",
				});
				return {
					content: [
						{
							type: "text",
							text: `Linked #${String(sourceId)} -> #${String(targetId)} (${label})`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function updateUserModelTool(deps: MemoryDependencies) {
	return tool(
		"update_user_model",
		"Update your understanding of the user along a behavioral or psychological " +
			"dimension. Unlike store_memory (discrete facts), this tracks evolving patterns — " +
			"communication style, technical depth, emotional tendencies. Confidence grows " +
			"with evidence count. Use when you notice a recurring pattern, not for one-off " +
			"observations.",
		{
			dimension: z.string().min(1),
			value: jsonValueSchema,
			evidence: z.number().int().min(1).default(1),
		},
		async ({ dimension, value, evidence }) => {
			try {
				const dim = await deps.userModel.updateDimension(dimension, value, evidence, "theo");
				return {
					content: [
						{
							type: "text",
							text: `Updated ${dimension} (confidence: ${dim.confidence.toFixed(2)})`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function storeSkillTool(deps: MemoryDependencies) {
	return tool(
		"store_skill",
		"Save a learned strategy as a reusable procedural skill. " +
			"Use when you recognise a recurring situation that has a repeatable " +
			"solution. The `trigger` is a short phrase describing the situation; " +
			"the `strategy` is the repeatable approach. Pass `parentId` when refining " +
			"an existing skill — the new version supersedes it without deleting the lineage.",
		{
			name: z.string().min(1).max(120),
			trigger: z.string().min(1).max(500),
			strategy: z.string().min(1).max(4000),
			parentId: z.number().int().positive().optional(),
		},
		async ({ name, trigger, strategy, parentId }) => {
			try {
				const input =
					parentId !== undefined
						? { name, trigger, strategy, parentId }
						: { name, trigger, strategy };
				const skill = await deps.skills.create(input);
				const lineage =
					skill.parentId !== null
						? ` (v${String(skill.version)}, refines #${String(skill.parentId)})`
						: ` (v${String(skill.version)})`;
				return {
					content: [
						{
							type: "text",
							text: `Stored skill #${String(skill.id)} "${skill.name}"${lineage}`,
						},
					],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function searchSkillsTool(deps: MemoryDependencies) {
	return tool(
		"search_skills",
		"Search your procedural memory for learned strategies. " +
			"Use when facing a task you might have handled before — coding patterns, " +
			"communication approaches, problem-solving methods. Returns skills ranked by " +
			"trigger similarity and success rate.",
		{
			query: z.string().min(1),
			limit: z.number().int().min(1).max(10).default(3),
		},
		async ({ query, limit }) => {
			try {
				const skills = await deps.skills.findByTrigger(query, limit);
				const text = skills
					.map(
						(s) =>
							`[skill #${String(s.id)}] ` +
							`(success: ${(s.successRate * 100).toFixed(0)}%, v${String(s.version)}) ` +
							`${s.trigger}\n  Strategy: ${s.strategy}`,
					)
					.join("\n\n");
				return {
					content: [{ type: "text", text: text || "No matching skills found." }],
				};
			} catch (error) {
				return errorResult(error);
			}
		},
	);
}

export function memoryToolList(deps: MemoryDependencies) {
	return [
		storeMemoryTool(deps),
		searchMemoryTool(deps),
		storeSkillTool(deps),
		searchSkillsTool(deps),
		readCoreTool(deps),
		updateCoreTool(deps),
		linkMemoriesTool(deps),
		updateUserModelTool(deps),
	];
}

// Tool name prefixing follows `mcp__${mapKey}__${toolName}` where `mapKey` is
// the caller's `mcpServers: { memory: ... }` key, NOT the `name` below. The
// two must stay in sync or `allowedTools: ["mcp__memory__*"]` silently misses.
export function createMemoryServer(deps: MemoryDependencies): McpSdkServerConfigWithInstance {
	return createSdkMcpServer({
		name: "memory",
		tools: memoryToolList(deps),
	});
}
