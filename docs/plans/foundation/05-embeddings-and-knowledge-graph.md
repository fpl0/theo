# Phase 5: Embeddings & Knowledge Graph

## Motivation

The knowledge graph is Theo's semantic memory — facts, preferences, observations, beliefs stored as
nodes connected by labeled edges. This phase makes the graph operational: nodes can be created with
embeddings, edges can link them with temporal versioning, and similar nodes can be found by vector
similarity.

Embeddings are the bridge between natural language and vector search. Every node and episode gets an
embedding vector that captures its semantic meaning, enabling "fuzzy" retrieval that goes beyond
keyword matching. Local embedding generation (no API calls) means privacy, speed, and zero marginal
cost.

## Depends on

- **Phase 3** — Event bus (all mutations emit events)
- **Phase 4** — Memory schema (tables must exist)

## Scope

### Files to create

| File | Purpose |
| ------ | --------- |
| `src/memory/embeddings.ts` | Embedding service: text -> `Float32Array` via HuggingFace Transformers ONNX |
| `src/memory/graph/types.ts` | `Node`, `Edge`, `NodeKind`, `NodeId`, `EdgeId`, `Sensitivity`, `TrustTier` types |
| `src/memory/graph/nodes.ts` | `NodeRepository` — create, getById, update, adjustConfidence, findSimilar |
| `src/memory/graph/edges.ts` | `EdgeRepository` — create, expire, update (expire+create), getForNode |
| `tests/memory/embeddings.test.ts` | Embedding dimension validation, lazy loading |
| `tests/memory/graph/nodes.test.ts` | Node CRUD, event emission, similarity search |
| `tests/memory/graph/edges.test.ts` | Edge creation, temporal versioning, active filtering |

## Design Decisions

### Embedding Model

`Xenova/all-mpnet-base-v2` — 768 dimensions. This is the only compatible model: the schema defines
`vector(768)` columns.

### Embedding Service

```typescript
interface EmbeddingService {
  embed(text: string): Promise<Float32Array>;
  embedBatch(texts: readonly string[]): Promise<readonly Float32Array[]>;
}
```

Uses `@huggingface/transformers` with ONNX runtime. The model is loaded lazily on first call (avoids
blocking startup).

The service is a class, not a singleton. Tests inject a mock that returns deterministic vectors.

```typescript
class HuggingFaceEmbeddingService implements EmbeddingService {
  private pipeline: Pipeline | null = null;

  async embed(text: string): Promise<Float32Array> {
    const pipe = await this.getPipeline();
    const output = await pipe(text, { pooling: "mean", normalize: true });
    const data = output.data;
    if (!(data instanceof Float32Array)) {
      throw new Error(`Expected Float32Array from pipeline, got ${typeof data}`);
    }
    return data;
  }

  private async getPipeline(): Promise<Pipeline> {
    if (!this.pipeline) {
      const { pipeline } = await import("@huggingface/transformers");
      this.pipeline = await pipeline("feature-extraction", "Xenova/all-mpnet-base-v2");
    }
    return this.pipeline;
  }
}
```

### pgvector + postgres.js Integration

postgres.js does not natively understand pgvector's `vector` type. Vectors must be serialized as
string literals in tagged templates. pgvector accepts the format `'[0.1,0.2,...]'` and returns it in
the same format.

```typescript
// Serialize Float32Array to pgvector string literal
function toVectorLiteral(v: Float32Array): string {
  return `[${Array.from(v).join(",")}]`;
}

// Parse pgvector string literal to Float32Array
function fromVectorLiteral(v: string): Float32Array {
  return new Float32Array(v.slice(1, -1).split(",").map(Number));
}
```

Usage in tagged template queries:

```typescript
// Insert — pass the vector as a string literal
const vectorStr = toVectorLiteral(embedding);
await sql`
  INSERT INTO node (kind, body, embedding, trust, confidence, importance, sensitivity)
  VALUES (${kind}, ${body}, ${vectorStr}::vector, ${trust}, ${confidence}, ${importance}, ${sensitivity})
  RETURNING *
`;

// Query — same pattern for the query vector
const queryStr = toVectorLiteral(queryEmbedding);
const rows = await sql`
  SELECT *, 1 - (embedding <=> ${queryStr}::vector) AS similarity
  FROM node
  WHERE embedding IS NOT NULL
  ORDER BY embedding <=> ${queryStr}::vector
  LIMIT ${limit}
