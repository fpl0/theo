# Phase 11: CLI Gate — Full TUI

## Motivation

The CLI gate is Theo's primary development and testing interface. It proves the entire stack works
end-to-end: type a message, see Theo think, watch it invoke tools, get a streamed response — all in
the terminal.

This phase goes beyond a basic readline loop. It builds a real TUI with multiline editing,
slash-command autocomplete, scrollable conversation history, interrupt-and-redirect, and live tool
output. The result should feel like a native terminal chat client, not a debug REPL.

## Depends on

- **Phase 10** — Chat engine (the gate delegates to the engine)

## Stack Addition

| Concern | Choice | Why |
| ---------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| TUI framework | `ink` (React for terminals) | Flexbox layout, declarative rendering, streaming-friendly. Battle-tested at scale — Claude Code itself runs on an Ink fork. Works with Bun 1.2.0+. |
| Ink UI primitives | `@inkjs/ui` | Spinner, Select for structured UI elements |

Ink pulls React as a peer dependency. This is the only new runtime dependency for this phase. No
blessed, no terminal-kit, no raw ANSI escape code management.

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/gates/types.ts` | `Gate` interface, shared types for all gates |
| `src/gates/cli/app.tsx` | Root Ink `<App>` component — layout, state machine |
| `src/gates/cli/components/message-list.tsx` | Scrollable conversation history renderer |
| `src/gates/cli/components/input-area.tsx` | Multiline editor with slash-command autocomplete |
| `src/gates/cli/components/tool-output.tsx` | Live tool call status display |
| `src/gates/cli/components/status-bar.tsx` | Session info, engine state, memory stats |
| `src/gates/cli/gate.ts` | `CliGate` class — implements `Gate`, mounts Ink app |
| `src/gates/cli/hooks.ts` | Custom Ink hooks: `useEngine`, `useStream`, `useHistory` |
| `src/gates/cli/theme.ts` | Color palette, text styles, layout constants |
| `tests/gates/cli.test.ts` | Unit tests for gate logic and components |

This phase creates the `CliGate` class but does not modify `src/index.ts`. The entry point is wired
in Phase 14 (Engine lifecycle).

## Design Decisions

### Gate Interface

```typescript
interface Gate {
  readonly name: string;
  start(): Promise<void>;
  stop(): Promise<void>;
}
```

All gates implement this interface. The engine receives a `gate: string` identifier and processes
uniformly. The TUI is entirely a presentation concern — the engine has no knowledge of Ink, React,
or terminal rendering.

### Application State Machine

The TUI operates as a state machine with four states:

```text
idle → processing → streaming → idle
                 ↘ error → idle
```

```typescript
type TuiState =
  | { readonly phase: "idle" }
  | { readonly phase: "processing"; readonly startedAt: number }
  | { readonly phase: "streaming"; readonly chunks: number }
  | { readonly phase: "error"; readonly message: string };
```

State transitions drive the entire UI: what the input area shows, whether the spinner is visible,
whether Ctrl+C aborts or quits.

### Layout

The terminal is divided into three vertical zones:

```text
┌─────────────────────────────────────┐
│  Message List (scrollable)          │  ← flexGrow: 1
│  - user messages (right-aligned)    │
│  - theo responses (left-aligned)    │
│  - tool calls (collapsible panels)  │
│                                     │
├─────────────────────────────────────┤
│  Status Bar                         │  ← 1 row
│  session: abc | tools: 3 | 2.1s    │
├─────────────────────────────────────┤
│  Input Area                         │  ← 3+ rows
│  you> multiline text here           │
│       with cursor support           │
└─────────────────────────────────────┘
```

Implemented with Ink's flexbox:

```tsx
function App({ engine, bus }: AppProps) {
  const { state, messages, send, abort, reset } = useEngine(engine, bus);

  return (
    <Box flexDirection="column" height="100%">
      <Box flexGrow={1} flexDirection="column" overflow="hidden">
        <MessageList messages={messages} />
      </Box>
      <StatusBar state={state} />
      <InputArea
        state={state}
        onSubmit={send}
        onAbort={abort}
        onReset={reset}
      />
    </Box>
  );
}
```

### Multiline Input Editor

The `<InputArea>` component handles multiline text editing. It is a custom component built on Ink's
`useInput` hook — `@inkjs/ui`'s TextInput is single-line only.

**Key bindings:**

| Key | Action |
| ----- | -------- |
| `Enter` | Submit message (when input is non-empty) |
| `Shift+Enter` or `Alt+Enter` | Insert newline |
| `Up` / `Down` | Move cursor between lines (or cycle history if on first/last line) |
| `Ctrl+C` | Abort current turn (if processing) or exit (if idle) |
| `Tab` | Accept autocomplete suggestion |
| `Esc` | Dismiss autocomplete popup, or clear input |

**Input history:** The last 100 inputs are stored in memory (not persisted). `Up` on the first line
of an empty input cycles through history. This mirrors standard shell behavior.

```typescript
function useInputHistory(maxSize = 100) {
  const [history, setHistory] = useState<string[]>([]);
  const [cursor, setCursor] = useState(-1);

  const push = useCallback((text: string) => {
    setHistory(prev => [text, ...prev.slice(0, maxSize - 1)]);
    setCursor(-1);
  }, [maxSize]);

  const navigate = useCallback((direction: "up" | "down") => {
    setCursor(prev => {
      if (direction === "up") return Math.min(prev + 1, history.length - 1);
      if (direction === "down") return Math.max(prev - 1, -1);
      return prev;
    });
  }, [history.length]);

  const current = cursor >= 0 ? history[cursor] : undefined;

  return { push, navigate, current };
}
```

### Slash-Command Autocomplete

When the input starts with `/`, a popup appears above the input area showing matching commands. The
popup updates as the user types and disappears when the prefix no longer matches.

```typescript
const SLASH_COMMANDS: readonly SlashCommand[] = [
  { name: "/quit",   description: "Exit Theo",            aliases: ["/exit"] },
  { name: "/reset",  description: "Clear session",        aliases: [] },
  { name: "/status", description: "Show engine state",    aliases: [] },
  { name: "/memory", description: "Show memory stats",    aliases: [] },
  { name: "/clear",  description: "Clear screen",         aliases: [] },
  { name: "/help",   description: "Show available commands", aliases: ["/?"] },
] as const;

