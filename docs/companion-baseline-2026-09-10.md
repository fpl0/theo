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

The saved private memories and persona are immediately usable by the existing
service. The scheduler, host policy and Telegram changes require a code release
and daemon restart. Build core, worker and supervisor from the same committed
source, verify the installed wheel, test the schema upgrade on a private copy,
and take a verified pre-migration snapshot before changing production state.

Schema 8 application code cannot be advertised as a schema 9 rollback: it lacks
work schedules and uses positional schedule inserts. Preserve the old release and
snapshot, retain compatibility gates, and never automatically restore the database
or resend uncertain effects. A target activation record must separately state the
installed source, actual checks and any remaining qualification gaps.
