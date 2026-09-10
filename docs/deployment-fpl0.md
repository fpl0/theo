# Theo on fpl0.local

The [10 September companion record](companion-baseline-2026-09-10.md) documents
release `20260910-a168268`: stable Telegram replies, standing full-host authority,
Luke-derived behavioral preferences, continued goal work and promised progress
checkpoints. The earlier [10 September upgrade record](deployment-fpl0-2026-09-10.md)
retains the `20260910-1cad162-r2` schema migration, canary and activation evidence. The [9 September record](deployment-fpl0-2026-09-09.md) retains the
earlier deployment and recovery evidence.
The [10 September monitoring record](evidence/observability-fpl0-2026-09-10.json)
covers the 4 GB resource envelope and alert corrections deployed independently
of that core release, including a recovered launchd replacement failure.

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

Strict `production_qualified` remains separate from operating permission. On
10 September the owner requested feature activation, so the installation uses
`operating_mode="owner_authorized"`, with background, autonomy, requested work,
models, deployments and notifications unpaused. Quiet hours are disabled. Native
account and isolation checks still apply to each attempt; no qualification
evidence or seven-day soak was fabricated. Host access is enabled, including the
separately installed privileged launcher. The later standing-owner grant permits
general and privileged host commands without repeated approval, with reads rooted
at `/`; durable receipts and revocation checks remain. The owner deferred GitHub authentication and self-maintenance
activation. Routine backups remain deferred pending the disk-encryption decision.

## Layout and service startup

- State: `~/Library/Application Support/Theo`, owner-only.
- Runner home: `~/.theo-runner`; each job has a scoped workspace.
- Worker and supervisor: `/opt/theo/worker-1cad162-r2` and
  `/opt/theo/supervisor-1cad162-r2`, installed
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

Monitoring can be deployed independently with optional `observability_source` and
`observability_python` paths in the manifest. They apply only to the observer and
stack services; the core source, release pointer, worker and supervisor stay pinned.
Install the locked observer wheel in a separate environment, retain the previous
manifest and plists, then generate only monitoring definitions with
`install --output ~/Library/LaunchAgents --services observer stack`. During a VM
resize, unload the stack recovery service first. Wait for the old monitoring
wrappers and their children to exit before bootstrapping their replacements;
`launchctl bootout` can return while shutdown is still in progress. Stop only its
Compose project and Colima profile, restart the profile with
`--memory 3 --activate=false`, and reload the two monitoring services. Retain volumes and verify health, queries, routes and
footprint. Restore the previous manifest, plists and VM allocation for monitoring
rollback. This operation does not pause the core or change its database.

## Upgrade or code rollback

Build clean committed source with `scripts/build_release.py`, including the
selected extras. Stage and verify the hashed release before a maintenance window.
Keep the last known working release and its matching worker environment.
The current host uses the system Python 3.14.7 with
`UV_PYTHON_PREFERENCE=only-system` during the build. A prior attempt selected a
managed interpreter below the user's home and failed to import `encodings` inside
the sandbox despite passing an ordinary init/doctor check. Verify an actual
sandboxed interpreter and worker import before every cutover.

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
The full stack budget is 4,000,000,000 bytes, including the VM and host helpers.
The dedicated VM has 3 GiB of RAM; Grafana has a 768 MiB hard limit and a 480 MiB
Go memory target. Historical 2 GB load reports do not qualify the new envelope.
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

Routine laptop test alerts stay in local diagnostics. Production notifications
group related incidents, preserve state during measurement gaps, and include the
host, component, measurement, next action and LAN dashboard link. See the
[alerting policy](observability.md#dashboards-and-alerts).
