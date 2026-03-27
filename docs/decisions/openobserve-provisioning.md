# OpenObserve Provisioning

## Context

Theo has comprehensive OTEL instrumentation (13 metrics, 24 spans, 60+ structured log messages) flowing to OpenObserve, but OpenObserve starts as a blank slate. Every `just reset` or fresh setup required manual dashboard creation. For an agent built for decades where observability is non-negotiable, understanding Theo's state should be immediate — not a manual setup chore.

## Decisions

### JSON files + shell script over Python script or volume mounts

JSON is the native OpenObserve export/import format. A shell script with `curl` and `python3 -c` for JSON parsing keeps it dependency-free and language-agnostic. Volume mounts were rejected because OpenObserve doesn't support config-file-based dashboard provisioning (unlike Grafana). A Python script was rejected to avoid adding runtime code for an infrastructure concern.

### 6 dashboards by concern, not 1 mega-dashboard

Matches Theo's module structure: Overview, Conversation, LLM, Memory, Resilience, Telegram. Each dashboard loads fast and has a clear scope. The Overview dashboard is the "is everything OK?" entry point; the others are for drill-down.

### PromQL for metrics, SQL for traces/logs

PromQL is the standard for time-series aggregation (rates, percentiles, sums). SQL queries against the traces/logs streams are used for tabular data (recent events, operation breakdowns) since OpenObserve stores those as structured records.

### Idempotent provisioning via title-based matching

The script uses dashboard titles (not IDs) to detect existing dashboards. Server-generated IDs are never stored in the JSON files. On update, the current hash is fetched to satisfy OpenObserve's conflict detection. Safe to run repeatedly.

### Alerts are best-effort

OpenObserve requires streams to exist before alerts can reference them. Since streams are created only after Theo sends its first data, alerts are skipped gracefully on fresh installs. Re-running `just dashboards` after Theo has run once creates all 5 alerts. A "silent" destination (webhook to self) satisfies the API's requirement for a destination without needing external configuration.

### Integrated into `just dev` and `just reset`

`just dev` now runs `up` → `dashboards` → `run`. `just reset` re-provisions after nuking volumes. Zero manual steps.

### Log stream bootstrap for alerts

OpenObserve requires a stream to exist before alerts can reference it. The provision script seeds a single init entry into the `default` log stream if it doesn't exist yet. This makes `just dashboards` work on a completely fresh install without waiting for Theo to run first.

### PostgreSQL 18 volume layout

PG18 Docker images changed the data directory convention — they expect the mount at `/var/lib/postgresql` (parent), not `/var/lib/postgresql/data`. The compose volume was updated to match. This is unrelated to OpenObserve but was discovered during provisioning testing.

## Files changed

- `infra/provision.sh` — provisioning script (dashboards + alerts)
- `infra/dashboards/*.dashboard.json` — 6 dashboard definitions
- `infra/alerts/*.alert.json` — 5 alert definitions
- `justfile` — `dashboards` target, updated `dev` and `reset`
- `docker-compose.yml` — PG18 volume mount fix (`/var/lib/postgresql`)
- `.claude/skills/setup/SKILL.md` — added provisioning to Phase 3
- `CLAUDE.md` — documented dashboards in Quick Start and Infrastructure
