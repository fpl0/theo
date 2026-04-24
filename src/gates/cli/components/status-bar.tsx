/**
 * One-row status strip below the message list.
 *
 * Layout:
 *
 *   [ PHASE ]  session · abc123    ·    $0.024 · 1.2k tok    ·    working…
 *
 * The phase pill colors itself by state (muted when idle, amber while
 * thinking, violet while streaming, red on error). Subsequent chips are
 * dim labels with prominent values, separated by a dot glyph so the eye
 * can scan left-to-right without overrun on narrow terminals.
 */

import { Box, Text } from "ink";
import type React from "react";
import type { TuiState } from "../state.ts";
import { symbols, theme } from "../theme.ts";

export interface TurnStats {
	readonly costUsd: number;
	readonly inputTokens: number;
	readonly outputTokens: number;
}

export interface StatusBarProps {
	readonly state: TuiState;
	readonly sessionId: string | null;
	readonly stats: TurnStats;
}

export function StatusBar({ state, sessionId, stats }: StatusBarProps): React.JSX.Element {
	return (
		<Box flexDirection="row" paddingX={1} gap={2}>
			<PhasePill state={state} />
			{sessionId !== null ? (
				<>
					<ChipSeparator />
					<Chip label="session" value={shortId(sessionId)} />
				</>
			) : null}
			{stats.costUsd > 0 ? (
				<>
					<ChipSeparator />
					<Chip label="cost" value={`$${stats.costUsd.toFixed(3)}`} />
				</>
			) : null}
			{stats.inputTokens + stats.outputTokens > 0 ? (
				<>
					<ChipSeparator />
					<Chip label="tok" value={formatTokens(stats.inputTokens + stats.outputTokens)} />
				</>
			) : null}
			{state.phase === "error" ? (
				<>
					<ChipSeparator />
					<Text color={theme.error}>{state.message}</Text>
				</>
			) : null}
		</Box>
	);
}

function PhasePill({ state }: { readonly state: TuiState }): React.JSX.Element {
	const { label, fg, bg } = phaseStyle(state);
	return (
		<Text color={fg} backgroundColor={bg}>
			{` ${label} `}
		</Text>
	);
}

interface PhaseStyle {
	readonly label: string;
	readonly fg: string;
	readonly bg: string;
}

function phaseStyle(state: TuiState): PhaseStyle {
	switch (state.phase) {
		case "idle":
			return { label: "idle", fg: theme.phase.idleFg, bg: theme.phase.idleBg };
		case "processing":
			return { label: "working", fg: theme.phase.workingFg, bg: theme.phase.workingBg };
		case "streaming":
			return { label: "streaming", fg: theme.phase.streamingFg, bg: theme.phase.streamingBg };
		case "error":
			return { label: "error", fg: theme.phase.errorFg, bg: theme.phase.errorBg };
	}
}

function Chip({ label, value }: { readonly label: string; readonly value: string }) {
	return (
		<Box>
			<Text color={theme.chip.label}>{label} </Text>
			<Text color={theme.chip.value}>{value}</Text>
		</Box>
	);
}

function ChipSeparator() {
	return <Text color={theme.chip.separator}>{symbols.chipSeparator}</Text>;
}

/** Show the last 6 chars of a session ID — enough to disambiguate. */
function shortId(id: string): string {
	return id.length <= 6 ? id : id.slice(-6);
}

/** Format a token total with a 1-decimal k/M suffix so the chip stays short. */
function formatTokens(n: number): string {
	if (n < 1_000) return String(n);
	if (n < 1_000_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
	return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}
