---
paths: ["src/memory/**", "tests/memory/**"]
---

# Memory system conventions

## Tiers

- **Knowledge Graph** (nodes + edges): semantic facts, preferences.
  Nodes have kind, body, embedding, trust/confidence/importance,
  access_count, last_accessed_at. Edges are temporally versioned
  (valid_from/valid_to). Node kinds: `fact`, `preference`,
  `observation`, `belief`, `goal`, `person`, `place`, `event`,
  `pattern`, `principle`.
- **Episodic**: conversation messages. Append-only. Linked to nodes
  via episode_node cross-reference.
- **Core Memory**: 4 named JSON documents (persona, goals, user_model,
  context). Always in system prompt, never truncated. Changelogged on
  every mutation.
- **User Model**: structured dimensions across psychological
  frameworks. Confidence computed from evidence count.
- **Self Model**: calibration per task domain (including
  session_management). Predictions vs outcomes.
- **Procedural Memory (Skills)**: learned behavioral patterns with
  trigger_context, strategy, success_rate, version lineage. Retrieved
  by trigger embedding similarity (separate from RRF). Promoted to
  persona when proven.

## Hybrid Retrieval (RRF)

Three signals fused in a single SQL query:

1. Vector similarity (pgvector HNSW, cosine distance)
2. Full-text search (tsvector/ts_rank_cd)
3. Graph traversal (BFS from vector seeds, recursive CTE)

Fusion: `score(node) = sum(1/(k + rank))` across signals. k=60 default.

The entire fusion is ONE database round-trip — a multi-CTE SQL query. No application-level merging.

## Privacy

Privacy is a gate, not a filter. Reject at the storage boundary,
before data enters the event log (which is immutable). The privacy
filter is a pure function: trust tier + content heuristics =
allow/reject decision.

## Key invariants

- Episodes are append-only — never UPDATE, use superseded_by for consolidation
- Core memory has exactly 4 slots — persona, goals, user_model, context
- All node storage goes through the privacy filter before persisting
- Edge `valid_to IS NULL` means active — always filter on this
- Hybrid retrieval is a single SQL query — no application-level post-processing loops
- Contradiction detection is async/background — never blocks node storage
- Trust tiers are ordered: owner > owner_confirmed > verified > inferred > external > untrusted
- Skills are retrieved by trigger embedding similarity, not by RRF content search
- Pattern and principle nodes are exempt from forgetting curve decay
- Promoted skills (promoted_at IS NOT NULL) are excluded from active skill retrieval
- Forgetting floor is 0.05 — nodes never fully disappear, decay affects ranking not existence
- Access tracking (access_count increment) on retrieval is fire-and-forget — never blocks search
- System prompt is ordered stable→volatile for cache efficiency (see foundation.md §3.7)
