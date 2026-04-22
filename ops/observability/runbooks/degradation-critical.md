# DegradationCritical

## What it means

The degradation ladder has reached L4 — only interactive turns are allowed.
Executive, reflex, and ideation classes are paused. Something is very
wrong, or a manual `/degradation 4` was issued.

## Triage

1. Check the Autonomy dashboard for the `degradation.level_changed` events
   that preceded L4 — what reason did the emitter record?
2. Look at `theo_autonomy_violations_total` — a violation often triggers
   the ladder.
3. Confirm whether the change was human-initiated (`/degradation`) or
   automatic.

## Resolution

- **If automatic:** identify the underlying trigger (cost overrun, repeated
  handler failures, budget exhaustion) and fix it. Only once the trigger
  clears should degradation be lowered.
- **If manual:** the owner should reset the level via `/degradation 0 reason`
  when ready.

## Related

- Dashboard: [Autonomy](http://localhost:3000/d/theo-autonomy)
- Source: `src/degradation/state.ts`
