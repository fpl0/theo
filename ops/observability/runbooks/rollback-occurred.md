# RollbackOccurred

## What it means

A `system.rollback` event fired — the startup healthcheck failed on the
current commit, and the engine reset the working tree to the last healthy
commit. Theo is now running the old code.

## Triage

1. Read the `system.rollback` event's `fromCommit` → `toCommit` pair.
2. Inspect the failed commit: what did `just check` reject?
3. Was the rollback automatic (healthcheck failure) or manual?

## Resolution

- **If automatic:** fix the broken commit, push to a feature branch, re-run
  CI, and only merge when `just check` passes.
- **If the rollback itself left artifacts on disk:** clean the workspace
  and let the next boot re-verify.

## Related

- Dashboard: [Overview](http://localhost:3000/d/theo-overview)
- Source: `src/selfupdate/healthcheck.ts`, `src/selfupdate/rollback.ts`
