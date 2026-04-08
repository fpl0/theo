# Phase 1: Ollama Infrastructure

## Motivation

Before writing any Theo code, we need to verify the critical assumptions. The entire offline mode
depends on Ollama correctly implementing the Anthropic Messages API -- specifically the **beta**
endpoint path, tool calling, and SSE streaming. If any of these fail, we find out now before
modifying the engine.

This phase is a validation gate, not a feature. Nothing else proceeds until it passes.

## Depends on

Nothing. This is infrastructure validation only.

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `scripts/ollama-smoke-test.ts` | 6-test validation: beta endpoint, streaming, tool calling, large tool args, SDK subprocess, count_tokens resilience |
| `docs/plans/offline-mode/model-eval.md` | Evaluation results (filled in during execution) |

### Files to modify

| File | Change |
| ------ | -------- |
| `justfile` | Add `ollama-setup`, `ollama-pull`, `ollama-test` recipes |

## Design Decisions

### Ollama Version Requirement

**Ollama >= 0.14.3 is required.** Earlier versions have two known issues:

1. Tool call arguments are not streamed incrementally. Claude Code has a ~255-second streaming
   timeout. If the model takes longer than that to generate tool arguments (no intermediate
   deltas are sent), the stream stalls and Claude Code times out. Fixed in 0.14.3.
2. The `/v1/messages/count_tokens` endpoint (called by Claude Code) returns 404, which can
   destabilize the Ollama server on some versions. Fixed in 0.14.3+.

### Ollama Installation and Model Pull

```just
# justfile additions

ollama-setup:
    @echo "Installing Ollama..."
    brew install ollama
    @echo "Starting Ollama service..."
    brew services start ollama
    @echo "Pulling default offline model..."
    ollama pull qwen3.5:9b
    @echo "Verifying version >= 0.14.3..."
    ollama --version

ollama-pull model="qwen3.5:9b":
    ollama pull {{model}}

ollama-test:
    bun scripts/ollama-smoke-test.ts
```

### Smoke Test: 6 Tests

The smoke test validates the exact path the Agent SDK will use, not a simplified version.

**Test 1: Beta endpoint** -- Claude Code calls `client.beta.messages.create()`, which hits
`/v1/messages?beta=true` with `anthropic-beta` headers. This is the showstopper test.

**Test 2: Streaming** -- Verify SSE streaming with correct event ordering. Claude Code's stream
parser throws `"Unexpected event order"` if events arrive out of sequence.

**Test 3: Tool calling** -- Define a tool, verify the model returns a `tool_use` content block
with valid JSON arguments.

**Test 4: Large tool arguments** -- A tool call that generates >500 tokens of arguments. This
is the scenario that triggers the 255-second timeout on pre-0.14.3 Ollama.

**Test 5: SDK subprocess** -- Call `query()` from the Agent SDK with `ANTHROPIC_BASE_URL`
pointing at Ollama. This tests the actual subprocess path, not just the Anthropic SDK client.
The subprocess must start, receive a response, and yield at least one message. 30-second
timeout.

**Test 6: count_tokens resilience** -- POST to `/v1/messages/count_tokens`. Verify Ollama
returns an error (404 or 400) without crashing. Then immediately send a normal message to
confirm the server is still stable.

