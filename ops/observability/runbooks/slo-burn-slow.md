# SLOSlowBurn

## What it means

A 6h + 30m burn-rate window shows an elevated but non-emergency error rate
— 5% of the 30d budget would burn within 6 hours if the trend continues.

## Triage

1. Review the SLOs dashboard for the trend line.
2. Correlate with recent events: schedule changes, config edits, model
   downgrades.

## Resolution

- **If burn is correlated with an off-peak window:** the alert can be
  silenced for the maintenance window and re-evaluated after.
- **If burn is correlated with sustained cloud-provider issues:** open a
  ticket with the provider and file a sustained-incident note.
- **Otherwise:** this is a quality-of-service trend; consider lowering the
  SLO target or investing in stability improvements before the budget
  approaches zero.

## Related

- Dashboard: [SLOs](http://localhost:3000/d/theo-slos)
- Source: `src/telemetry/slos.ts`
