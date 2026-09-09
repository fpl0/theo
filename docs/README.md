# Theo documentation

The [project README](../README.md) introduces Theo and the local setup. These guides describe the current implementation; dated reports retain the scope and limitations of the build they tested.

## Use and operate Theo

| Guide | Read it for |
| --- | --- |
| [Terminal](terminal.md) | Interactive chat, named sessions, attachments, drafts and cancellation. |
| [Telegram](telegram.md) | Bot pairing, private token handling, groups/topics, controls, media and delivery recovery. |
| [Operations](operations.md) | Configuration, native accounts, isolation, assets, backups, releases and qualification. |
| [Observability](observability.md) | Local Grafana setup, telemetry, alerts, correlation and the 2 GB stack budget. |
| [Tool catalogue](tools.md) | All model-facing tools and the generated input schemas. |

## Develop and verify

| Reference | Read it for |
| --- | --- |
| [Architecture](architecture.md) | Package responsibilities, dependency rules, SQLite authority and execution boundaries. |
| [Code quality](code-quality.md) | Current CI checks and dated code-review results. |
| [Live testing](live-testing.md) | Opt-in native, Telegram, behavior and local-model reproduction commands. |
| [Acceptance status](acceptance.md) | Implemented capabilities, verified scope, remaining qualification and product limits. |
| [Requirement matrix](requirements.md) | Original ADR goals and their implementation/test evidence. |
| [Telegram implementation status](telegram-implementation.md) | Recorded client checks and remaining Telegram qualification. |
| [Evidence index](evidence/README.md) | Raw reports, source snapshots, failed attempts and terminal captures. |

## Dated design and evaluation records

- [8 September code review](review-2026-09-08.md) and [9 September complex evaluation](complex-evaluation-2026-09-09.md).
- [Telegram implementation plan](telegram-integration-plan.md), retained as the original design and completion criteria.
- [9 September dashboard review](evidence/dashboard-design-2026-09-09.md).
- [7 September compatibility snapshot](compatibility.json), [capacity measurements](capacity-results.json) and [release verification](release-verification.json).

For current installed dependencies use `uv.lock`; for current runtime eligibility and data-root state use `theo doctor --json`, `theo accounts list` and `theo qualification status`. Historical reports do not establish the state of a running installation.