`;
```

The `::vector` cast tells PostgreSQL to interpret the string as a pgvector value. The tagged
template parameterizes the string safely — no SQL injection risk.

When reading rows back, `embedding` comes back as a string. Use `fromVectorLiteral()` in
`rowToNode()` to convert it.

### Embedding Failure Handling

Node creation must never fail due to embedding failure. If `embed()` throws, the node is still
inserted with `embedding: null`. A bus handler on `memory.node.created` retries embedding
asynchronously. The `findSimilar` query already handles this via `WHERE embedding IS NOT NULL`.

```typescript
async create(data: CreateNodeInput): Promise<Node> {
  let vectorStr: string | null = null;
  try {
    const embedding = await this.embeddings.embed(data.body);
    vectorStr = toVectorLiteral(embedding);
  } catch {
    // Node will be created without embedding.
    // Bus handler on memory.node.created retries embedding async.
  }

  const [row] = await this.sql`
    INSERT INTO node (kind, body, embedding, trust, confidence, importance, sensitivity)
    VALUES (
      ${data.kind}, ${data.body},
      ${vectorStr === null ? null : sql`${vectorStr}::vector`},
      ${data.trust ?? "inferred"},
      ${data.confidence ?? 1.0},
      ${data.importance ?? 0.5},
      ${data.sensitivity ?? "normal"}
    )
    RETURNING *
  `;
  const node = rowToNode(row);

  await this.bus.emit({
    type: "memory.node.created",
    id: newEventId(),
    version: 1,
    actor: data.actor,
    data: {
      nodeId: node.id,
      kind: node.kind,
      body: node.body,
      sensitivity: node.sensitivity,
      hasEmbedding: vectorStr !== null,
    },
    metadata: data.metadata ?? {},
  });

  return node;
}
```

### Node Types

```typescript
type NodeId = number & { readonly __brand: "NodeId" };

type NodeKind =
  | "fact" | "preference" | "observation" | "belief"
  | "goal" | "person" | "place" | "event"
  | "pattern" | "principle";

type Sensitivity = "normal" | "financial" | "medical" | "identity" | "location" | "relationship";

type TrustTier = "owner" | "owner_confirmed" | "verified" | "inferred" | "external" | "untrusted";

interface Node {
  readonly id: NodeId;
  readonly kind: NodeKind;
  readonly body: string;
  readonly embedding: Float32Array | null;
  readonly trust: TrustTier;
  readonly confidence: number;
  readonly importance: number;
  readonly sensitivity: Sensitivity;
  readonly accessCount: number;
  readonly lastAccessedAt: Date | null;
  readonly createdAt: Date;
  readonly updatedAt: Date;
}
```

### Node Repository

All mutations emit events through the bus. The repository handles SQL + event emission.

