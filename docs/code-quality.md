# Code quality and free CI

The current workflow checks source quality, deterministic behavior and installed distributions. Live native and Telegram evaluations are separate, opt-in activities; CI receives no model or Telegram credentials.

During the 9 September documentation refresh, the current macOS checkout passed **238 offline tests, with one expected Linux root-only skip** in 66.50 seconds. Ruff lint and formatting passed across 131 files; strict Pyright reported zero errors or warnings. This result includes the source reorganization and later Telegram/telemetry tests; it does not retroactively extend the earlier native evaluation's scope.

The [8 September review](review-2026-09-08.md) records fifteen fixes across delivery, recovery, memory, scheduling and native execution, **156 passing offline tests on its frozen build**, and local Codex/Claude adapter E2E evidence. The [complex evaluation](complex-evaluation-2026-09-09.md) records 40 passing native turns and four host-state checks with separate answer-quality review. See [acceptance status](acceptance.md) for the remaining production gates.

## Historical fixes — 7 September 2026

This earlier review focused on storage, transport/resource lifetimes, daemon startup, dependency declarations and CI. It is not a claim that every module has undergone an exhaustive security audit.

| Finding | Change and verification |
| --- | --- |
| Unescaped SQLite URI paths misinterpret `?`, `#` and `%` in owner directories | Use `Path.as_uri()` for read connections in storage, embeddings, imports and backups. A regression creates, reads, backs up and verifies state under a directory containing those characters. |
| RPC requests leaked pending futures if sending failed or was cancelled | Register/send/wait/cleanup share one protected scope. Tests cover broken pipes and cancellation during send. The timeout now covers send backpressure too, with a dedicated regression. |
| SQLite connection context managers commit/rollback but do not close | Explicitly close the live E2E observer's connection after every poll. Backup connections also close if opening the destination fails. Close the local model hash file deterministically. |
| Supervisor startup failed in existing macOS CI | The prior run timed out before the first core heartbeat. Its long macOS test path exceeds the portable Unix socket limit. Host-only broker sockets now use a private, short `/tmp` directory; configured worker sockets stay inside their configured home. Oversized worker paths fail clearly. Startup failure releases the socket and daemon lock. Existing supervisor integration remains enabled on macOS. |
| `aiohttp` was imported directly but installed only transitively | Declare it as a direct dependency. Existing locked package versions remain unchanged. |
| Mutable action tags and unpinned uv changed the build environment implicitly | Pin verified official action commit SHAs and uv; use `uv sync --locked` to reject stale locks. Dependabot proposes weekly updates for Actions and uv dependencies. |

## Enforced checks

`.github/workflows/ci.yml` runs on pushes to main, pull requests and manual dispatch:

- Ruff lint and formatting across source, tests and scripts.
- Strict Pyright checking of production source.
- Offline pytest with strict marker/config validation, including durability and protocol subprocess fixtures.
- Wheel/source distribution builds, then an independent installed-wheel import and bundled-migration check outside the checkout.
- Both `ubuntu-24.04` and `macos-14`, with a 15-minute limit and cancellation of superseded runs.

The workflow token has read-only repository contents access and is not persisted in checkout configuration. It uses ordinary `pull_request`, not privileged `pull_request_target`. Live Telegram and model tests remain opt-in and receive no CI credentials. Optional browser/embedding Python dependencies are installed for type checking; no browser binaries or model weights are downloaded by the suite.

## Why this CI is free

Theo is a public repository. GitHub documents [free standard hosted runners for public repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions). This workflow uses those standard Linux/macOS runners, disables persistent Actions caching, and uploads no build or test artifacts. Results stay in job logs. It does not use larger runners, paid external analysis services or metered model calls.

The job condition requires `github.event.repository.private == false`. If visibility changes, the job skips instead of consuming a private repository's allowance; revisit CI before making that change. This workflow does not control billing for unrelated workflows, services or future configuration edits. No billing settings were changed.

## Validation and remaining work

The 7 September verification after those fixes reported: **95 tests passed, 2 host-restriction skips**, zero Pyright errors/warnings, clean Ruff lint/format checks, and successful wheel/source distribution builds. Its skips concerned UID transitions and Unix sockets on that test host. Later local Mac checks run the socket/supervisor cases. See the [Actions page](https://github.com/fpl0/theo/actions/workflows/ci.yml) for the actual hosted result for each commit.

The source is now organized into responsibility-based packages described in [the architecture guide](architecture.md#source-organization). Tool schemas, explicit effect policies, authorization and capability handlers are separate; daemon composition, native protocols, terminal interactions, Telegram delivery/media and operator data operations have dedicated modules. An immutable typed invocation and tool definition connect the broker to handlers. Internal `Json`/`Any` values remain where payloads are still dynamic.

Architecture tests guard import cycles and dependency direction, verify that CLI syntax imports do not load execution services, and compare the published tool schemas with the catalog. Ruff requires production module docstrings. The installed-wheel smoke check imports every production module outside the checkout, checks console/process entry points, and applies the bundled migrations to a temporary database.

Model quality, real subscription integration and deployment isolation remain separate live acceptance gates. CI success does not establish production qualification.
