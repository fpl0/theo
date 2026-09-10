# Self-maintenance implementation

Theo's maintenance pipeline separates conversational coding jobs from a controller
that holds publication authority and a host service that owns activation and
recovery. The [design](self-maintenance-design.md) defines the full acceptance
criteria. This implementation is still undergoing deployment qualification; it is
not yet enabled on `fpl0.local`. See the [progress record](evidence/self-maintenance-progress-2026-09-10.json)
for checks and concrete remaining work.

## Requests, candidates and repairs

A private owner job uses `maintenance_begin` to commit an objective and a publish
or deploy target. The core creates separate coding and review jobs. The controller
prepares the source workspace before the coding job becomes eligible to run.
`maintenance_submit` seals the submitted revision and copies bounded source files
into controller-owned storage. Git configuration and hooks from the worker are
never imported.

Each candidate binds a base commit, source commit, tree, source digest and dependency
lock digest. Its verification and independent review must match that identity.
A failed candidate check or a rejected review creates a new candidate round with
fresh coding and review jobs. Earlier submissions, verdicts and receipts remain
available in the core and controller journals. Revoking the old jobs prevents
stale attempts from changing the new round.

If the base branch advances before merge, the controller first resolves the merge
outcome. A known unmerged candidate is integrated into a new workspace using the
previous accepted patch and the new base. Conflicts and the original patch remain
available to the coding job. The resulting revision needs fresh checks and review.
An uncertain merge blocks revision and publication until it is reconciled.

Branch updates use an exact expected previous commit. An unrelated branch change
cannot be overwritten. Required CI checks bind both the source revision and the
configured GitHub Actions workflow. Strict repository rules must be present before
an automatic merge is attempted.

The two-revision limit applies to proactive maintenance. Owner-requested repairs
retain their overall task deadline. Neither path bypasses model eligibility,
operating pauses, current standing policy, or publication uncertainty.

## Host commands and operating controls

The private `host_command` tool can request commands anywhere on the host. Fixed
system diagnostics have standing permission; other commands require a durable
approval for the exact arguments, directory, timeout and execution identity.
Privileged execution additionally requires the separately installed root launcher.
See [host access setup](operations.md#host-access-and-confirmations) and the
[tool catalogue](tools.md). Configuring a path does not prove that privileged
execution is installed or isolated correctly.

Model, autonomy, requested-work, deployment and notification pauses are independent.
Activation uses a maintenance-owned drain flag and preserves owner pauses.
A model pause parks new activation inference. Recovery of a previously verified
bundle uses deterministic process, heartbeat and database checks and does not
create an inference job. Recovery never restores an older database snapshot.

## Deployment status and remaining gates

The controller, builder and host service must have the documented OS ownership
boundaries. The GitHub App key must remain controller-private. Native runners and
candidate commands must not acquire controller, core or root-launcher authority.

Remaining acceptance work includes complete isolated recipe execution on macOS,
descendant-process containment, bounded storage, old/new schema compatibility
canaries, independent health and alert coverage, controller handover, GitHub App
provisioning, and live activation/probation/rollback. Local tests use real SQLite,
Git and Unix sockets, with synthetic GitHub and model outcomes where stated;
they do not establish these live gates. Proactive self-deployment stays disabled
until its end-to-end proof and standing policy are recorded.
