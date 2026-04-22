/**
 * Tiny inline-markdown renderer for assistant messages.
 *
 * Supports a deliberately small subset:
 *
 *   - `**bold**`    → bold text
 *   - `*italic*`    → italic text
 *   - `` `code` ``  → inline monospace with a muted color
 *
 * Block-level constructs (code fences, lists, headers) pass through as
 * plain text — the TUI is not a markdown viewer and trying to be one leads
 * to brittle, inconsistent output. What we *do* need is to stop spraying
 * literal asterisks and backticks at the user, which is the common case
 * the model reaches for inside a single sentence.
 *
 * The parser is a single-pass, left-to-right scan with a tiny state set;
 * it does not attempt to be a full markdown grammar. It is tested by
 * inspection against typical assistant outputs and is fast enough to run
 * per-render without memoization.
 */

import { Text } from "ink";
import type React from "react";
import { theme } from "../theme.ts";

type InlineSegment =
	| { readonly kind: "text"; readonly value: string }
	| { readonly kind: "bold"; readonly value: string }
	| { readonly kind: "italic"; readonly value: string }
	| { readonly kind: "code"; readonly value: string };

/** Render a plain string with minimal inline-markdown styling. */
export function RichText({
	text,
	color,
	dimColor,
}: {
	readonly text: string;
	readonly color?: string;
	readonly dimColor?: boolean;
}): React.JSX.Element {
	const segments = parseInline(text);
	const colorProp = color !== undefined ? { color } : {};
	const dimProp = dimColor === true ? { dimColor: true as const } : {};
	return (
		<Text>
			{segments.map((seg, i) => {
				const key = `${i}:${seg.kind}`;
				if (seg.kind === "text") {
					return (
						<Text key={key} {...colorProp} {...dimProp}>
							{seg.value}
						</Text>
					);
				}
				if (seg.kind === "bold") {
					return (
						<Text key={key} {...colorProp} bold>
							{seg.value}
						</Text>
					);
				}
				if (seg.kind === "italic") {
					return (
						<Text key={key} {...colorProp} italic>
							{seg.value}
						</Text>
					);
				}
				return (
					<Text key={key} color={theme.chip.accent}>
						{seg.value}
					</Text>
				);
			})}
		</Text>
	);
}

/**
 * Walk `input` once and split it into styled segments.
 *
 * The parser is permissive: unmatched markers fall back to literal text,
 * so the model emitting `a * b * c` without spaces does not silently
 * italicize everything. We require a non-whitespace char immediately after
 * `*` or `**` for it to count as an opener.
 */
export function parseInline(input: string): InlineSegment[] {
	const out: InlineSegment[] = [];
	let buf = "";
	const flushText = (): void => {
		if (buf.length === 0) return;
		out.push({ kind: "text", value: buf });
		buf = "";
	};
	let i = 0;
	while (i < input.length) {
		const ch = input[i];
		if (ch === "`") {
			const end = input.indexOf("`", i + 1);
			if (end > i) {
				flushText();
				out.push({ kind: "code", value: input.slice(i + 1, end) });
				i = end + 1;
				continue;
			}
		}
		if (ch === "*" && input[i + 1] === "*") {
			const end = input.indexOf("**", i + 2);
			if (end > i + 2 && !/\s/u.test(input[i + 2] ?? "")) {
				flushText();
				out.push({ kind: "bold", value: input.slice(i + 2, end) });
				i = end + 2;
				continue;
			}
		}
		if (ch === "*") {
			const end = input.indexOf("*", i + 1);
			if (end > i + 1 && !/\s/u.test(input[i + 1] ?? "") && input[end - 1] !== " ") {
				flushText();
				out.push({ kind: "italic", value: input.slice(i + 1, end) });
				i = end + 1;
				continue;
			}
		}
		buf += ch;
		i++;
	}
	flushText();
	return out;
}
