/**
 * Multiline input editor with slash-command autocomplete and shell-style
 * history.
 *
 * Built directly on Ink's `useInput` because `@inkjs/ui`'s TextInput is
 * single-line only. The scope is intentionally a textarea, not an IDE:
 *   - cursor movement within and between lines
 *   - Enter submits (when non-empty); Shift+Enter / Alt+Enter / Ctrl+J inserts a newline
 *   - Up/Down navigates history when on the first/last line of empty/short input
 *   - Tab accepts the top autocomplete match
 *   - Esc dismisses the autocomplete popup (or clears input if none shown)
 *   - Ctrl+C forwards to `onAbort` (engine decides if this is abort-turn or quit)
 *
 * No word wrap, no syntax highlighting, no mouse selection.
 */

import { Box, Text, useInput } from "ink";
import type React from "react";
import { useCallback, useMemo, useState } from "react";
import { matchSlashCommands, type SlashCommand } from "../commands.ts";
import type { TuiState } from "../state.ts";
import { theme, USER_PROMPT } from "../theme.ts";

export interface InputAreaProps {
	readonly state: TuiState;
	readonly history: readonly string[];
	readonly onSubmit: (text: string) => void;
	readonly onAbort: () => void;
}

export function InputArea({
	state,
	history,
	onSubmit,
	onAbort,
}: InputAreaProps): React.JSX.Element {
	const [lines, setLines] = useState<string[]>([""]);
	const [row, setRow] = useState(0);
	const [col, setCol] = useState(0);
	// -1 means "live input"; 0..history.length-1 indexes history.
	const [, setHistoryCursor] = useState(-1);

	const currentText = lines.join("\n");
	const lastLine = lines[lines.length - 1] ?? "";
	const firstLine = lines[0] ?? "";
	// Autocomplete on the first line only — the input is a command if and only
	// if it starts with `/`.
	const autocompleteMatches: readonly SlashCommand[] = useMemo(() => {
		if (lines.length !== 1) return [];
		return matchSlashCommands(firstLine);
	}, [lines.length, firstLine]);

	const replaceInput = useCallback((text: string) => {
		const next = text.length === 0 ? [""] : text.split("\n");
		setLines(next);
		const lastIdx = next.length - 1;
		setRow(lastIdx);
		setCol((next[lastIdx] ?? "").length);
	}, []);

	const clearInput = useCallback(() => {
		setLines([""]);
		setRow(0);
		setCol(0);
		setHistoryCursor(-1);
	}, []);

	const submit = useCallback(() => {
		const text = currentText;
		if (text.length === 0) return;
		clearInput();
		onSubmit(text);
	}, [currentText, clearInput, onSubmit]);

	const insertText = useCallback(
		(text: string) => {
			setLines((prev) => {
				const copy = [...prev];
				const line = copy[row] ?? "";
				copy[row] = line.slice(0, col) + text + line.slice(col);
				return copy;
			});
			setCol((c) => c + text.length);
			setHistoryCursor(-1);
		},
		[row, col],
	);

	const insertNewline = useCallback(() => {
		setLines((prev) => {
			const copy = [...prev];
			const line = copy[row] ?? "";
			const before = line.slice(0, col);
			const after = line.slice(col);
			copy.splice(row, 1, before, after);
			return copy;
		});
		setRow((r) => r + 1);
		setCol(0);
		setHistoryCursor(-1);
	}, [row, col]);

	const backspace = useCallback(() => {
		setLines((prev) => {
			const copy = [...prev];
			const line = copy[row] ?? "";
			if (col > 0) {
				copy[row] = line.slice(0, col - 1) + line.slice(col);
				return copy;
			}
			if (row > 0) {
				const prevLine = copy[row - 1] ?? "";
				copy[row - 1] = prevLine + line;
				copy.splice(row, 1);
				return copy;
			}
			return copy;
		});
		if (col > 0) {
			setCol((c) => c - 1);
		} else if (row > 0) {
			const prevLen = (lines[row - 1] ?? "").length;
			setRow((r) => r - 1);
			setCol(prevLen);
		}
		setHistoryCursor(-1);
	}, [row, col, lines]);

	const navigateHistory = useCallback(
		(direction: "up" | "down") => {
			if (history.length === 0) return;
			setHistoryCursor((prev) => {
				const next =
					direction === "up" ? Math.min(prev + 1, history.length - 1) : Math.max(prev - 1, -1);
				if (next === prev) return prev;
				const text = next >= 0 ? (history[next] ?? "") : "";
				replaceInput(text);
				return next;
			});
		},
		[history, replaceInput],
	);

	const acceptAutocomplete = useCallback(() => {
		const top = autocompleteMatches[0];
		if (top === undefined) return;
		replaceInput(top.name);
		setHistoryCursor(-1);
	}, [autocompleteMatches, replaceInput]);

	// useInput must stay active during processing/streaming so Ctrl+C can
	// interrupt a running turn. Text editing is gated per-key below.
	const isBusy = state.phase === "processing" || state.phase === "streaming";
	useInput((input, key) => {
		// Ctrl+C: always intercepted — the gate decides abort vs. quit.
		if (key.ctrl && input === "c") {
			onAbort();
			return;
		}

		// During a turn, ignore all other keys — the user can't edit while
		// Theo is thinking. Ctrl+C is the only affordance.
		if (isBusy) return;

		// Shift+Enter or Alt+Enter inserts a newline. Ctrl+J is a universal
		// fallback for terminals that don't distinguish shift+enter.
		if (key.return && (key.shift || key.meta)) {
			insertNewline();
			return;
		}
		if (key.ctrl && input === "j") {
			insertNewline();
			return;
		}

		if (key.return) {
			submit();
			return;
		}

		if (key.tab) {
			if (autocompleteMatches.length > 0) acceptAutocomplete();
			return;
		}

		if (key.escape) {
			// Dismiss autocomplete if showing; otherwise clear the input.
			// Either way, clearing is the simplest, least surprising action.
			clearInput();
			return;
		}

		if (key.backspace || key.delete) {
			backspace();
			return;
		}

		if (key.leftArrow) {
			if (col > 0) setCol((c) => c - 1);
			else if (row > 0) {
				const prevLen = (lines[row - 1] ?? "").length;
				setRow((r) => r - 1);
				setCol(prevLen);
			}
			return;
		}

		if (key.rightArrow) {
			if (col < (lines[row] ?? "").length) setCol((c) => c + 1);
			else if (row < lines.length - 1) {
				setRow((r) => r + 1);
				setCol(0);
			}
			return;
		}

		if (key.upArrow) {
			if (row > 0) {
				setRow((r) => r - 1);
				setCol((c) => Math.min(c, (lines[row - 1] ?? "").length));
			} else {
				navigateHistory("up");
			}
			return;
		}

		if (key.downArrow) {
			if (row < lines.length - 1) {
				setRow((r) => r + 1);
				setCol((c) => Math.min(c, (lines[row + 1] ?? "").length));
			} else {
				navigateHistory("down");
			}
			return;
		}

		// Regular characters (including pasted strings).
		if (input.length > 0 && !key.ctrl && !key.meta) {
			insertText(input);
		}
	});

	return (
		<Box flexDirection="column">
			{autocompleteMatches.length > 0 ? <AutocompletePopup matches={autocompleteMatches} /> : null}
			<Box flexDirection="row" gap={1}>
				<Text color={theme.user.label}>{USER_PROMPT}</Text>
				<Box flexDirection="column">
					{lines.map((line, i) => (
						<Text key={`L${String(i)}:${line}`}>{renderLine(line, i === row ? col : -1)}</Text>
					))}
					{lastLine.length === 0 && lines.length === 1 ? (
						<Text dimColor>{describeHint(state)}</Text>
					) : null}
				</Box>
			</Box>
		</Box>
	);
}