```typescript
class NodeRepository {
  constructor(
    private readonly sql: Sql,
    private readonly bus: EventBus,
    private readonly embeddings: EmbeddingService,
  ) {}

  async create(data: CreateNodeInput): Promise<Node> {
    // See "Embedding Failure Handling" section above for full implementation
  }

  async update(nodeId: NodeId, data: UpdateNodeInput): Promise<Node> {
    // Re-embed if body changed
    let vectorStr: string | undefined;
    if (data.body !== undefined) {
      try {
        const embedding = await this.embeddings.embed(data.body);
        vectorStr = toVectorLiteral(embedding);
      } catch {
        // Keep existing embedding if re-embed fails
      }
    }

    const [row] = await this.sql`
      UPDATE node SET
        kind = COALESCE(${data.kind ?? null}, kind),
        body = COALESCE(${data.body ?? null}, body),
        embedding = COALESCE(${vectorStr ? sql`${vectorStr}::vector` : null}, embedding),
        trust = COALESCE(${data.trust ?? null}, trust),
        confidence = COALESCE(${data.confidence ?? null}, confidence),
        importance = COALESCE(${data.importance ?? null}, importance),
        sensitivity = COALESCE(${data.sensitivity ?? null}, sensitivity)
      WHERE id = ${nodeId}
      RETURNING *
    `;

    if (!row) {
      throw new Error(`Node ${nodeId} not found`);
    }

    const node = rowToNode(row);
    await this.bus.emit({
      type: "memory.node.updated",
      id: newEventId(),
      version: 1,
      actor: data.actor,
      data: { nodeId: node.id, fields: Object.keys(data) },
      metadata: data.metadata ?? {},
    });

    return node;
  }

  async adjustConfidence(nodeId: NodeId, delta: number, actor: string): Promise<void> {
    // Clamp result to [0.0, 1.0] in SQL
    const [row] = await this.sql`
      UPDATE node
      SET confidence = GREATEST(0.0, LEAST(1.0, confidence + ${delta}))
      WHERE id = ${nodeId}
      RETURNING id, confidence
    `;

    if (!row) {
      throw new Error(`Node ${nodeId} not found`);
    }

    await this.bus.emit({
      type: "memory.node.confidence_adjusted",
      id: newEventId(),
      version: 1,
      actor,
      data: { nodeId, delta, newConfidence: row.confidence as number },
      metadata: {},
    });
  }

  async findSimilar(embedding: Float32Array, threshold: number, limit: number): Promise<Node[]> {
    const queryStr = toVectorLiteral(embedding);
    // Compute distance once via ORDER BY + LIMIT (triggers HNSW index),
    // then filter by threshold in application code.
    const rows = await this.sql`
      SELECT *, 1 - (embedding <=> ${queryStr}::vector) AS similarity
      FROM node
      WHERE embedding IS NOT NULL
      ORDER BY embedding <=> ${queryStr}::vector
      LIMIT ${limit}
    `;
    return rows
      .filter((row) => (row.similarity as number) >= threshold)
      .map(rowToNode);
  }
}
```

`findSimilar` computes distance only once in the `ORDER BY` clause and derives similarity as an
output column. The `ORDER BY + LIMIT` pattern is what triggers HNSW index usage. Threshold filtering
happens in application code after the database returns the nearest neighbors — this avoids computing
distance twice in SQL and keeps the query plan optimal.

### Access Tracking

```typescript
  async recordAccess(nodeIds: readonly NodeId[]): Promise<void> {
    if (nodeIds.length === 0) return;
    await this.sql`
      UPDATE node
      SET access_count = access_count + 1,
          last_accessed_at = now()
      WHERE id = ANY(${nodeIds})
    `;
  }
```

`adjustConfidence` clamps the result to [0.0, 1.0] in SQL using `GREATEST`/`LEAST`. Phase 13
(contradiction detection) uses this to degrade confidence on contradicted nodes.

### Edge Types & Temporal Versioning

```typescript
type EdgeId = number & { readonly __brand: "EdgeId" };

interface Edge {
  readonly id: EdgeId;
  readonly sourceId: NodeId;
  readonly targetId: NodeId;
  readonly label: string;
  readonly weight: number;
  readonly validFrom: Date;
  readonly validTo: Date | null;  // null = currently active
  readonly createdAt: Date;
}
```

Updating an edge = expire the old one (`valid_to = now()`) + create a new one. Full history
preserved. Active edges have `valid_to IS NULL`.

```typescript
class EdgeRepository {
  async update(edgeId: EdgeId, newData: Partial<EdgeInput>): Promise<Edge> {
    return this.sql.begin(async (tx) => {
      // Expire old edge
      await tx`UPDATE edge SET valid_to = now() WHERE id = ${edgeId} AND valid_to IS NULL`;
      await this.bus.emit({ type: "memory.edge.expired", ... });

      // Create new edge with updated data
      const [row] = await tx`INSERT INTO edge (...) VALUES (...) RETURNING *`;
      await this.bus.emit({ type: "memory.edge.created", ... });

      return rowToEdge(row);
    });
  }

  async getActiveForNode(nodeId: NodeId): Promise<Edge[]> {
    const rows = await this.sql`
      SELECT * FROM edge
      WHERE (source_id = ${nodeId} OR target_id = ${nodeId})
        AND valid_to IS NULL
      ORDER BY created_at DESC
    `;
    return rows.map(rowToEdge);
  }
}
```

## Definition of Done

- [ ] `EmbeddingService.embed("hello")` returns a `Float32Array` of length 768
  (Xenova/all-mpnet-base-v2)
