/**
 * Visual palette and layout constants for the CLI gate.
 *
 * Hex-encoded rather than Ink's named colors — named palettes (`cyan`,
 * `magenta`, `yellow`) vary enormously across terminal themes and tend
 * toward the garish end of the spectrum. Fixed hex values give a
 * consistent look on any true-color terminal (VT100 fallback still works
 * because Ink dithers gracefully).
 *
 * Components import `theme` and `symbols` — no hardcoded colors or glyphs
 * anywhere else. A single edit here retunes the entire TUI.
 */

export const theme = {
	// Role accents.
	user: {
		label: "#22d3ee", // cyan-400, tasteful electric cyan
		text: "#e5e7eb", // zinc-200, softer than pure white
	},
	assistant: {
		label: "#a78bfa", // violet-400, Theo's signature
		text: "#e5e7eb",
	},
	system: {
		text: "#9ca3af", // zinc-400 — present but not loud
	},

	// Tool-call chips.
	tool: {
		pending: "#fbbf24", // amber-400 for in-flight
		done: "#4ade80", // green-400 for success
		error: "#f87171", // red-400 for failure
		name: "#9ca3af", // muted name text
	},

	// Status bar phase pill.
	phase: {
		idleFg: "#a1a1aa", // zinc-400
		idleBg: "#27272a", // zinc-800
		workingFg: "#fde68a", // amber-200
		workingBg: "#78350f", // amber-900
		streamingFg: "#c4b5fd", // violet-300
		streamingBg: "#4c1d95", // violet-900
		errorFg: "#fecaca", // red-200
		errorBg: "#7f1d1d", // red-900
	},

	// Chip accents in the status bar.
	chip: {
		label: "#71717a", // muted label before a value
		value: "#d4d4d8", // zinc-300 for the value itself
		accent: "#a78bfa", // for cost / token pops
		separator: "#3f3f46", // zinc-700 for the dot between chips
	},

	// Error state text.
	error: "#f87171",

	// Interrupted messages fade to dim gray.
	interrupted: "#52525b",

	// Input area.
	input: {
		prompt: "#22d3ee",
		text: "#e5e7eb",
		hint: "#52525b",
		cursor: "#a78bfa",
	},

	// Autocomplete popup.
	autocomplete: {
		border: "#3f3f46",
		match: "#22d3ee",
		description: "#71717a",
	},

	// Subtle separator between turns (inline, not a full rule).
	separator: "#27272a",

	// Header banner.
	header: {
		brand: "#a78bfa",
		tagline: "#71717a",
		border: "#27272a",
	},
} as const;

/**
 * Glyphs used across the UI. Kept minimal and terminal-safe — every one
 * renders on any monospace terminal without falling back to a replacement
 * character.
 */
export const symbols = {
	userPrompt: "›", // user line marker
	assistantPrompt: "◆", // solid diamond for Theo
	toolPending: "•", // filled circle during spinner turn (spinner is the real affordance)
	toolDone: "✓",
	toolError: "✗",
	chipSeparator: "·",
	inputPrompt: "▸",
	cursor: "▌", // half block — slim cursor, not the full 2588
} as const;

/** Max input history entries kept in the shell-style Up/Down cycle. */
export const MAX_INPUT_HISTORY = 100;

/** Labels still exported for components that mix glyph + word. */
export const USER_PROMPT = symbols.userPrompt;
export const ASSISTANT_PROMPT = symbols.assistantPrompt;
