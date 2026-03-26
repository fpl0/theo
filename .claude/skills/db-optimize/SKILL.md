---
name: db-optimize
description: "You are a Principal Engineer specializing in PostgreSQL performance. Analyze and optimize Theo's database. Do NOT make changes without confirming first."
user-invocable: true
---

You are a Principal Engineer specializing in PostgreSQL performance. Analyze and optimize Theo's database with deep expertise. Do NOT make changes without confirming first.

## 1. Collect diagnostics

Connect to the database using the credentials in `.env.local` via `psql` or `docker compose exec postgres psql -U theo`. Run these diagnostic queries and record every result before making recommendations.

### Cluster health

```sql
SELECT version();
SELECT pg_size_pretty(pg_database_size('theo')) AS db_size;
SELECT name, setting, unit, source FROM pg_settings
WHERE name IN (
  'shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem',
  'random_page_cost', 'effective_io_concurrency', 'max_connections',
  'max_parallel_workers_per_gather', 'max_worker_processes',
  'wal_buffers', 'checkpoint_completion_target', 'max_wal_size',
  'autovacuum', 'autovacuum_vacuum_scale_factor', 'autovacuum_analyze_scale_factor',
  'autovacuum_vacuum_cost_delay', 'autovacuum_vacuum_cost_limit',
  'jit', 'huge_pages', 'default_statistics_target'
) ORDER BY name;
```

### Table statistics

```sql
SELECT
  schemaname, relname,
  n_live_tup, n_dead_tup,
  CASE WHEN n_live_tup > 0
    THEN round(100.0 * n_dead_tup / n_live_tup, 1)
    ELSE 0
  END AS dead_pct,
  seq_scan, seq_tup_read,
  idx_scan, idx_tup_fetch,
  CASE WHEN seq_scan + idx_scan > 0
    THEN round(100.0 * seq_scan / (seq_scan + idx_scan), 1)
    ELSE 0
  END AS seq_pct,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
  last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC;
```

### Index analysis

```sql
-- Index usage: are all indexes being used?
SELECT
  schemaname, relname, indexrelname,
  idx_scan, idx_tup_read, idx_tup_fetch,
  pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC;

-- Duplicate or overlapping indexes (leftmost prefix match)
SELECT
  a.indexrelid::regclass AS index_a,
  b.indexrelid::regclass AS index_b,
  a.indrelid::regclass AS table_name
FROM pg_index a
JOIN pg_index b ON a.indrelid = b.indrelid
  AND a.indexrelid <> b.indexrelid
  AND a.indkey::text = LEFT(b.indkey::text, length(a.indkey::text))
WHERE a.indrelid::regclass::text NOT LIKE 'pg_%';

-- Index bloat estimation
SELECT
  schemaname, tablename, indexname,
  pg_size_pretty(pg_relation_size(indexname::regclass)) AS index_size,
  idx_scan
FROM pg_stat_user_indexes
JOIN pg_indexes USING (schemaname, tablename, indexname)
WHERE idx_scan = 0 AND indexname NOT LIKE 'pg_%'
ORDER BY pg_relation_size(indexname::regclass) DESC;
```

### Connection pool and activity

```sql
SELECT
  state, wait_event_type, wait_event,
  count(*) AS count,
  max(now() - state_change) AS longest_in_state
FROM pg_stat_activity
WHERE datname = 'theo'
GROUP BY state, wait_event_type, wait_event
ORDER BY count DESC;

SELECT count(*) AS total_connections,
  count(*) FILTER (WHERE state = 'active') AS active,
  count(*) FILTER (WHERE state = 'idle') AS idle,
  count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_tx,
  count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting_on_lock
FROM pg_stat_activity
WHERE datname = 'theo';
```

### Table and index bloat (real estimate)

```sql
SELECT
  current_database(), schemaname, tablename,
  pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)) AS total_size,
  pg_size_pretty(pg_relation_size(schemaname || '.' || tablename)) AS table_size,
  pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)
    - pg_relation_size(schemaname || '.' || tablename)) AS index_overhead
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname || '.' || tablename) DESC;
```

### Slow query patterns (if pg_stat_statements is available)

```sql
SELECT
  calls, total_exec_time::numeric(10,2) AS total_ms,
  mean_exec_time::numeric(10,2) AS mean_ms,
  max_exec_time::numeric(10,2) AS max_ms,
  rows,
  left(query, 120) AS query_preview
FROM pg_stat_statements
WHERE dbname = 'theo'
ORDER BY mean_exec_time DESC
LIMIT 20;
```

If `pg_stat_statements` is not installed, recommend enabling it:
```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```
And adding to postgresql.conf: `shared_preload_libraries = 'pg_stat_statements'`.

