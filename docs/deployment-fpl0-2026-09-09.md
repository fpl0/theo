# fpl0.local deployment — 9 September 2026

Theo's production infrastructure is running on `fpl0.local`. A native Codex reply
has now passed on that host, but Theo conversation activation still requires
current Codex spending-control attestation. Healthchecks.io sign-in remains a
separate monitoring gap. This is an operational deployment with explicit remaining
qualification gates, not a claim of full production qualification.

## Installed state

- Active release: `20260909-7cc0327`, source
  `7cc032700171087019a4e20471abb7579760d052`.
- Previous compatible release retained: `20260909-2e44471`.
- Apple Silicon, macOS 15.7.9, 16 GiB RAM; sleep disabled and power-loss restart enabled.
- Native Codex 0.153.4 pinned separately; selected model `gpt-6-astra`.
  Native account inspection reported ChatGPT Pro. No model account was enrolled
  without the required spending-control evidence.
- Production Telegram: [Theo](https://t.me/theo_fpl0_bot).
  Independent alerts: [Theo Health](https://t.me/theo_fpl0_health_bot).
- Three launchd agents supervise the core, observer and dedicated Colima stack.
  Startup after a reboot requires the owner's login, as selected in the plan.
  A full host reboot was not performed during this deployment.
- [Production Grafana](http://fpl0.local:13000/d/theo-overview?var-environment=production),
  also reachable at `http://192.168.0.125:13000`. Authentication is required;
  telemetry/storage ports remain on loopback. WAN exposure and TLS termination
  are not configured. The private local `.local/deployment/operator-access.txt`
  contains the generated login details and has mode 0600.
- Browser binaries, embeddings and a pinned local
  [MLX Whisper small model](https://huggingface.co/mlx-community/whisper-small-mlx)
  were installed. Synthetic speech transcription passed.

## Observed safeguards

The core recovered in **26.06 seconds** after a controlled process crash. The
independent health notification was accepted and visibly received in Telegram.
Existing successful delivery receipts were preserved.

The real installation passed **upgrade → rollback → upgrade** between the two
retained releases. Each switch waited for the core to stop, verified the release
and schema, created a pre-switch snapshot and switched the current pointer.
SQLite integrity remained healthy and successful receipts stayed intact. These
were compatible schema-5 code changes; no database rollback was attempted.

The latest snapshot restored into a separate unused root in **0.13 seconds**.
Quarantine, notification pause and background pause were verified. This small,
fresh production snapshot had zero external blobs; it does not qualify the large
capacity fixture or machine-loss recovery.

A generated-code probe against the actual production paths could not read core
configuration, service secrets, releases, either native credential location or a
sibling workspace. Service control was denied and its own workspace remained
writable. These results, restart, maintenance, alerts and restore evidence were
recorded for the Mac deployment gate.

Grafana recovered automatically in **34.18 seconds** after an intentional stop.
The core PID was unchanged and its heartbeat remained fresh. The alert test's
firing and resolved notifications were both visibly received in Telegram.
The production bot's `/start` and `/status` responses were also verified through
the actual Telegram application. These commands do not establish model inference.

## Validation scope

- Initial deployment implementation: 299 offline tests passed, one Linux-only
  dedicated-UID test skipped on macOS. This preceded the later builder and
  production-monitoring fixes; it is not a full-suite claim for the final release.
- Final release: 21 focused tests passed on fpl0.local without skips, covering
  telemetry, supervisor, deployment, release-builder and macOS isolation paths.
- Final production code passed strict Pyright; Ruff lint and formatting passed.
- Pipeline validation passed all six health checks, 86 metric/dashboard queries,
  10 log queries and 19 alert queries. Trace storage, log correlation, exemplars
  and Grafana trace-to-log navigation queries were verified.
- Eleven polling-alert fixtures passed, including missing production signals
  while local or qualification traffic remains healthy.
- The final ten-minute load check passed: 8,680 synthetic operations,
  **1,852,442,240 bytes peak whole-stack footprint**, zero host swap and no OOM
  events. This includes the VM, Docker, observer and monitoring launchd helpers.
  The controlled Grafana stop/recovery occurred during this run. The exact
  measurements are in the [deployment evidence](evidence/deployment-fpl0-2026-09-09.json).

## Remaining activation and qualification

During the conversation incident, the model pause was cleared. The existing
conversation then entered `waiting_for_auth` and Theo delivered its account
eligibility error. Its account registry remains empty. Background autonomy stays
paused.

A separate synthetic diagnostic used the pinned Codex executable, selected
`gpt-6-astra` model, ChatGPT subscription login and Theo's actual macOS runner
sandbox. After checking fresh included allowance, one ephemeral turn with native
tools disabled completed in **5.276 seconds**, returning the exact requested
synthetic text. This establishes native inference on `fpl0.local`; it does not
establish a conversation through Theo or verify paid usage controls. The private
host report is `native-inference-diagnostic.json` in the production data root.

1. Deploy the automatic Codex account-check update and verify a real conversation
   through Theo. The update replaces daily manual attestation with live native
   subscription, model and allowance checks; the earlier deployment used the
   manual evidence gate.
2. Finish Healthchecks.io sign-in, configure the one-minute heartbeat with a
   two-minute grace period, connect Telegram and verify outage/recovery delivery.
3. Send the requested final greeting after these activation checks succeed.
4. Keep background autonomy paused until the native, behavioural, deterministic,
   capacity/restore and genuine seven-day service gates pass. The brief telemetry
   load test does not satisfy the seven-day gate.

FileVault and routine backups remain deferred by the owner. Mandatory local
release snapshots are retained with encryption explicitly marked unverified.
No off-machine recovery or backup RPO is claimed. See the
[operational runbook](deployment-fpl0.md) for the installed layout and upgrade order.
