# Companion behavior review — 10 September 2026

This change follows an owner-requested comparison with the archived Luke assistant
and review of Theo's production conversation. It changes behavior while retaining
canonical SQLite memory, durable jobs, native subscription checks and receipted
delivery. The reviewed source starts at `4747e9e`; private source material and raw
conversation snapshots are deliberately outside the repository.

## Evidence and diagnosis

The review covered all 45 owner messages and 38 assistant messages available in
Theo at the snapshot, plus 85 tool records. Luke's archive was accessible on the
target host: its database contained 4,849 messages from March through September.
The behavioral review read Luke's main guidance, selected dated feedback and
procedures, and the associated owner corrections. It did not read every Luke
message or claim a complete personal-history import.

Nine sourced behavioral lessons were saved to private Theo memory: conversational
voice, personal presence, initiative, follow-through, investigating obstacles,
respect for attention, using current state, leaving finished work alone, and
representation boundaries. A new saved persona version preserves its predecessor.
All nine lessons were verified in canonical context for a plain greeting. The
owner subsequently reiterated standing authority to operate the whole computer;
that current instruction supersedes older per-command confirmation preferences.

The observed failures were concrete: workspace restrictions were mistaken for
host-wide inability; approval requests were repeated; a goal remained blocked
after successful access; a work instruction was scheduled as verbatim reminder
text; and Telegram repeatedly replaced a draft bubble. Existing persona wording
alone did not repair these execution paths.

## Resulting behavior

- Telegram displays typing and then a stable receipted final answer. Model text
  deltas never become Telegram drafts; terminal streaming remains available.
- Recent standing preferences enter bounded context across greetings and topic
  changes, with current revision and conversation visibility checks.
- Proactive review uses recent private conversation, attention items, commitments
  and changed blockers. Unchanged evidence is deduplicated. It admits useful work
  instead of only creating proposals; its terminal notes stay internal.
- Goal execution checks for new executable steps each minute. The model can inspect
  a plan and replace a stale next action without overwriting completed steps.
- `schedule_task` explicitly distinguishes reminder text from future model work.
  Work receives fresh conversation context and delivers only an intentional result
  or meaningful development. Schema 9 preserves existing reminders and origins.
- The owner can grant full host authority once. In standing mode, host commands,
  including privileged ones through the installed launcher, need no repeated
  approval. Reads can cover the whole host. Commands retain durable receipts,
  deadlines, cancellation and dispatch-time revocation checks; groups cannot
  inherit private host authority.

## Validation scope

The Codex `gpt-5.6-sol` companion batch passed six native cases: grounded greeting,
committed standing preference, preference continuity in a new topic, a written and
registered artifact, stale-plan repair with actual goal updates, and scheduled work.
The last case initially calculated the wrong timezone offset, then read the tool's
local-time result and corrected the schedule before claiming completion.

Four personality cases passed on the second batch. The first batch passed three:
one response incorrectly stored an exchange-local explanation request as a lasting
preference. That failed report was preserved and the instruction corrected.

Transcript inspection supports a narrower conclusion than the mechanical pass:
the replies were concise, grounded and action-backed in this sample. Some wording
remained stylized; one astronomy artifact used imprecise orbital-period wording.
These checks do not establish a general answer-quality score, equivalence to months
of Luke interaction, or a seven-day production soak.

Offline regressions cover preference privacy/revision handling, schedule migration,
work-prompt non-delivery, changed-context admission and deduplication, stale plans,
full host authority and revocation, and typing followed by one stable final receipt.
Native tests use synthetic state and local delivery sinks. They are distinct from
target-host installation and real Telegram client observations.

## Release considerations

The saved private memories and persona became usable by the existing service
before the code cutover. The scheduler, host policy and Telegram changes were then
activated through a release and daemon restart. Core, worker and supervisor must
share the committed source; installed-wheel checks, a private migration rehearsal
and verified pre-migration snapshot were performed before activation.

Schema 8 application code cannot be advertised as a schema 9 rollback: it lacks
work schedules and uses positional schedule inserts. Preserve the old release and
snapshot, retain compatibility gates, and never automatically restore the database
or resend uncertain effects. A target activation record must separately state the
installed source, actual checks and any remaining qualification gaps.

## Follow-through correction

The first production canary successfully read the archive and executed a privileged
command without approval, but its narrowly scoped verification reply left the owner
with another unfinished-work status. The owner correctly identified the missing
plan, timeline and unsolicited next update. Access alone had not solved ownership.

