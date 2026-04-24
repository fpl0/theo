/**
 * Slash-command registry for the CLI gate.
 *
 * Commands are handled inside the TUI before ever reaching the chat engine.
 * They never generate durable events — they are a UX affordance, not a
 * protocol feature.
 *
 * Kept as a pure data module so tests can exercise matching without Ink.
 */

export interface SlashCommand {
	readonly name: string;
	readonly description: string;
	readonly aliases: readonly string[];
}

export const SLASH_COMMANDS: readonly SlashCommand[] = [
	{ name: "/quit", description: "Exit Theo", aliases: ["/exit"] },
	{ name: "/reset", description: "Clear session", aliases: [] },
	{ name: "/status", description: "Show engine state", aliases: [] },
	{ name: "/memory", description: "Show memory stats", aliases: [] },
	{ name: "/clear", description: "Clear screen", aliases: [] },
	{ name: "/help", description: "Show available commands", aliases: ["/?"] },
] as const;

/** All canonical slash-command names for strict matching. */
export type SlashCommandName = (typeof SLASH_COMMANDS)[number]["name"];

/**
 * Return slash commands whose canonical name or alias starts with the given
 * prefix. Empty or non-slash prefix returns an empty list.
 */
export function matchSlashCommands(prefix: string): readonly SlashCommand[] {
	if (!prefix.startsWith("/")) return [];
	return SLASH_COMMANDS.filter(
		(cmd) => cmd.name.startsWith(prefix) || cmd.aliases.some((a) => a.startsWith(prefix)),
	);
}

/** Resolve a submitted command (including aliases) to its canonical form, or null. */
export function resolveSlashCommand(text: string): SlashCommandName | null {
	const word = text.trim().split(/\s+/u)[0] ?? "";
	for (const cmd of SLASH_COMMANDS) {
		if (cmd.name === word) return cmd.name as SlashCommandName;
		if (cmd.aliases.includes(word)) return cmd.name as SlashCommandName;
	}
	return null;
}
