# AutonomyViolation

## What it means

Theo attempted an action outside its permitted autonomy domain — the
policy engine rejected it, and the attempt was logged. This may be benign
(a subagent misjudging the permitted scope) or malicious (a prompt
injection trying to escalate).

## Triage

1. Inspect the most recent `autonomy.violation` log entry for `domain`
   (e.g., `git_write`, `cloud_api`).
2. Cross-reference with the active goal or reflex — which caused the
   attempt?
3. Review the surrounding turn in the Trace view via the linked exemplar.

## Resolution

- **If it was a legitimate owner intent:** raise the domain's permitted
  level for that goal via the proposal workflow (`/approve`).
- **If it looks like prompt injection:** rotate any webhook secrets used
  by the source, quarantine the goal, and replay the causation chain to
  understand how external content reached the executor.

## Related

- Dashboard: [Autonomy](http://localhost:3000/d/theo-autonomy)
- Source: `src/goals/`, `src/memory/egress.ts`
