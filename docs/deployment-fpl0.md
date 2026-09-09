# Theo on fpl0.local

The [9 September deployment record](deployment-fpl0-2026-09-09.md) documents the
installed release, observed recovery drills and remaining activation steps.

This deployment runs native Theo on the Apple Silicon Mac `fpl0.local`, with a
dedicated Colima Grafana/Alloy/Prometheus/Loki/Tempo stack. Existing assistants and
Docker contexts are preserved. Grafana is the authenticated LAN entry point;
collector and storage ports remain loopback-only.

The owner selected Codex only, deferred disk encryption and routine backups, and
accepted manual login after reboot. `encrypted_storage_verified` remains false.
`required_backends=["codex"]`, `require_encrypted_storage=false`,
`scheduled_backups_enabled=false`, and `allow_unencrypted_release_backup=true`
record these choices. The last setting permits only mandatory local snapshots
before code switching; ordinary backups still require verified encryption. There
is no machine-loss recovery or backup RPO claim.

Strict `production_qualified` remains separate from `deployment_ready` under this
policy. Both require real evidence, and policy changes invalidate deployment
evidence. Background autonomy remains paused until native, Mac, behaviour,
capacity/restore, deterministic and genuine seven-day soak gates pass. A running
bot or successful greeting does not establish those gates.

## Layout and service startup

- State: `~/Library/Application Support/Theo`, owner-only.
- Runner home: `~/.theo-runner`; each job has a scoped workspace.
- Worker and supervisor: `/opt/theo/worker` and `/opt/theo/supervisor`, installed
  from the same lock, outside protected state and the runner's writable paths.
- Native runtime: `/opt/theo/native/codex` and its sibling
  `codex-code-mode-host`, copied together from the same pinned distribution.
  Record both checksums in `runtime-manifest.json`. Copying only `codex` lets
  conversation text work while native tool execution fails.
- Deployment source and manifest are recorded by the installation. The private
  environment JSON contains bot credentials and Grafana's generated password.

`scripts/deploy_services.py --manifest MANIFEST install --output ~/Library/LaunchAgents`
generates `local.theo.supervisor`, `local.theo.observer`, and `local.theo.stack`.
Bootstrap them in `gui/501` after provisioning. Credentials are absent from plists;
services read the owner-only environment file. Service logs rotate at 2 MB with
two backups. The stack supervisor recreates stopped containers and starts the
dedicated VM when needed; the core supervisor has bounded recovery backoff.

## Upgrade or code rollback

Build clean committed source with `scripts/build_release.py`, including the
selected extras. Stage and verify the hashed release before a maintenance window.
Keep the last known working release and its matching worker environment.

Set `maintenance_draining=true` through the operator database API, wait until
running jobs and executing deliveries finish, then `theo service pause`. Confirm
the core has stopped. `upgrade --release ID` and `rollback --release ID` require
the daemon lock, drained workers, a compatible schema and a verified pre-switch
snapshot. They atomically change application code and leave autonomy paused;
neither restores SQLite. Synchronize the worker when its dependencies change.

Clear `maintenance_draining`, resume supervision, and check doctor, heartbeat,
Telegram polling, the model canary, and dashboards. The native canary must call
a Theo tool and verify its committed result and final delivery receipt; a greeting
or successful MCP tool listing alone does not exercise Codex's tool execution
helper. Check `/status` separately to verify the command path. If the startup canary
fails, pause supervision, switch to the previous compatible release, and repeat checks.
Never automatically retry uncertain effects. Restore into a new quarantined root
only after a separate recovery decision and explicit reconciliation.

## Monitoring and account renewal

Grafana at `http://fpl0.local:13000` requires authentication. No router forwarding
is configured. Prometheus, Loki and Tempo are accessed through its data sources.
Open `/d/theo-overview?var-environment=production` or select **Production Theo**
in the Traffic selector. Polling alerts evaluate each environment separately so
test or laptop traffic cannot mask a stopped production poller.
The full stack budget is 2,000,000,000 bytes, including the VM and host helpers.
Metrics retain seven days, logs three days, and traces 24 hours; production trace
sampling is 10%. Qualification traffic is explicitly labelled separately.

Local health alerts go to Telegram independently of model execution. A configured
Healthchecks.io heartbeat provides host/network outage alerts through Telegram;
until its private ping URL and integration are verified, external monitoring is
an explicit gap. Use one-minute heartbeats and a two-minute grace period.

Codex checks its native subscription identity, model catalogue and current included
allowance before each turn, and monitors account/allowance changes during inference.
It refuses API-key authentication, available paid credits, and unknown or exhausted
allowance. No daily manual attestation is required. Observations do not claim that
provider purchase settings are disabled. Resolve any reported access failure and
explicitly retry waiting jobs; requested reminders remain available during model
pauses. See the [account workflow](operations.md#account-evidence).
