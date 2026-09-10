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
An installation sets `bundle_root` to separate controller-owned storage, outside
both the private controller root and coding workspaces. Its group permits core
and runner reads and execution, without writes. This avoids granting traversal
into the controller's credential and journal directory to load application code.
Install the independent supervisor as root with a private state directory and an
external, root-owned `selection` pointer. It drops to `core_uid`/`core_gid` before
executing application code. The selected bundle is supplied directly to the core;
a release projection inside writable core storage cannot choose what the
supervisor runs. Root-owned ancestors protect both the process records and the
selection. Installation may supply a root-private `telegram_token_file`; only the
channel credential and explicit application settings cross into the core process.
Generated launchd definitions name the separate controller account and root
supervisor account and use isolated Python startup.
The supervisor invokes its pinned `core_access` helper under the core UID with no
supplementary groups before opening SQLite, heartbeat, pause or lock files. The
helper accepts a bounded list of operations, not SQL or arbitrary paths. This also
keeps database sidecars writable by the core after backups and drain changes.
Controller and supervisor configuration and policy files require root-owned,
non-writable ancestors, including the lexical path before symlink resolution.
The controller has a private primary group and supplementary membership in the
core's workspace group. Before job admission, it explicitly assigns prepared
trees to that group: coding trees permit group writes, while review trees permit
only reads and traversal. This includes the complete development environment and
Git metadata. A durable preparation receipt prevents repeating the handoff while
a job is active. Tree validation rejects foreign owners, external links, hard
links and special files before changing permissions.

`ControllerConfig.vm` selects the disposable Mac verification driver. Its protected
configuration pins the complete base image, Tart executable, standalone Python,
UV and host-only guest agent. The driver hashes these inputs before cloning, copies
the accepted source and locked wheels over the guest stream, and runs the configured
checks plus fixed distribution and installed-wheel checks. Candidate commands run
as guest UID 622. The installed-wheel canary is supplied by the controller and runs
outside the candidate checkout. See the original
[builder proof](evidence/self-maintenance-builder-2026-09-10.json) and subsequent
[driver integration evidence](evidence/self-maintenance-vm-integration-2026-09-10.json).
The subsequent [bundle and development proof](evidence/self-maintenance-bundles-2026-09-10.json)
records the successful combined component pipeline and the failures that preceded it.

An installation also pins `minimum_source` and `minimum_source_sha256` to a
root-owned source tree outside writable service storage. The verifier combines
the candidate implementation with that baseline's tests, check configuration and
tooling lock. In the guest it installs and seals a separate minimum environment
before candidate packaging hooks run, then executes the pinned Ruff, format,
Pyright and pytest checks. Candidate checks and installed-package checks follow
in their own environment. Both locks' wheels are staged with their recorded hashes.
The receipt binds the baseline and combined tree; packaging requires that receipt.
Deleting a candidate test or relaxing its config therefore cannot remove the
baseline check. Baseline changes are protected control-layer changes and need
review against the existing recipe; they are not a model tool argument. The new
minimum-gate path still requires its target-VM acceptance evidence.

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

The packaging path builds complete standalone Python prefixes for both core and
worker, including standard and dynamic libraries. It installs locked dependencies
and the candidate wheel in the guest, rewrites console scripts for relocation,
and moves the prefixes away from their original build path.
The independently rebuilt wheel must match the verified candidate's wheel hash.
After stopping build processes, the root guest helper freezes the source and
outputs beneath a protected parent and rechecks the complete source identity.
Fixed permission canaries require both prefixes to reject existing-file and
directory writes. Functional checks then exercise those sealed copies, so the
archive and the tested runtimes have the same immutable bytes.

The controller receives a sealed archive through small, hash-checked responses on
one interactive connection. Every offset, source identity, archive size and chunk
hash must match; acceptance also requires the final archive hash and a successful
transport exit. Extraction rejects links outside the bundle, path aliases,
special files and excessive tar metadata before publishing any bundle. Both
runtime inventories and the dependency lock must agree. The controller adds
separately configured native inputs and records the whole bundle manifest before
an atomic rename on the destination filesystem.

`native_files` and `native_executables` bind the bundle's native programs. A Codex
selection requires its adjacent `codex-code-mode-host`. The core resolves these
programs from the verified bundle; missing programs wait for repair. Native asset
hashes participate in the subscription eligibility fingerprint. `runtime_extras`
selects the Python extras included in a deployment prefix; downloading browser or
model assets remains a separate installation operation.

Coding workspace preparation uses the same guest build and sealed transfer, with
all locked development groups and Python extras. A literal source path replaces
the installed project for editable imports, and the job's shared workspace group
receives a writable environment. Repair patch evidence is preserved. This path
does not execute an installation hook on the controller or accept the development
environment as a deployment bundle.

Generated commands use writable home, temporary and cache directories inside
their job workspace. Directory creation happens after sandbox entry, so a
candidate-created scratch symlink cannot redirect core writes. Executable lookup
includes the job's development environment. Unix sockets within that workspace
support offline broker and protocol tests; external Unix endpoints, IPv4 and IPv6
remain denied. Native account files stay outside command access. Use a short
workspace root at installation because macOS limits Unix socket path length.
An operator-provisioned `worker_home/workspaces` symlink can point at a shorter
workspace root. The sandbox resolves that root and grants writes only to the
issued job alongside the native home; other job directories remain denied.

This remains an incomplete deployment installation. Minimum-gate preservation,
all retained staging/cache budgets, final service wiring and the live
acceptance campaign below still need final integration and qualification.
The [service identity evidence](evidence/self-maintenance-identities-2026-09-10.json)
records physical Mac checks of the protected supervisor, core-only database
helper, controller-private files, full development environment group handoff and
read-only review tree. These use synthetic state. The host reports FileVault off;
production encrypted-storage qualification remains outstanding.

Remaining acceptance work includes final bundle qualification, bounded storage, old/new schema compatibility
canaries, independent health and alert coverage, controller handover, GitHub App
provisioning, and live activation/probation/rollback. Local tests use real SQLite,
Git and Unix sockets, with synthetic GitHub and model outcomes where stated;
they do not establish these live gates. Proactive self-deployment stays disabled
until its end-to-end proof and standing policy are recorded.
