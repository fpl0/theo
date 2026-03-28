"""SQL queries for edge operations.

Extracted from :mod:`theo.memory.edges` to keep that module focused on
the public API and under the ~200 line convention.
"""

EXPIRE_ACTIVE_EDGE = """
UPDATE edge
SET valid_to = now()
WHERE source_id = $1
    AND target_id = $2
    AND label = $3
    AND valid_to IS NULL
"""

INSERT_EDGE = """
INSERT INTO edge (source_id, target_id, label, weight, meta)
VALUES ($1, $2, $3, $4, $5)
RETURNING id
"""

SELECT_EDGES_OUTGOING = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE source_id = $1 AND valid_to IS NULL
ORDER BY created_at
"""

SELECT_EDGES_OUTGOING_BY_LABEL = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE source_id = $1 AND label = $2 AND valid_to IS NULL
ORDER BY created_at
"""

SELECT_EDGES_INCOMING = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE target_id = $1 AND valid_to IS NULL
ORDER BY created_at
"""

SELECT_EDGES_INCOMING_BY_LABEL = """
SELECT id, source_id, target_id, label, weight, meta, valid_from, valid_to, created_at
FROM edge
WHERE target_id = $1 AND label = $2 AND valid_to IS NULL
ORDER BY created_at
"""

TRAVERSE = """
WITH RECURSIVE graph AS (
    SELECT
        target_id AS node_id,
        1 AS depth,
        ARRAY[source_id, target_id] AS path,
        weight AS cumulative_weight
    FROM edge
    WHERE source_id = $1
        AND valid_to IS NULL

    UNION ALL

    SELECT
        e.target_id,
        g.depth + 1,
        g.path || e.target_id,
        g.cumulative_weight * e.weight
    FROM graph g
    INNER JOIN edge e ON e.source_id = g.node_id
    WHERE e.valid_to IS NULL
        AND g.depth < $2
        AND e.target_id <> ALL(g.path)
)
SELECT DISTINCT ON (node_id)
    node_id, depth, path, cumulative_weight
FROM graph
ORDER BY node_id, cumulative_weight DESC
"""

TRAVERSE_BY_LABEL = """
WITH RECURSIVE graph AS (
    SELECT
        target_id AS node_id,
        1 AS depth,
        ARRAY[source_id, target_id] AS path,
        weight AS cumulative_weight
    FROM edge
    WHERE source_id = $1
        AND valid_to IS NULL
        AND label = $3

    UNION ALL

    SELECT
        e.target_id,
        g.depth + 1,
        g.path || e.target_id,
        g.cumulative_weight * e.weight
    FROM graph g
    INNER JOIN edge e ON e.source_id = g.node_id
    WHERE e.valid_to IS NULL
        AND e.label = $3
        AND g.depth < $2
        AND e.target_id <> ALL(g.path)
)
SELECT DISTINCT ON (node_id)
    node_id, depth, path, cumulative_weight
FROM graph
ORDER BY node_id, cumulative_weight DESC
"""

EXPIRE_EDGE_BY_ID = """
UPDATE edge
SET valid_to = now()
WHERE id = $1 AND valid_to IS NULL
"""
