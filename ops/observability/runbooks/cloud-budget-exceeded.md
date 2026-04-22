# CloudBudgetExceeded

## What it means

Autonomous cloud spend in the last 24 hours has exceeded the configured
daily budget. Every further autonomous turn is either consuming money the
owner did not authorize or breaking a guard.

## Triage

1. Open the Cost dashboard and identify which `turn_class` (reflex,
   executive, ideation) dominates.
2. Check the degradation level — if it's still L0, the ladder should be
   raised to L2+ to stop ideation immediately.
3. Review recent `cloud_egress.turn` events for outlier spend.

## Resolution

- **If spend is a burst (one runaway job):** cancel the offending goal via
  `/cancel <id>` and raise degradation to L2.
- **If spend is sustained:** revoke consent via `/consent revoke` — all
  autonomous turns halt until the owner re-grants consent.
- **If the budget itself is too low:** adjust the daily budget in config
  after the owner agrees the spend is worth it.

## Related

- Dashboard: [Cost](http://localhost:3000/d/theo-cost)
- Source: `src/memory/egress.ts`, `src/memory/cloud_audit.ts`