interface SlashCommand {
  readonly name: string;
  readonly description: string;
  readonly aliases: readonly string[];
}
```

The autocomplete component:

```tsx
function AutocompletePopup({ prefix }: { prefix: string }) {
  const matches = SLASH_COMMANDS.filter(
    cmd => cmd.name.startsWith(prefix) || cmd.aliases.some(a => a.startsWith(prefix))
  );

  if (matches.length === 0) return null;

  return (
    <Box flexDirection="column" borderStyle="single" borderColor="gray" paddingX={1}>
      {matches.map(cmd => (
        <Box key={cmd.name} gap={2}>
          <Text color="cyan">{cmd.name}</Text>
          <Text dimColor>{cmd.description}</Text>
        </Box>
      ))}
    </Box>
  );
}
```

`Tab` accepts the top match and fills it into the input. `Esc` or typing a non-`/` character
dismisses the popup.

### Streaming Architecture

The streaming pipeline is unchanged from the engine design (Phase 10). The TUI subscribes to
ephemeral events and renders them incrementally.

**Producer (engine, Phase 10):**

1. Engine calls SDK `query()` with `includePartialMessages: true`
2. As SDK yields text deltas, engine emits `stream.chunk` ephemeral events on the bus
3. When the turn completes, engine emits `stream.done`

**Consumer (TUI, this phase):**

```typescript
function useStream(bus: EventBus) {
  const [chunks, setChunks] = useState<string[]>([]);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const offChunk = bus.onEphemeral("stream.chunk", (event) => {
      setChunks(prev => [...prev, event.data.text]);
    });
    const offDone = bus.onEphemeral("stream.done", () => {
      setDone(true);
    });
    return () => { offChunk(); offDone(); };
  }, [bus]);

  const text = chunks.join("");
  const reset = useCallback(() => { setChunks([]); setDone(false); }, []);

  return { text, done, reset };
}
```

**Fallback:** If no `stream.chunk` events arrive before the turn completes, the message list renders
the complete response from `TurnResult.value.text`.

### Streaming Tool Output

When the engine invokes MCP tools during a turn, the bus emits ephemeral events for tool lifecycle:

```typescript
type EphemeralEvent =
  | { readonly type: "stream.chunk";
      readonly data: {
        readonly text: string;
        readonly sessionId: string;
      };
    }
  | { readonly type: "stream.done";
      readonly data: { readonly sessionId: string };
    }
  | { readonly type: "tool.start";
      readonly data: {
        readonly name: string;
        readonly input: string;
        readonly callId: string;
      };
    }
  | { readonly type: "tool.done";
      readonly data: {
        readonly callId: string;
        readonly durationMs: number;
      };
    };
