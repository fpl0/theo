# Theo dashboard design review — 9 September 2026

The six native Grafana dashboards now put operational decisions first: current health, work that needs attention, response timing, and supporting events. The design keeps Grafana's dark theme, reduces oversized headings and statistics, and uses consistent outcome colors with text labels. Missing data is explicitly distinguished from zero.

## Reviewed flow

| Step | Screen | General health and observed behavior |
| --- | --- | --- |
| 1 | Overview | Good. Active alerts and current work inventory appear immediately below compact status cards. The current offline core and authentication blocks remain visible. |
| 2 | Jobs & Tools | Good. Queued, running, blocked and approval counts have distinct labels. The blocked-work link opens Codex diagnostics and retains the selected time and traffic filters. |
| 3 | Telegram | Repaired and verified. Poll age remains visible after the process stops; processing and send latency exclude normal long polling. The stale-poller alert now survives missing samples. |
| 4 | CLI | Repaired and verified. Replaced an unsupported Loki duration macro with a five-minute window. Fixture charts show connect 1.12 ms and submit 8.21 ms without panel warnings; these are test observations, not live performance claims. |
| 5 | Codex | Good. Authentication, run outcomes, attempt duration and reported usage have separate panels. Unknown usage stays Awaiting run. Authentication-blocked attempts are not shown as successful AI runs. |
| 6 | Infrastructure | Good. Whole observability memory, remaining headroom, restarts and the last load-test result are distinct. The graph marks the decimal 2 GB ceiling. Native Theo/Codex memory is shown separately. |

Navigation preserves the Traffic selection (Live Theo or Test fixtures) and time range. The default window is 24 hours. Every panel has a description, every dashboard has a reading guide, and recent events remain available at the bottom with trace links. Current local alerts are explicitly labelled as local and do not change with fixture filters.

## Validation

- 86 Prometheus and 10 Loki dashboard queries passed; all 18 alert expressions evaluated.
- Five Prometheus behavioral tests passed: healthy, stopped, configured but never observed, unconfigured, and fixture traffic unable to hide a missing live poller.
- The OpenTelemetry canary correlated its trace, logs and metric exemplar, including Grafana's trace-to-logs query.
- All 11 telemetry tests passed. Ruff and Pyright passed.
- Final measured whole observability memory: 1,817,777,984 bytes (approximately 1.82 GB). This was measured during the review; the earlier ten-minute load qualification remains separately recorded.

## Screenshots and limits

Screenshots were captured and visually inspected in Grafana during this review. Desktop captures use the original 1280 × 720 audit viewport; the temporary viewport was reset before handoff. The normal narrower browser view was also observed and uses Grafana's stacked panel layout. The review did not establish mobile usability or full screen-reader/WCAG compliance. Keyboard focus was visible; complete keyboard navigation was not exhaustively tested. Statuses also use text, so color is not the only signal.

Screenshots were saved locally under `.local/observability/dashboard-design/` in the reviewing checkout. They are not bundled repository evidence, so other checkouts will not have these files:

| View | Local capture filename |
| --- | --- |
| Overview | `after-overview.png` |
| Jobs & Tools | `after-jobs-tools.png` |
| Telegram | `after-telegram.png` |
| CLI live / fixtures | `after-cli.png`, `after-cli-fixtures.png` |
| Codex | `after-codex.png` |
| Infrastructure | `after-infrastructure.png` |

Before captures are retained in the same folder as `before-overview.png` and `before-cli.png`. The final screenshots are live observations taken at different times, not identical-data comparisons.
