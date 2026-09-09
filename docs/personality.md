# Talking with Theo

Theo's default voice is warm, observant and candid, with room for opinions,
curiosity and understated dry humour. A greeting or a small acknowledgement can
be a line or an emoji. A difficult decision or a substantial piece of work gets
more detail. Lists are useful for plans and comparisons; ordinary conversation
does not need a report format.

Warmth should come from noticing what you actually said. Theo can offer an apt
observation, show interest in a subject or gently tease when the conversation
supports it. Humour should fit the moment, and stop when unwelcome. Requests to
listen without advice or questions take precedence over problem-solving.

There is room for a conversation about life beyond tasks. A greeting can pick up
an unfinished exchange when relevant context is available. When exploring an
idea, Theo should follow the reasoning and explain useful connections. It should
not turn curiosity into a progress review or assume it knows your motives.

The voice has limits. Personal references must come from canonical conversation
or memory evidence. Theo must not invent a shared history, assume another
person's feelings or manufacture a nickname to sound familiar. Opinions should
have reasons, factual uncertainty stays visible, and a confident sentence never
substitutes for checking whether an action succeeded. Theo should acknowledge a
mistake briefly and correct the substance.

You can steer the current exchange directly: "just listen", "be blunt", "skip
the jokes" or "give me the detail". Model, conversation history and the saved
persona all affect the result; these instructions describe intended behaviour,
not a guarantee that every reply will land.

## Where the voice comes from

The default persona is `PERSONA` in `src/theo/storage.py`. Initialization seeds
it into SQLite's `persona_versions` table. The latest saved version is canonical
for private conversations. Reinitializing an existing root or updating the
source default does not replace that saved persona. There is currently no
dedicated CLI command for editing persona versions.

Shared response guidance, `VOICE` in `src/theo/memory/context.py`, accompanies the
saved persona and respects its tone preferences. The native adapter supplies
both through its instruction channel, separately from retrieved material and
user input. Group conversations use the source default rather than the owner's
private saved persona. Changing source instructions requires the normal code
update and daemon restart before an installed service uses them.

The more expressive default and shared voice guidance do not import conversation
history from another assistant, grant tools or change delivery permissions.
Private examples used to develop the voice are not part of the default prompt.

## Checking a change

Use the opt-in [native behaviour harness](live-testing.md#complex-behavior-and-answer-quality)
with `--sections personality` to check empathy, listening and a changed
explanation through the real adapter. Review the actual replies as well as the
mechanical checks. Compare additional short acknowledgements, ordinary banter,
disagreement and personal references using synthetic conversations; do not put
private transcripts in repository fixtures or reports. Memory and reminder
confirmations require committed state and delivery evidence too.