```

> **Note:** `tool.start` and `tool.done` ephemeral events must be emitted by the engine (Phase 10).
If Phase 10 does not include these, they should be added as a prerequisite delta to this phase.

The `<ToolOutput>` component renders these as collapsible panels inline with the conversation:

```tsx
function ToolOutput({ calls }: { calls: ToolCall[] }) {
  return (
    <Box flexDirection="column" marginLeft={2}>
      {calls.map(call => (
        <Box key={call.callId} gap={1}>
          {call.done
            ? <Text color="green">✓</Text>
            : <Spinner type="dots" />}
          <Text dimColor>{call.name}</Text>
          {call.done && <Text dimColor>({call.durationMs}ms)</Text>}
        </Box>
      ))}
    </Box>
  );
}
```

During streaming, active tool calls show a spinner. Completed calls show a checkmark with duration.
This gives visibility into what the agent is doing without flooding the screen.

### Conversation History (Message List)

The `<MessageList>` component renders the full conversation with visual distinction between
participants:

```typescript
interface DisplayMessage {
  readonly role: "user" | "assistant";
  readonly text: string;
  readonly timestamp: Date;
  readonly toolCalls?: ToolCall[];
  readonly streaming?: boolean; // true while chunks are arriving
}
```

**Rendering rules:**

- User messages: prefixed with `you>`, styled with a distinct color
- Assistant messages: prefixed with `theo>`, rendered as-is (markdown rendered to ANSI if feasible
  in later iteration)
- Tool calls: nested under the assistant message that triggered them
- Streaming messages: the current partial text, updated in place as chunks arrive
- The list auto-scrolls to the bottom on new content

The message list is the primary scrollable region. When the output exceeds the terminal height,
earlier messages scroll up. The user can scroll back with `Shift+Up` / `Shift+Down` or `PageUp` /
`PageDown` while idle.

### Interrupt and Redirect

Ctrl+C during an active turn:

1. Calls `engine.abortCurrentTurn()` (SDK abort mechanism)
2. Marks the current streaming message as interrupted: `"[interrupted]"` appended
3. Transitions state to `idle`
4. Focus returns to the input area immediately
5. The user can type a new message without waiting for cleanup

This is "interrupt-and-redirect" — not just cancellation. The user presses Ctrl+C and immediately
starts typing the next thought. The aborted turn's partial output remains visible in the
conversation history, clearly marked as interrupted.

```typescript
const abort = useCallback(() => {
  engine.abortCurrentTurn();
  setMessages(prev => {
    const last = prev[prev.length - 1];
    if (last?.streaming) {
      return [
        ...prev.slice(0, -1),
        { ...last, text: last.text + "\n[interrupted]", streaming: false },
      ];
    }
    return prev;
  });
  setState({ phase: "idle" });
}, [engine]);
```

Ctrl+C at idle exits gracefully (triggers `stop()`).

### CliGate Class

The `CliGate` class is the bridge between the `Gate` interface and the Ink application:

```typescript
class CliGate implements Gate {
  readonly name = "cli";
  private inkInstance: Instance | null = null;

  constructor(
    private readonly engine: ChatEngine,
    private readonly bus: EventBus,
  ) {}

  async start(): Promise<void> {
    const { render } = await import("ink");
    this.inkInstance = render(
      <App engine={this.engine} bus={this.bus} />
    );

    // Wait for the Ink app to unmount (user quit or external stop)
    await this.inkInstance.waitUntilExit();
  }