- [ ] `NodeRepository.create()` inserts a node with embedding and emits `memory.node.created`
- [ ] `NodeRepository.create()` succeeds with `embedding: null` when embedding fails
- [ ] `NodeRepository.update()` updates fields, re-embeds if body changed, emits
  `memory.node.updated`
- [ ] `NodeRepository.adjustConfidence()` adjusts and clamps to [0.0, 1.0], emits event
- [ ] `NodeRepository.findSimilar()` returns nodes above the similarity threshold using HNSW index
- [ ] `EdgeRepository.create()` inserts an active edge and emits `memory.edge.created`
- [ ] `EdgeRepository.update()` expires old edge + creates new edge in a transaction
- [ ] `EdgeRepository.getActiveForNode()` returns only edges where `valid_to IS NULL`
- [ ] pgvector strings round-trip correctly through `toVectorLiteral` / `fromVectorLiteral`
- [ ] No `as` casts — runtime type checks and branded type factory functions only
- [ ] All types use branded IDs (`NodeId`, `EdgeId`)
- [ ] `Node` interface includes `accessCount` and `lastAccessedAt` fields
- [ ] `NodeRepository.recordAccess()` batch-increments access_count and sets last_accessed_at
- [ ] `NodeKind` includes `"pattern"` and `"principle"`
- [ ] `just check` passes

## Test Cases

### `tests/memory/embeddings.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Correct dimension | Embed a string | Float32Array of length 768 |
| Deterministic | Embed same string twice | Same vector |
| Batch embedding | Embed 3 strings | 3 Float32Arrays |
| Lazy loading | Access pipeline after construction | Pipeline created on first call, not on construction |

### `tests/memory/graph/nodes.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Create node | Valid input | Node with ID, embedding populated, event emitted |
| Create node (embedding fails) | Mock embedding service throws | Node created with `embedding: null`, event emitted with `hasEmbedding: false` |
| Get by ID | Existing node ID | Returns node |
| Get by ID (missing) | Non-existent ID | Returns null |
| Update node | Change body | Node updated, re-embedded, event emitted |
| Update node (not found) | Non-existent ID | Throws error |
| Adjust confidence (positive) | adjustConfidence(id, +0.3) on node with confidence 0.8 | Confidence becomes 1.0 (clamped), event emitted |
| Adjust confidence (negative) | adjustConfidence(id, -0.5) on node with confidence 0.2 | Confidence becomes 0.0 (clamped), event emitted |
| Find similar | Seed 3 nodes, query with similar embedding | Returns matching nodes above threshold |
| Find similar (none) | Query with orthogonal embedding | Empty array |

### `tests/memory/graph/edges.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Create edge | Valid source + target | Active edge (valid_to = null), event emitted |
| Expire edge | Existing active edge | `valid_to` set, event emitted |
| Update edge | Existing edge | Old expired, new created, both events emitted |
| Active edges only | Mix of active and expired edges | Only active returned |
| Cascade delete | Delete source node | Edges cascade-deleted |

### `tests/memory/graph/access.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| Record access | recordAccess([id1, id2]) | Both nodes have access_count=1, last_accessed_at set |
| Record access idempotent | recordAccess([id1]) twice | access_count=2 |
| Record access empty | recordAccess([]) | No error, no DB call |
| Pattern node | Create node with kind="pattern" | Node created |
| Principle node | Create node with kind="principle" | Node created |

### `tests/memory/vector-roundtrip.test.ts`

| Test | Scenario | Expected |
| ------ | ---------- | ---------- |
| toVectorLiteral | Float32Array([0.1, 0.2, 0.3]) | `"[0.1,0.2,0.3]"` |
| fromVectorLiteral | `"[0.1,0.2,0.3]"` | Float32Array([0.1, 0.2, 0.3]) |
| Round-trip | toVectorLiteral then fromVectorLiteral | Same values (within float precision) |
| Insert + read | Insert vector via tagged template, read back | Matches original Float32Array |

## Risks

**Medium risk.** The `@huggingface/transformers` package is the main uncertainty:

- First-run downloads the ONNX model (~100MB). Tests should use a mock.
- ONNX with CoreML on macOS may have startup latency.
- The embedding dimension MUST match the `vector(768)` column — a mismatch is a runtime crash.

**Mitigation:** The embedding service is behind an interface. Tests inject a mock that returns
fixed-dimension vectors. Integration tests can use the real model if available, falling back to mock
otherwise.
