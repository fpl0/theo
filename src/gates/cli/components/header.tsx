/**
 * Startup banner for the CLI gate.
 *
 * One line at the top of the TUI: brand mark, short tagline, and a muted
 * caption with the current workspace + environment. Rendered once at
 * mount — cheap enough that we do not bother memoizing.
 */

import { Box, Text } from "ink";
import type React from "react";
import { theme } from "../theme.ts";

export interface HeaderProps {
	readonly workspace?: string;
	readonly environment?: string;
}

export function Header({ workspace, environment }: HeaderProps): React.JSX.Element {
	const captionBits: string[] = [];
	if (environment !== undefined && environment.length > 0) captionBits.push(environment);
	if (workspace !== undefined && workspace.length > 0) captionBits.push(workspace);
	const caption = captionBits.join(" · ");
	return (
		<Box
			flexDirection="row"
			paddingX={1}
			paddingY={0}
			borderStyle="single"
			borderColor={theme.header.border}
			borderLeft={false}
			borderRight={false}
			borderTop={false}
		>
			<Box flexGrow={1}>
				<Text color={theme.header.brand} bold>
					Theo
				</Text>
				<Text color={theme.header.tagline}>{"  personal AI, persistent memory"}</Text>
			</Box>
			{caption.length > 0 ? <Text color={theme.header.tagline}>{caption}</Text> : null}
		</Box>
	);
}
