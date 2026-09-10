# fpl0.local upgrade — 10 September 2026

The owner authorized upgrading Theo, merging the development branch into the
default branch, and clearing operating pauses. PR #4 merged into `main` at
`1cad16290c346d9dc2eae8ea3880cf0e414c2187`. The host runs release
`20260910-1cad162-r2` from that source, using Python 3.14.7. The final documentation
commit does not change the installed application code.

## Upgrade and recovery preparation

The previous release was `20260909-ffdb217`, using schema 5. The new release uses
schema 8. Before touching production schema, the operator took a verified local
snapshot and tested migrations on a separate private copy. Existing canonical
rows were preserved, SQLite integrity and foreign-key checks passed, and both
installed application versions created durable local jobs, memories and synthetic
delivery receipts that the other version could reopen.

The previous source's offline suite also ran against schema 8: **352 passed, one
Linux-only identity skip**. This supported staging a separate compatibility
descriptor, `20260910-ffdb217-schema8`, without modifying the original release.
It retains the previous application bytes and accepts schemas 5 through 8.
Its manifest includes the compatibility evidence. Production rollback was not
exercised during this upgrade; the retained code recovery path does not restore
an older database.

Production work drained to zero running jobs and executing actions/outbox entries.
The supervisor stopped the core, a fresh mandatory pre-switch snapshot passed
verification, and the old supervisor wrapper and descendants exited before the
new launchd definition was loaded. The release pointer switched through the
operator release service. Core, worker and independent supervisor environments
use the same lock and installed application source. Monitoring remains on its
independently deployed environment.

## Runtime and feature checks

Both the PR head and merged application commit passed Linux and macOS CI,
including lint, formatting, strict typing, offline tests, distributions and the
installed-package check. The three target runtime environments each passed the
outside-checkout smoke check: **127 modules, 8 core migrations and 4 controller
migrations**. Browser, embedding and speech extras are included.

The initial build selected managed Python 3.14.5 below the user's home. Ordinary
package checks passed, but the sandboxed interpreter failed to import `encodings`.
That artifact was not activated. Rebuilding with the host's system Python 3.14.7
resolved the issue. A real sandbox process was denied protected-core reads and
writes, and the worker imported Theo and MCP successfully. Both native Codex
executables matched the retained runtime manifest.

A live operator canary ran Codex `gpt-6-astra` through the production coordinator
and broker. It called `get_status`, executed the standing-permission `id` host
diagnostic and inspected its result, then delivered a greeting to the configured
private owner Telegram conversation. The native run, host action and Telegram
action completed, and a persisted Telegram receipt was verified. This tested an
operator-queued request and real outbound Telegram delivery; it was not a new
Telegram user-client ingress campaign.

The privileged launcher at `/private/var/theo/bin/host-command-fpl0` uses the
previously pinned, root-owned helper. Its helper bytes match this release's source.
The exact no-argument sudo rule passed `visudo`; a host canary returned UID 0,
while native and generated worker processes were denied direct launcher access.
The production core was restarted with that launcher configured. Normal command
approval and receipt requirements remain in effect.

Additional target checks produced a finite 768-dimensional embedding, fetched and
rendered a public example page, generated a local voice clip and transcribed its
synthetic phrase correctly. Grafana, Prometheus, Loki and Tempo health endpoints
returned HTTP 200; Grafana was also reached from the operator's Mac over the LAN.
The checked monitoring footprint was approximately 3.15 GB, below its 4 GB budget.
This was a point observation, not a new capacity or soak qualification.

## Operating state and remaining setup

The active configuration uses `operating_mode="owner_authorized"` and grants the
private requested-work runtime-control tool all supported pause scopes. Background,
autonomy, requested work, models, deployments and notifications were all unpaused.
Maintenance draining and quarantine were false, and quiet hours were disabled.
Unprivileged and approval-bound privileged host access were enabled.

The owner deferred remote GitHub authentication. The maintenance controller is
not connected and proactive self-maintenance remains disabled until its separate
installation and live acceptance are completed. Routine backups remain disabled
pending an explicit disk-encryption decision; FileVault is off. Mandatory verified
local release snapshots use the previously authorized exception. Neither this
upgrade nor clearing operating pauses establishes full production qualification,
machine-loss recovery or a seven-day soak.

Raw sanitized checks are in [the upgrade evidence](evidence/upgrade-fpl0-2026-09-10.json).