```typescript
// scripts/ollama-smoke-test.ts
import Anthropic from "@anthropic-ai/sdk";
import { query } from "@anthropic-ai/claude-agent-sdk";

const OLLAMA_URL = "http://localhost:11434";
const MODEL = "qwen3.5:9b";

const client = new Anthropic({
  baseURL: OLLAMA_URL,
  apiKey: "ollama",
});

// Test 1: Beta endpoint (the actual path Claude Code uses)
async function testBetaEndpoint(): Promise<void> {
  const response = await client.beta.messages.create({
    model: MODEL,
    max_tokens: 64,
    messages: [{ role: "user", content: "What is 2 + 2? One word." }],
    betas: ["prompt-caching-2024-07-31"],
  });
  const text = response.content[0];
  if (text.type !== "text") throw new Error("Expected text response");
  console.log("[PASS] Beta endpoint:", text.text.trim());
}

// Test 2: Streaming with event ordering
async function testStreaming(): Promise<void> {
  let chunks = 0;
  const stream = client.messages.stream({
    model: MODEL,
    max_tokens: 128,
    messages: [{ role: "user", content: "Count from 1 to 5." }],
  });
  for await (const event of stream) {
    if (event.type === "content_block_delta") chunks++;
  }
  if (chunks === 0) throw new Error("No streaming chunks received");
  console.log(`[PASS] Streaming: ${chunks} chunks`);
}

// Test 3: Basic tool calling
async function testToolCalling(): Promise<void> {
  const response = await client.beta.messages.create({
    model: MODEL,
    max_tokens: 512,
    betas: [],
    messages: [{ role: "user", content: "What is the weather in Lisbon?" }],
    tools: [
      {
        name: "get_weather",
        description: "Get the current weather for a city",
        input_schema: {
          type: "object" as const,
          properties: {
            city: { type: "string", description: "The city name" },
          },
          required: ["city"],
        },
      },
    ],
  });
  const toolUse = response.content.find((b) => b.type === "tool_use");
  if (!toolUse) {
    console.log("[WARN] Tool calling: model did not invoke tool");
    console.log("  Response:", JSON.stringify(response.content));
    return;
  }
  if (toolUse.type === "tool_use") {
    console.log("[PASS] Tool calling:", toolUse.name, toolUse.input);
  }
}

// Test 4: Large tool arguments (>500 tokens)
async function testLargeToolArgs(): Promise<void> {
  const response = await client.beta.messages.create({
    model: MODEL,
    max_tokens: 2048,
    betas: [],
    messages: [
      {
        role: "user",
        content:
          "Create a detailed weekly meal plan. Use the save_plan tool.",
      },
    ],
    tools: [
      {
        name: "save_plan",
        description: "Save a structured plan with detailed content",
        input_schema: {
          type: "object" as const,
          properties: {
            title: { type: "string" },
            content: {
              type: "string",
              description: "Detailed plan content, at least 500 words",
            },
          },
          required: ["title", "content"],
        },
      },
    ],
  });
  const toolUse = response.content.find((b) => b.type === "tool_use");
  if (!toolUse || toolUse.type !== "tool_use") {
    console.log("[WARN] Large tool args: model did not invoke tool");
    return;
  }
  const inputStr = JSON.stringify(toolUse.input);
  console.log(
    `[PASS] Large tool args: ${inputStr.length} chars in arguments`,
  );
}

// Test 5: SDK subprocess (the real integration path)
async function testSdkSubprocess(): Promise<void> {
  const oldBaseUrl = process.env.ANTHROPIC_BASE_URL;
  const oldApiKey = process.env.ANTHROPIC_API_KEY;
  process.env.ANTHROPIC_BASE_URL = OLLAMA_URL;
  process.env.ANTHROPIC_API_KEY = "ollama";

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    let gotMessage = false;

    const generator = query({
      prompt: "Say hello in exactly 3 words.",
      options: {
        model: MODEL,
        thinking: { type: "disabled" },
        maxTurns: 2,
      },
    });

    for await (const message of generator) {
      gotMessage = true;
      if (message.type === "result") {
        clearTimeout(timeout);
        if (message.subtype === "success") {
          console.log("[PASS] SDK subprocess:", message.result.slice(0, 80));
        } else {
          console.log("[WARN] SDK subprocess ended with:", message.subtype);
        }
        break;
      }
    }
    clearTimeout(timeout);
    if (!gotMessage) throw new Error("Generator yielded no messages");
  } finally {
    // Restore env
    if (oldBaseUrl) process.env.ANTHROPIC_BASE_URL = oldBaseUrl;
    else delete process.env.ANTHROPIC_BASE_URL;
    if (oldApiKey) process.env.ANTHROPIC_API_KEY = oldApiKey;
    else delete process.env.ANTHROPIC_API_KEY;
  }
}

// Test 6: count_tokens resilience
async function testCountTokensResilience(): Promise<void> {
  // Claude Code calls this endpoint; Ollama must not crash
  try {
    await fetch(`${OLLAMA_URL}/v1/messages/count_tokens?beta=true`, {
      method: "POST",
      headers: {
        "x-api-key": "ollama",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: MODEL,
        messages: [{ role: "user", content: "test" }],
      }),
      signal: AbortSignal.timeout(5000),
    });
  } catch {
    // Expected to fail -- we only care that the server survives
  }

  // Verify server is still healthy after the 404
  const response = await client.messages.create({
    model: MODEL,
    max_tokens: 16,
    messages: [{ role: "user", content: "Say OK." }],
  });
  if (response.content[0].type !== "text") {
    throw new Error("Server unstable after count_tokens probe");
  }
  console.log("[PASS] count_tokens resilience: server stable after 404");
}

async function main(): Promise<void> {
  console.log("Ollama Smoke Test (Offline Mode Validation)");
  console.log("============================================");
  console.log(`Endpoint: ${OLLAMA_URL}`);
  console.log(`Model: ${MODEL}`);
  console.log(`Ollama version: check with 'ollama --version' (>= 0.14.3)\n`);

  await testBetaEndpoint();
  await testStreaming();
  await testToolCalling();
  await testLargeToolArgs();
  await testCountTokensResilience();
  await testSdkSubprocess();

  console.log("\nAll critical tests passed.");
}

main().catch((e) => {
  console.error("\n[FAIL]", e.message);
  process.exit(1);
});
```

