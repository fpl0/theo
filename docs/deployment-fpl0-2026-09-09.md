# fpl0.local deployment — 9 September 2026

Theo is answering Telegram conversations on `fpl0.local`. The original blocked
conversation and a fresh greeting both completed through native Codex, with actual
Telegram receipts and visible replies. Codex now checks its account automatically;
daily manual attestation is no longer required. Healthchecks.io sign-in remains a
separate monitoring gap, and full background-autonomy qualification remains open.

## Installed state

- Active release: `20260909-ffdb217`, source
  `ffdb2176d87148f325b1b7bce0a3ae4677dac857`.
- Previous compatible releases retained: `20260909-df9885c`,
  `20260909-7cc0327` and `20260909-2e44471`.
- Apple Silicon, macOS 15.7.9, 16 GiB RAM; sleep disabled and power-loss restart enabled.
- Native Codex 0.153.4 pinned separately; selected model `gpt-6-astra`.
  Native account inspection reported ChatGPT Pro. Each conversation verifies the
  live account, model catalogue and included allowance before inference.
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

## Original deployment validation scope

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

## Conversation recovery

Initially, clearing the model pause exposed the missing manual account record:
the conversation entered `waiting_for_auth`. The error message reached Telegram,
but a network failure left its receipt uncertain. An owner reply referencing that
exact bot message supplied the actual Telegram message identity, destination and
content; the operator reconciled that delivery before retrying the preserved job.

A separate synthetic diagnostic used the pinned Codex executable, selected
`gpt-6-astra` model, ChatGPT subscription login and Theo's actual macOS runner
sandbox. After checking fresh included allowance, one ephemeral turn with native
tools disabled completed in **5.276 seconds**, returning the exact requested
synthetic text. This establishes native inference on `fpl0.local`; it does not
establish a conversation through Theo or verify paid usage controls. The private
host report is `native-inference-diagnostic.json` in the production data root.

Release `20260909-df9885c` replaced Codex's manual account gate with live checks.
The core and worker passed the installed-package smoke check outside the source
checkout: 86 modules and five migrations each. The application switched after
draining, pausing supervision and taking its mandatory pre-switch snapshot. The
worker and supervisor environments were synchronized, and all three launchd
services were reloaded.

The original conversation completed, and a fresh Telegram greeting request also
completed through `gpt-6-astra`. Both replies were visibly received in Telegram.
There were no queued, running, auth-wait or quota-wait jobs and no uncertain
actions at the recovery check. Grafana was reachable from the laptop over the LAN;
production heartbeat and polling were current, and all Grafana alerts were normal.

The update passed **327 offline tests**, with the Linux-only dedicated-UID check
skipped on macOS. Ruff lint/format, strict Pyright and the distribution build
passed. Regression coverage includes fresh renewal after three days, unknown or
paid allowance rejection, identity changes, and a native process stopped when a
mid-turn quota update arrives. See the
[activation evidence](evidence/activation-fpl0-2026-09-09.json).

## Queue-status and reply update

Release `20260909-ffdb217` adds the scoped `get_status` tool, excludes the reporting
job from queue counts, and keeps ordinary private replies free of automatic quote
references. The committed source passed **352 offline tests**, with one Linux-only
check skipped on macOS, plus Ruff lint/format, strict Pyright and the distribution
build. Installed core, worker and supervisor environments each passed the
outside-checkout smoke check: 86 modules and five migrations.

The deployment drained work, paused the core, verified release hashes and schema,
took the mandatory pre-switch snapshot, and retained `20260909-df9885c` for code
rollback. All three launchd services were reloaded. The resumed core produced a
fresh heartbeat in **15.05 seconds**; SQLite integrity and foreign keys passed.

The first live canary delivered a reply but could not run the status tool. A
synthetic diagnostic isolated the cause: the pinned Codex executable lacked its
`codex-code-mode-host` companion. MCP listing worked, but tool execution could not
start. The matching helper was copied from the same 0.153.4 distribution after
verifying both executable checksums. The deployment evidence records this failed
tool canary.

After that repair, a real owner conversation called `get_status` through the native
Codex worker and Theo broker. Its committed result reported zero unfinished jobs
other than the excluded reporting job, and its completed reply had an actual
Telegram receipt. The native Telegram client showed the correct queue answer and
an ordinary private reply without a quote. `/status` separately reported
`queued: 0` while another real conversation was running. Further owner activity
continued during validation; these are timestamped observations, not a promise
that the queue remains empty.

Grafana remained reachable over the LAN. At the final monitoring check, all alerts
were normal, core and Telegram polling signals were fresh, uncertain actions were
zero, and the measured whole-stack footprint was **1,861,879,104 bytes** with no
host swap. This is a current sample, not a new load or soak qualification. See the
[redeployment evidence](evidence/redeployment-fpl0-ffdb217.json).

## Remaining qualification

1. Finish Healthchecks.io sign-in, configure the one-minute heartbeat with a
   two-minute grace period, connect Telegram and verify outage/recovery delivery.
2. Keep background autonomy paused until the native, behavioural, deterministic,
   capacity/restore and genuine seven-day service gates pass. The brief telemetry
   load test does not satisfy the seven-day gate.

FileVault and routine backups remain deferred by the owner. Mandatory local
release snapshots are retained with encryption explicitly marked unverified.
No off-machine recovery or backup RPO is claimed. See the
[operational runbook](deployment-fpl0.md) for the installed layout and upgrade order.