The correction adds `goal_checkpoint`: one transaction saves a commitment and its
future native job. Due checkpoints take priority over queued batches, use current
goal/conversation evidence, and require a final receipted update. Replacing a
checkpoint cancels its obsolete future job without cancelling the reporting run.
Goal completion or pause closes pending checkpoints. Promised updates are exempt
from the unsolicited-message count cap, while delivery pauses and freshness checks
remain in force. Partial deep-work outcomes stay internal; a completed goal retains
its final-result obligation. Persona and shared guidance require a concise stage
breakdown, a measured completion estimate or a timed checkpoint, substantial batches,
an exact saved coverage cursor, and continued work without duplicate review schedules.

The first follow-through harness attempt exposed a fixture error: it left work
paused and provided neither sources nor an existing reviewer. That failed report
was preserved. A corrected fixture supplies the existing source worker and enables
work. Its two native turns demonstrated a saved checkpoint, fresh measured progress,
a delivered update and the next checkpoint. A lexical oracle initially rejected
“measure” as the inventory stage; it was corrected to accept that equivalent wording.
These reports remain distinct from real-time production evidence.

The final production-source snapshot passed 558 offline tests with one Linux-only
skip. Focused job, delivery, scheduling, crash and architecture checks also passed. Ruff, strict Pyright, the build and an isolated
installed-wheel check passed (128 modules, nine core/four controller migrations).
The target activation record separately identifies the installed source and
additional checks; this paragraph does not claim an unrun production soak.

The autonomy regression created a real invoice plan, executed it, checked and
registered the correct artifact, completed the goal, and delivered exactly one
useful final message. Its old harness compared that delivered message to the now
internal terminal note and reported a false delivery mismatch. The raw failure is
preserved; the harness now grades the committed final-action text and records the
terminal note separately. All underlying artifact, completion and receipt checks
passed. Delegated calculation and delivery also passed.

A later follow-through reply used a worker's allotted thirty-minute window as an
ETA basis despite having no measured pace. That transcript is preserved. Shared
guidance now explicitly separates queue deadlines from observed throughput, and
the follow-through regression rejects that substitution.

## Target activation

Release `20260910-cf5bb10` first activated the schema-9 companion changes. Its real
Codex `gpt-6-astra` canary read Luke guidance and executed a privileged command with
exit code 0 and uid 0, without an approval row. One stable Telegram final chunk had
one successful attempt and no draft previews. Existing completed work was retained
and the archive goal was reactivated.

The follow-through release is **`20260910-a168268`**, source
`a16826867fff28317f5f13ae4222c77a44bcf48b`. Core, independent worker and supervisor
were built from that clean commit with the same locked browser, embedding and
speech dependencies. All three installed environments passed the outside-checkout
smoke check. A real Mac sandbox denied protected reads/writes, and the sandboxed
worker imported `encodings`, Theo and MCP successfully. A verified snapshot preceded
the switch; `cf5bb10` remains a schema-compatible application rollback. A cutover
assertion initially compared a tuple of host paths with a list; it stopped before
the switch, was corrected, and activation completed. No schema gate was bypassed.

The final two-turn native follow-through batch passed both automated cases with
receipted local delivery and the next checkpoint committed. Transcript review still
found an unnecessary mention of an inaccessible internal skill in the first reply;
this small sample is not a claim of perfect naturalness or full Luke equivalence.

The real 23:33 Dublin checkpoint exposed the old scheduled-work notification gate:
its progress message was held with `critic_unchecked`. The exact unattempted
owner-requested message was reclassified as a promised update, then delivered once.
No ambiguous or successful chunk was resent. The already-promised **00:03 Dublin,
11 September** update was moved to a durable `goal_checkin`; its old schedule was
disabled after the replacement existed. The new path is non-discretionary, exempt
from unsolicited-message caps, and requires a receipted final before fulfillment.
The final checkpoint state and production checks are recorded separately in the
[activation evidence](evidence/companion-fpl0-2026-09-10.json).

All background, autonomy, requested-work, model, notification and deployment pauses
were cleared after cutover. The live account refreshed its own Codex eligibility.
The saved persona is version 3 and the standing host policy and `/` read grant remain
active. The personal-history archive goal continues with preserved coverage; it is
not falsely marked complete. Full archive reading and a general companion-quality
score are not claimed. No new Telegram user-client campaign, seven-day soak,
FileVault verification or machine-loss recovery qualification was performed.

The first goal batch after the final cutover completed through Codex `gpt-6-astra`,
registered two coverage/report artifacts, saved supported memories and advanced the
next action. It created **zero owner-message actions**, leaving its partial-work
note internal. This directly verifies the quieter batch behavior on the target;
the next promised checkpoint was still future-queued when evidence was captured.