## 2. Analyze the schema

Read all migration files in `src/theo/db/migrations/`. Apply deep PostgreSQL expertise:

### HNSW vector index tuning

The default HNSW parameters (`m=16`, `ef_construction=64`) are suitable for small datasets. Evaluate whether the dataset size warrants different parameters:

- **< 100K rows**: defaults are fine. `m=16, ef_construction=64`.
- **100K–1M rows**: consider `m=24, ef_construction=100`. Better recall at marginal build cost.
- **> 1M rows**: consider `m=32, ef_construction=128`. Also evaluate IVFFlat as an alternative for bulk insert workloads.

Check if `SET hnsw.ef_search = N` should be tuned at the session level for recall vs speed tradeoff. Default is 40; raise to 100–200 for higher recall.

### Foreign key indexes

PostgreSQL does NOT auto-create indexes on FK columns. Verify that `edge.source_id` and `edge.target_id` have indexes (they should from the composite indexes, but confirm the composite index satisfies FK-based DELETE CASCADE lookups — it does if the FK column is the leftmost in the index).

### Partial indexes

Evaluate whether these patterns exist or are needed:
- `WHERE embedding IS NOT NULL` — skip un-embedded rows in vector search
- `WHERE valid_to IS NULL` on edge — already present (`ix_edge_current`), confirm it's being used
- `WHERE kind = 'X'` for hot query patterns on specific node kinds

### TOAST and large text

The `body` column stores arbitrary text. PostgreSQL TOASTs values > 2KB. If bodies are frequently large (> 8KB), consider:
- Whether `EXTERNAL` storage strategy would help (avoids compression overhead for already-compressed or opaque data)
- Whether a summary column should be added for search instead of full-text on the entire body

### Autovacuum tuning for write-heavy tables

If `node` or `edge` tables have high write rates:
```sql
ALTER TABLE node SET (
  autovacuum_vacuum_scale_factor = 0.05,    -- vacuum at 5% dead rows (default 20%)
  autovacuum_analyze_scale_factor = 0.02     -- analyze at 2% changes (default 10%)
);
```

### Statistics targets

For columns used in complex WHERE clauses or JOINs, consider raising the statistics target:
```sql
ALTER TABLE node ALTER COLUMN kind SET STATISTICS 500;   -- default is 100
ALTER TABLE edge ALTER COLUMN label SET STATISTICS 500;
```

## 3. Analyze connection pool configuration

Read `src/theo/db/pool.py` and evaluate:

- **`min_size` / `max_size`**: for a single-process personal agent, `min=2, max=10` is reasonable. But if all queries are sequential, `max=5` saves memory. Each idle connection holds ~5–10MB of server memory.
- **`command_timeout=60s`**: fine for migrations. For normal queries, 30s is more defensive. Consider setting it lower and using per-query timeouts for long operations.
- **`max_inactive_connection_lifetime=300s`**: good. Prevents stale connections behind proxies or firewalls.
- **`server_settings`**: `application_name=theo` is set. Consider adding `statement_timeout` as a server-level safety net: `server_settings={"application_name": "theo", "statement_timeout": "30000"}`.

## 4. Check for missing PostgreSQL extensions

Evaluate whether these extensions would benefit the workload:

- **`pg_stat_statements`**: essential for production query analysis. Always enable.
- **`pg_trgm`**: if fuzzy text search (trigram similarity) is needed beyond tsvector.
- **`btree_gist`** or **`btree_gin`**: if exclusion constraints or multi-type GIN indexes are needed.
- Do NOT recommend extensions that aren't clearly needed.

## 5. EXPLAIN ANALYZE key queries

If you can identify the application's hot queries (from code or `pg_stat_statements`), run `EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)` on them to verify index usage and identify:
- Sequential scans that should be index scans
- Bitmap heap scans that could be index-only scans (check `VACUUM` and visibility map)
- Nested loops that should be hash joins (or vice versa)
- Sort operations that could be eliminated by index ordering

## 6. Present findings

Organize recommendations into three tiers:

### Critical (do now)
Issues causing measurable performance degradation or data integrity risk.

### Recommended (do soon)
Improvements that will matter as data grows. Quantify the threshold (e.g., "when node table exceeds 100K rows").

### Informational (monitor)
Observations that are fine today but should be watched. Include the diagnostic query to re-run.

For each recommendation, provide:
- **What**: one-line description
- **Why**: the specific diagnostic result that triggered it
- **Impact**: quantified improvement (e.g., "reduces DELETE CASCADE from O(n) seqscan to O(log n) index scan")
- **How**: exact SQL or code change
- **Risk**: what could go wrong, lock duration, whether it requires downtime

Wait for explicit approval before creating any migration files or running any DDL.
