-- Trust provenance: every node carries a trust tier for retrieval filtering
-- and adversarial robustness (external data cannot silently override owner data).
-- Tiers follow the research consensus (§7 Research Foundations):
--   owner     – directly provided by the user in conversation (highest trust)
--   verified  – user explicitly confirmed external data or agent inference
--   inferred  – agent derived from behaviour, consolidation, or reasoning
--   external  – sourced from APIs, documents, email, web (cannot override owner)

ALTER TABLE node
    ADD COLUMN trust text NOT NULL DEFAULT 'inferred'
        CONSTRAINT node_trust_check
            CHECK (trust IN ('owner', 'verified', 'inferred', 'external'));

-- Fast trust-filtered retrieval: "entities of kind X that I can trust".
CREATE INDEX IF NOT EXISTS ix_node_kind_trust ON node (kind, trust);

-- Confidence semantics: weight represents confidence 0.0–1.0, not an
-- arbitrary magnitude.  Enforce the range so the agent cannot assign
-- meaningless values.
ALTER TABLE edge
    ADD CONSTRAINT edge_weight_range CHECK (weight >= 0.0 AND weight <= 1.0);

-- Edge deduplication: only one *active* edge of a given type between two
-- nodes.  The agent expires the old edge (sets valid_to) before creating a
-- new one, preserving full temporal history while preventing duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS ix_edge_active_unique
    ON edge (source_id, target_id, label) WHERE valid_to IS NULL;
