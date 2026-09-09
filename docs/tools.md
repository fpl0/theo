# Tool catalogue

All handlers pass through owner/run/generation grants in `tools/broker.py` and lease/privacy checks in `tools/authorization.py`. `tools/registry.py` binds each strict schema to a capability handler and an explicit read/write/outbound receipt policy. Implementations live in `tools/handlers/`; they receive an authorized invocation rather than the broker.

The current catalogue contains **51 tools: 33 baseline tools and 18 additions**. Schema JSON is generated from the Pydantic catalog in [tool-schemas.json](tool-schemas.json). Run `uv run python scripts/export_tool_schemas.py` after intentionally changing a tool contract; the architecture tests detect stale documentation. All 33 baseline handlers are exercised by the contract fixture; live channel/provider operation remains a separate gate.

| Tool | Baseline | Behaviour |
|---|---|---|
| `send_message` | Yes | Queue an owner message; committed means queued, never sent. |
| `reply` | Yes | Queue a reply retaining its message reference. |
| `forward` | Yes | Forward an existing Telegram message through the action ledger. |
| `edit_message` | Yes | Edit an exact Telegram message. |
| `delete_message` | Yes | Request reviewed deletion of a Telegram message. |
| `pin` | Yes | Pin an existing Telegram message. |
| `send_photo` | Yes | Send a registered, validated photo. |
| `send_document` | Yes | Deliver a registered artifact. |
| `send_voice` | Yes | Deliver an existing local voice artifact. |
| `send_video` | Yes | Deliver a registered video. |
| `send_audio` | Additional | Deliver a registered audio artifact. |
| `send_animation` | Additional | Deliver a registered animation. |
| `send_sticker` | Additional | Deliver a registered sticker. |
| `send_video_note` | Additional | Deliver a registered round video note. |
| `send_media_group` | Additional | Deliver an ordered album of registered artifacts. |
| `send_contact` | Additional | Deliver contact details. |
| `send_venue` | Additional | Deliver a venue and its location. |
| `send_location` | Yes | Deliver geographic coordinates. |
| `send_poll` | Yes | Create a Telegram poll. |
| `send_buttons` | Yes | Send URL buttons; approval callbacks are host-owned. |
| `react` | Yes | React to a specific message. |
| `get_reactions` | Yes | Read reactions observed by the bot; absence is unknown. |
| `schedule_task` | Yes | Persist a reminder before promising it. |
| `list_tasks` | Yes | List persisted schedules. |
| `delete_task` | Yes | Cancel a schedule without deleting its history. |
| `remember` | Yes | Save an inference or propose a reviewed correction; no silent overwrite. |
| `recall` | Yes | Search current active SQLite memory. |
| `forget` | Yes | Archive a memory with recoverable history. |
| `recall_conversation` | Yes | Read canonical messages in this conversation. |
| `connect` | Yes | Link memories with typed evidence. |
| `restore` | Yes | Restore an archived memory or prior revision. |
| `bulk_memory` | Yes | Store a bounded batch with individual results. |
| `memory_history` | Yes | Read complete immutable revisions. |
| `review_corrections` | Yes | List correction proposals for owner review; the model cannot approve. |
| `pin_attention` | Yes | Persist a contextual attention pin. |
| `unpin_attention` | Yes | Remove a contextual attention pin. |
| `get_cost_report` | Yes | Inspect nullable token usage and included allowance pool state. |
| `log_deep_work_quality` | Yes | Record a subjective rating alongside host-observed run outcomes. |
| `browse` | Yes | Read a public web source as untrusted evidence. |
| `delegate` | Yes | Create a durable child job with a final-report obligation. |
| `goal_create` | Additional | Create a structured outcome and executable plan. |
| `goal_update` | Additional | Transition a goal with evidence and dependency checks. |
| `step_complete` | Additional | Complete one plan step with outcome evidence. |
| `fact_propose` | Additional | Propose a fact revision for explicit owner review. |
| `artifact_register` | Additional | Validate and hash an actual workspace file. |
| `action_status` | Additional | Inspect committed, pending, delivered or uncertain action state. |
| `file_read` | Additional | Read a bounded text file inside this job's workspace. |
| `file_write` | Additional | Write a draft inside this job's isolated workspace. |
| `command_run` | Additional | Execute an argument array within the verified OS boundary and workspace. |
| `voice_create` | Additional | Create a voice artifact using local macOS speech and FFmpeg. |
| `skill_propose` | Additional | Propose a versioned skill without activating it or expanding grants. |

Mutating tools report committed, ready, awaiting approval, pending review, failed or uncertain outcomes; queued delivery is not a successful remote send. `get_reactions` reports only observed feedback and marks completeness false. Generated commands require the verified Mac boundary. Model-facing tools do not activate skills, review their own corrections, edit billing/isolation configuration or promote production releases.

Outbound schemas accept `destination_id` for an explicitly registered destination; legacy `target` resolves through the same routing checks. Cross-destination sends retain approval requirements. Polls default to non-anonymous so owner answers can be observed where Telegram permits them. See [Telegram](telegram.md) for visibility and review behavior.
