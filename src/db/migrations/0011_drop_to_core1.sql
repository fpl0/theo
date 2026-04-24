-- Core 1 prune: drop tables for autonomous infrastructure that no longer
-- has TypeScript backing on `main`. The full implementation is preserved
-- on the `theo-complete` branch; tables are recreated by checking out that
-- branch and re-running migrations through 0010.

-- Scheduler (Phase 12)
DROP TABLE IF EXISTS job_execution CASCADE;
DROP TABLE IF EXISTS scheduled_job CASCADE;

-- Goals / executive function (Phase 12a)
DROP TABLE IF EXISTS goal_task CASCADE;
DROP TABLE IF EXISTS goal_state CASCADE;
DROP TABLE IF EXISTS resume_context CASCADE;
DROP TABLE IF EXISTS autonomy_policy CASCADE;

-- Webhooks / reflexes / ideation / proposals (Phase 13b)
DROP TABLE IF EXISTS webhook_delivery CASCADE;
DROP TABLE IF EXISTS webhook_body CASCADE;
DROP TABLE IF EXISTS webhook_secret CASCADE;
DROP TABLE IF EXISTS reflex_rate_limit CASCADE;
DROP TABLE IF EXISTS ideation_run CASCADE;
DROP TABLE IF EXISTS ideation_backoff CASCADE;
DROP TABLE IF EXISTS proposal CASCADE;
DROP TABLE IF EXISTS consent_ledger CASCADE;
DROP TABLE IF EXISTS degradation_state CASCADE;

-- Self-update (Phase 15)
DROP TABLE IF EXISTS self_update_state CASCADE;

-- Self-model: gated autonomy in Phase 13b. Without autonomy, the calibration
-- table is dead weight. The session manager no longer records predictions.
DROP TABLE IF EXISTS self_model_domain CASCADE;

-- effective_trust_tier on events: Phase 13b's causation-chain trust
-- propagation is gone. Core 1 always writes 'owner' or 'system'. The column
-- stays for now (events are immutable; the column has DEFAULT 'owner' from
-- migration 0008), but no read path uses it.
