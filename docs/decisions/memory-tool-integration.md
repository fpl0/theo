# Memory tool integration

**Date:** 2026-03-26
**Ticket:** FPL-14

## Context

The MemGPT/AgeMem pattern gives Claude autonomous control over Theo's memory during conversations. Claude can decide what to remember, search its knowledge, and update its self-model. This requires tool definitions exposed via the Anthropic tool-use API and a tool execution loop in the conversation engine.

## Decisions

### Tool definitions live in `memory/tools.py`

The four memory tools (store_memory, search_memory, read_core_memory, update_core_memory) are defined as Anthropic-compatible tool schemas in a dedicated module within the memory package. This keeps tool definitions close to the operations they wrap and avoids cluttering the conversation engine.

The `execute_tool` dispatcher maps tool names to memory operations and catches all exceptions, returning error strings to Claude rather than raising. This lets Claude adapt to failures gracefully (e.g. retry with different parameters or inform the user).

### Tool loop integrated directly into `_execute_turn`

Rather than creating a separate abstraction for the tool loop, it's embedded in the conversation engine's `_execute_turn` method. The loop:

1. Streams a response from Claude (passing tool definitions)
2. If the response contains `ToolUseRequest` events, executes each tool
3. Appends the assistant message (with tool_use blocks) and tool results to the message list
4. Re-calls `stream_response` with the updated messages
5. Repeats until Claude produces a final text response or hits the 10-iteration cap

This is simpler than a separate tool executor class and keeps all turn logic in one place. The max iteration cap (10) prevents infinite loops if Claude keeps requesting tools.

### Tools always passed to stream_response

Every LLM call includes the tool definitions, even for simple messages. Claude decides whether to use tools based on the conversation context. This avoids complexity of trying to predict when tools are needed.

### OTEL instrumentation

- **Span attribute** `turn.tool_calls`: count of tool calls per turn, on the `conversation.turn` span
- **Counter** `theo.conversation.tool_calls`: total tool calls, attributed by `tool.name`
- **Span** `execute_tool`: wraps each individual tool execution with `tool.name` attribute

## Alternatives considered

### Separate ToolExecutor class

Could have created a `ToolExecutor` that the conversation engine delegates to. Rejected as premature abstraction — the tool loop is ~30 lines and tightly coupled to the conversation flow. If more tool sources emerge beyond memory, this can be extracted later.

### Conditional tool passing

Could skip passing tools for "reactive" speed messages. Rejected because it adds prediction complexity and Claude handles this naturally by not calling tools for simple greetings.