interface AutocompletePopupProps {
	readonly matches: readonly SlashCommand[];
}

function AutocompletePopup({ matches }: AutocompletePopupProps): React.JSX.Element {
	return (
		<Box
			flexDirection="column"
			borderStyle="single"
			borderColor={theme.autocomplete.border}
			paddingX={1}
		>
			{matches.map((cmd) => (
				<Box key={cmd.name} gap={2}>
					<Text color={theme.autocomplete.match}>{cmd.name}</Text>
					<Text color={theme.autocomplete.description} dimColor>
						{cmd.description}
					</Text>
				</Box>
			))}
		</Box>
	);
}

/** Render one line with a cursor marker. The marker is a visible caret char. */
function renderLine(line: string, cursorCol: number): string {
	if (cursorCol < 0) return line.length === 0 ? " " : line;
	const before = line.slice(0, cursorCol);
	const after = line.slice(cursorCol);
	// `\u2588` (FULL BLOCK) renders as a solid caret in most terminals.
	return `${before}\u2588${after}`;
}

function describeHint(state: TuiState): string {
	switch (state.phase) {
		case "idle":
			return "type a message - Enter sends, Shift+Enter for newline, / for commands";
		case "error":
			return `error: ${state.message} - type to retry`;
		case "processing":
		case "streaming":
			return "working...";
	}
}