  async stop(): Promise<void> {
    this.inkInstance?.unmount();
    // Do NOT call process.exit — Phase 14 handles shutdown
  }
}
```

`start()` blocks until the TUI exits. `stop()` unmounts Ink, which restores the terminal to its
original state (Ink handles alternate screen buffer cleanup).

### Special Commands

| Command | Action |
| --------- | -------- |
| `/quit` or `/exit` | Graceful shutdown via `stop()` |
| `/reset` | Release current session, clear message list |
| `/status` | Toggle status bar detail (session ID, memory stats, engine state) |
| `/memory` | Show memory tier summary (core count, graph nodes, recent episodic) |
| `/clear` | Clear visible conversation history (does not affect engine state) |
| `/help` or `/?` | List all available commands |

Commands are handled inside the `<InputArea>` before reaching the engine. They never generate
events.

### Theme

A minimal, consistent color scheme:

```typescript
const theme = {
  user: { label: "cyan", text: "white" },
  assistant: { label: "magenta", text: "white" },
  tool: { label: "yellow", spinner: "yellow", done: "green" },
  error: { label: "red", text: "red" },
  status: { bg: "gray", text: "white" },
  autocomplete: { border: "gray", match: "cyan", description: "gray" },
  interrupted: { text: "gray" },
} as const;
```

Colors are defined once and imported everywhere. No hardcoded color strings in components.

## Definition of Done

- [ ] `ink` and `@inkjs/ui` added to dependencies, verified working with Bun
- [ ] `Gate` interface defined in `src/gates/types.ts`
- [ ] `CliGate` class implements `Gate`, mounts and unmounts Ink app
- [ ] Layout renders three zones: message list, status bar, input area
- [ ] Multiline input editor supports Enter to submit, Shift/Alt+Enter for newlines
- [ ] Input history navigable with Up/Down arrows (last 100 inputs)
- [ ] Slash-command autocomplete popup appears when input starts with `/`
- [ ] Tab accepts top autocomplete match
- [ ] All slash commands (`/quit`, `/exit`, `/reset`, `/status`, `/memory`, `/clear`, `/help`) work
- [ ] Streaming responses render incrementally via `stream.chunk` ephemeral events
- [ ] Non-streaming fallback prints complete `TurnResult.value.text` after the turn
- [ ] Tool calls display with spinner (active) and checkmark (done) via `tool.start`/`tool.done`
  events
- [ ] Conversation history scrolls, auto-follows new content
- [ ] Manual scroll with Shift+Up/Down or PageUp/PageDown while idle
- [ ] Ctrl+C during processing aborts the turn, marks output as interrupted, returns to input
- [ ] Ctrl+C at idle exits gracefully
- [ ] Errors display inline in the conversation (not crash)
- [ ] `stop()` unmounts Ink without calling `process.exit()`
- [ ] `just check` passes

## Test Cases

### `tests/gates/cli.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Gate interface | `CliGate` instance | Implements `Gate` with `name="cli"` |
| Message forwarding | Simulate submit from input area | `engine.handleMessage()` called with correct body and `gate="cli"` |
| Empty input | Submit with empty text | No engine call, input stays focused |
| Quit command | `/quit` submitted | `stop()` called, Ink unmounts |
| Reset command | `/reset` submitted | Session released, message list cleared |
| Clear command | `/clear` submitted | Message list cleared, session intact |
| Slash autocomplete | Type `/re` | Popup shows `/reset` as match |
| Tab completion | Type `/re` then Tab | Input fills to `/reset` |
| Error handling | Engine returns `Result.err` | Error message displayed inline, no crash |
| Input history | Submit two messages, press Up twice | Second message, then first message restored |
| Streaming display | Emit `stream.chunk` events | Text appears incrementally in message list |
| Streaming fallback | No `stream.chunk`, turn completes | Complete response rendered from result |
| Tool output | Emit `tool.start` then `tool.done` | Spinner shown, then checkmark with duration |
| Interrupt | Ctrl+C during processing | Turn aborted, `[interrupted]` shown, state returns to idle |
| State machine | Submit message | State transitions: idle → processing → streaming → idle |

**Note on Ink testing:** Ink provides `ink-testing-library` for rendering components in tests
without a real terminal. Use `render(<App />)` from the testing library, then inspect `lastFrame()`
for output assertions.

### Manual integration test

1. `just up` — start PostgreSQL
2. `just migrate` — apply migrations
3. Start Theo via the dev command
4. Type "Hello, I'm testing Theo" — response streams token by token
5. Type "What did I just say?" — Theo recalls via memory
6. Type a multiline message (Shift+Enter for newlines) — sent as one message
7. Type `/re` — autocomplete popup shows `/reset`
8. Press Tab — input fills to `/reset`, press Enter — session clears
9. Press Up arrow — previous input restored from history
10. Start a long response, press Ctrl+C — response marked `[interrupted]`, input ready
11. Immediately type a follow-up — interrupt-and-redirect works
12. `/quit` — clean exit, terminal restored

## Prerequisite Delta

Phase 10 (Chat Engine) must emit `tool.start` and `tool.done` ephemeral events on the bus when the
SDK invokes MCP tools. If Phase 10 does not include these, add them before implementing tool output
display in this phase.

The ephemeral event union in Phase 2 should be extended:

```typescript
type EphemeralEvent =
  | { readonly type: "stream.chunk";
      readonly data: {
        readonly text: string;
        readonly sessionId: string;
      };
    }
  | { readonly type: "stream.done";
      readonly data: { readonly sessionId: string };
    }
  | { readonly type: "tool.start";
      readonly data: {
        readonly name: string;
        readonly input: string;
        readonly callId: string;
      };
    }
  | { readonly type: "tool.done";
      readonly data: {
        readonly callId: string;
        readonly durationMs: number;
      };
    };
```

## Risks

### Medium risk: Ink + Bun compatibility edge cases

Ink works with Bun 1.2.0+ for basic rendering. Edge cases around raw stdin mode, signal handling,
and alternate screen buffer may surface. Mitigation: test early with `bun --bun` flag, file issues
upstream if needed. Claude Code's Ink fork exists as a reference for workarounds.

### Low risk: Multiline input complexity

Building a multiline editor on `useInput` is non-trivial but bounded. The scope is intentionally
limited: no syntax highlighting, no word wrap at arbitrary widths, no mouse selection. Just cursor
movement, line insertion, and basic editing. This is a textarea, not an IDE.

### Low risk: Terminal compatibility

Different terminal emulators handle ANSI sequences slightly differently. Ink abstracts most of this.
The main concern is key detection for Shift+Enter and Alt+Enter — some terminals don't distinguish
these from plain Enter. Fallback: use Ctrl+J for newline insertion if modifier detection fails.
