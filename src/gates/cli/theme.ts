/**
 * Color palette and layout constants for the CLI gate.
 *
 * Centralized so a single edit retunes the entire TUI. Components import the
 * `theme` value instead of hardcoding color strings.
 *
 * Colors follow Ink's accepted set (named colors, hex strings, or #RRGGBB).
 */

export const theme = {
	user: { label: "cyan", text: "white" },
	assistant: { label: "magenta", text: "white" },
	tool: { label: "yellow", spinner: "yellow", done: "green" },
	error: { label: "red", text: "red" },
	status: { bg: "gray", text: "white" },
	autocomplete: { border: "gray", match: "cyan", description: "gray" },
	interrupted: { text: "gray" },
} as const;

/** Max input history entries kept in the shell-style Up/Down cycle. */
export const MAX_INPUT_HISTORY = 100;

/** Prefix shown on the user's input line. */
export const USER_PROMPT = "you>";

/** Prefix shown before assistant responses. */
export const ASSISTANT_PROMPT = "theo>";
