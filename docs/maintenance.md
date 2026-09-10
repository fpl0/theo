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

`ControllerConfig.vm` selects the disposable Mac verification driver. Its protected
configuration pins the complete base image, Tart executable, standalone Python,
UV and host-only guest agent. The driver hashes these inputs before cloning, copies
the accepted source and locked wheels over the guest stream, and runs the configured
checks plus fixed distribution and installed-wheel checks. Candidate commands run
as guest UID 622. The installed-wheel canary is supplied by the controller and runs
outside the candidate checkout. See the original
[builder proof](evidence/self-maintenance-builder-2026-09-10.json) and subsequent
[driver integration evidence](evidence/self-maintenance-vm-integration-2026-09-10.json).

The proof VM has no host directory shares, clipboard, audio or routed network.
`packet_sink.py` discards its virtual Ethernet traffic without elevated host
permissions. A pinned guest-agent patch admits only the hypervisor host's peer
identity: the upstream administrative endpoint also accepted guest-origin
connections and cannot be used unchanged for this boundary. Build that guest
component with `scripts/build_vm_agent.py`; its manifest records the upstream
commit, patch, compiler and binary hashes. Building it alone does not qualify it.

Before candidate admission, the root guest bootstrap disables the image's published
login accounts and requires the directory service's disabled-account authentication
result. It also verifies that the build identity cannot use sudo or authenticate
with the published administrator credentials. The root-owned `vm_guest.py` runner
stops that identity's detached descendants and limits time and output.
Completed operations retain exact receipts and private logs; changed requests
cannot reuse an operation ID, and interrupted uncommitted results remain uncertain.
Read logs in bounded chunks and verify their hashes. Direct large command responses
failed during the proof. The independent watchdog bounds VM lifetime, host memory
growth, driver output and free disk space. It signals the owned Tart process;
unrelated virtualization services are observed for accounting, never killed.
An exclusive installation lock and process birth records fence recovery. Controller
startup reconciles those records before resuming its journal. Successful checks
stop and delete their VM; failed admitted candidates retain stopped images and
private logs. Virtual disk capacity is bounded before another VM is admitted.

This is verification integration, not a completed deployment installation. The VM
path currently refuses release packaging until a relocatable standalone runtime is
bundled and canaried on the physical host. Development workspace preparation,
minimum-gate preservation, all retained staging/cache budgets, and separate host
service identities still need final integration and qualification.

Remaining acceptance work includes relocatable packaging, bounded storage, old/new schema compatibility
canaries, independent health and alert coverage, controller handover, GitHub App
provisioning, and live activation/probation/rollback. Local tests use real SQLite,
Git and Unix sockets, with synthetic GitHub and model outcomes where stated;
they do not establish these live gates. Proactive self-deployment stays disabled
until its end-to-end proof and standing policy are recorded.