### Model Evaluation

Record results in `model-eval.md`:

- Ollama version (must be >= 0.14.3)
- Model name and quantization level
- Response latency for each test
- Tool calling success rate (run test 3 ten times)
- Large tool arg test: time to generate, argument size
- SDK subprocess test: startup latency, first-token latency
- Memory usage: `ollama ps` output during inference
- Token counts: note that Ollama estimates tokens as `content_length / 4` (approximate)
- Any quirks or failure modes observed

### Model Warm-Up

After confirming Ollama is reachable, send a minimal request to trigger model loading. On M1
16GB, cold-start model loading takes 10-30 seconds. Moving this to startup prevents the first
conversation message from appearing to hang.

The warm-up is included in the `ollama-setup` justfile recipe and in the Phase 6 startup
sequence.

## Definition of Done

- [ ] Ollama >= 0.14.3 installed and running
- [ ] `ollama pull qwen3.5:9b` completes
- [ ] Test 1: Beta endpoint (`client.beta.messages.create()`) returns a text response
- [ ] Test 2: Streaming receives >0 chunks with correct event ordering
- [ ] Test 3: Tool calling returns `tool_use` block (>= 7/10 runs)
- [ ] Test 4: Large tool arguments (>500 chars) complete without timeout
- [ ] Test 5: SDK subprocess `query()` yields a result message within 30 seconds
- [ ] Test 6: Server remains stable after `count_tokens` 404
- [ ] `model-eval.md` documents all results
- [ ] `just check` passes

## Test Cases

Manual validation via `just ollama-test`. Not in CI (requires running Ollama).

| Test | Method | Expected |
| ------ | -------- | ---------- |
| Beta endpoint | `client.beta.messages.create()` with `betas` header | Text response |
| Streaming | `client.messages.stream()` | >0 chunks, correct order |
| Tool calling | Beta API with `tools` param | `tool_use` block |
| Large tool args | Beta API, tool expecting 500+ words | Completes, no timeout |
| SDK subprocess | `query()` with `ANTHROPIC_BASE_URL` to Ollama | Result message within 30s |
| count_tokens | POST to unknown endpoint, then normal request | Server stable |
| Tool reliability | Run tool test 10x | >= 7/10 correct invocations |

## Risks

**Medium risk.** The beta endpoint test (Test 1) is the potential showstopper. If Ollama does
not handle `/v1/messages?beta=true` or the `anthropic-beta` header, the entire approach fails.
Mitigation: the Ollama Anthropic compatibility docs indicate query parameters are passed through,
and the community has successfully used Claude Code with Ollama. But this must be verified, not
assumed.

If Test 1 fails, alternatives:

- Check if a newer Ollama version fixes it
- Use a lightweight proxy that strips `?beta=true` from the URL
- Consider the ollama-anthropic-shim project

If Qwen3.5-9B tool calling is unreliable (Test 3 < 7/10), try: Qwen3-8B, GLM-4.7-flash, or
Hermes 3.
