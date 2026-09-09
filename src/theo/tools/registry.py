"""Explicit catalog of the capabilities advertised to native workers.

Each entry binds its wire schema to a handler and read/write/outbound receipt
policy. Adding or reordering tools cannot silently change another tool policy.
"""

from functools import partial

from theo.tools import schemas
from theo.tools.contracts import ToolDefinition
from theo.tools.handlers import feedback, memory, outbound, work, workspace

REGISTRY: dict[str, ToolDefinition] = {
    "send_message": ToolDefinition(
        schemas.MessageArgs,
        "Queue an owner message; committed means queued, never sent.",
        partial(outbound.send, operation="send_message"),
        "outbound",
    ),
    "reply": ToolDefinition(
        schemas.MessageArgs,
        "Queue a reply retaining its message reference.",
        partial(outbound.send, operation="reply"),
        "outbound",
    ),
    "forward": ToolDefinition(
        schemas.ForwardArgs,
        "Forward an existing Telegram message through the action ledger.",
        partial(outbound.send, operation="forward"),
        "outbound",
    ),
    "edit_message": ToolDefinition(
        schemas.EditArgs,
        "Edit an exact Telegram message.",
        partial(outbound.send, operation="edit_message"),
        "outbound",
    ),
    "delete_message": ToolDefinition(
        schemas.MessageIdArgs,
        "Request reviewed deletion of a Telegram message.",
        partial(outbound.send, operation="delete_message"),
        "outbound",
    ),
    "pin": ToolDefinition(
        schemas.MessageIdArgs,
        "Pin an existing Telegram message.",
        partial(outbound.send, operation="pin"),
        "outbound",
    ),
    "send_photo": ToolDefinition(
        schemas.MediaArgs,
        "Send a registered, validated photo.",
        partial(outbound.send, operation="send_photo"),
        "outbound",
    ),
    "send_document": ToolDefinition(
        schemas.MediaArgs,
        "Deliver a registered artifact.",
        partial(outbound.send, operation="send_document"),
        "outbound",
    ),
    "send_voice": ToolDefinition(
        schemas.MediaArgs,
        "Deliver an existing local voice artifact.",
        partial(outbound.send, operation="send_voice"),
        "outbound",
    ),
    "send_video": ToolDefinition(
        schemas.MediaArgs,
        "Deliver a registered video.",
        partial(outbound.send, operation="send_video"),
        "outbound",
    ),
    "send_location": ToolDefinition(
        schemas.LocationArgs,
        "Deliver geographic coordinates.",
        partial(outbound.send, operation="send_location"),
        "outbound",
    ),
    "send_poll": ToolDefinition(
        schemas.PollArgs,
        "Create a Telegram poll.",
        partial(outbound.send, operation="send_poll"),
        "outbound",
    ),
    "send_buttons": ToolDefinition(
        schemas.ButtonsArgs,
        "Send URL buttons; approval callbacks are host-owned.",
        partial(outbound.send, operation="send_buttons"),
        "outbound",
    ),
    "react": ToolDefinition(
        schemas.ReactionArgs,
        "React to a specific message.",
        partial(outbound.send, operation="react"),
        "outbound",
    ),
    "get_reactions": ToolDefinition(
        schemas.MessageIdArgs,
        "Read reactions observed by the bot; absence is unknown.",
        outbound.get_reactions,
        "read",
    ),
    "schedule_task": ToolDefinition(
        schemas.ScheduleArgs, "Persist a reminder before promising it.", work.schedule_task, "write"
    ),
    "list_tasks": ToolDefinition(
        schemas.Empty, "List persisted schedules.", work.list_tasks, "read"
    ),
    "delete_task": ToolDefinition(
        schemas.IdArgs, "Cancel a schedule without deleting its history.", work.delete_task, "write"
    ),
    "remember": ToolDefinition(
        schemas.RememberArgs,
        "Save an inference or propose a reviewed correction; no silent overwrite.",
        memory.remember,
        "write",
    ),
    "recall": ToolDefinition(
        schemas.RecallArgs, "Search current active SQLite memory.", memory.recall, "read"
    ),
    "forget": ToolDefinition(
        schemas.IdArgs, "Archive a memory with recoverable history.", memory.forget, "write"
    ),
    "recall_conversation": ToolDefinition(
        schemas.ConversationArgs,
        "Read canonical messages in this conversation.",
        memory.recall_conversation,
        "read",
    ),
    "connect": ToolDefinition(
        schemas.ConnectArgs, "Link memories with typed evidence.", memory.connect, "write"
    ),
    "restore": ToolDefinition(
        schemas.RestoreArgs,
        "Restore an archived memory or prior revision.",
        memory.restore,
        "write",
    ),
    "bulk_memory": ToolDefinition(
        schemas.BulkArgs,
        "Store a bounded batch with individual results.",
        memory.bulk_memory,
        "write",
    ),
    "memory_history": ToolDefinition(
        schemas.IdArgs, "Read complete immutable revisions.", memory.memory_history, "read"
    ),
    "review_corrections": ToolDefinition(
        schemas.Empty,
        "List correction proposals for owner review; the model cannot approve.",
        memory.review_corrections,
        "read",
    ),
    "pin_attention": ToolDefinition(
        schemas.AttentionArgs, "Persist a contextual attention pin.", memory.pin_attention, "write"
    ),
    "unpin_attention": ToolDefinition(
        schemas.IdArgs, "Remove a contextual attention pin.", memory.unpin_attention, "write"
    ),
    "get_cost_report": ToolDefinition(
        schemas.Empty,
        "Inspect nullable token usage and included allowance pool state.",
        feedback.get_cost_report,
        "read",
    ),
    "log_deep_work_quality": ToolDefinition(
        schemas.QualityArgs,
        "Record a subjective rating alongside host-observed run outcomes.",
        feedback.log_deep_work_quality,
        "write",
    ),
    "browse": ToolDefinition(
        schemas.BrowseArgs,
        "Read a public web source as untrusted evidence.",
        workspace.browse,
        "write",
    ),
    "delegate": ToolDefinition(
        schemas.DelegateArgs,
        "Create a durable child job with a final-report obligation.",
        work.delegate,
        "write",
    ),
    "goal_create": ToolDefinition(
        schemas.GoalArgs,
        "Create a structured outcome and executable plan.",
        work.goal_create,
        "write",
    ),
    "goal_update": ToolDefinition(
        schemas.GoalUpdateArgs,
        "Transition a goal with evidence and dependency checks.",
        work.goal_update,
        "write",
    ),
    "step_complete": ToolDefinition(
        schemas.StepArgs,
        "Complete one plan step with outcome evidence.",
        work.step_complete,
        "write",
    ),
    "fact_propose": ToolDefinition(
        schemas.FactArgs,
        "Propose a fact revision for explicit owner review.",
        memory.fact_propose,
        "write",
    ),
    "artifact_register": ToolDefinition(
        schemas.ArtifactArgs,
        "Validate and hash an actual workspace file.",
        workspace.artifact_register,
        "write",
    ),
    "action_status": ToolDefinition(
        schemas.IdArgs,
        "Inspect committed, pending, delivered or uncertain action state.",
        outbound.action_status,
        "read",
    ),
    "file_read": ToolDefinition(
        schemas.FileReadArgs,
        "Read a bounded text file inside this job's workspace.",
        workspace.file_read,
        "read",
    ),
    "file_write": ToolDefinition(
        schemas.FileWriteArgs,
        "Write a draft inside this job's isolated workspace.",
        workspace.file_write,
        "write",
    ),
    "command_run": ToolDefinition(
        schemas.CommandArgs,
        "Execute an argument array within the verified OS boundary and workspace.",
        workspace.command_run,
        "write",
    ),
    "voice_create": ToolDefinition(
        schemas.VoiceArgs,
        "Create a voice artifact using local macOS speech and FFmpeg.",
        workspace.voice_create,
        "write",
    ),
    "skill_propose": ToolDefinition(
        schemas.SkillArgs,
        "Propose a versioned skill without activating it or expanding grants.",
        feedback.skill_propose,
        "write",
    ),
    "send_audio": ToolDefinition(
        schemas.MediaArgs,
        "Deliver a registered Telegram media artifact.",
        partial(outbound.send, operation="send_audio"),
        "outbound",
    ),
    "send_animation": ToolDefinition(
        schemas.MediaArgs,
        "Deliver a registered Telegram media artifact.",
        partial(outbound.send, operation="send_animation"),
        "outbound",
    ),
    "send_sticker": ToolDefinition(
        schemas.MediaArgs,
        "Deliver a registered Telegram media artifact.",
        partial(outbound.send, operation="send_sticker"),
        "outbound",
    ),
    "send_video_note": ToolDefinition(
        schemas.MediaArgs,
        "Deliver a registered Telegram media artifact.",
        partial(outbound.send, operation="send_video_note"),
        "outbound",
    ),
    "send_media_group": ToolDefinition(
        schemas.AlbumArgs,
        "Deliver an ordered album of registered artifacts.",
        partial(outbound.send, operation="send_media_group"),
        "outbound",
    ),
    "send_contact": ToolDefinition(
        schemas.ContactArgs,
        "Deliver a contact.",
        partial(outbound.send, operation="send_contact"),
        "outbound",
    ),
    "send_venue": ToolDefinition(
        schemas.VenueArgs,
        "Deliver a venue.",
        partial(outbound.send, operation="send_venue"),
        "outbound",
    ),
}

# The original acceptance suite names these capabilities explicitly.
BASELINE = (
    "send_message",
    "reply",
    "forward",
    "edit_message",
    "delete_message",
    "pin",
    "send_photo",
    "send_document",
    "send_voice",
    "send_video",
    "send_location",
    "send_poll",
    "send_buttons",
    "react",
    "get_reactions",
    "schedule_task",
    "list_tasks",
    "delete_task",
    "remember",
    "recall",
    "forget",
    "recall_conversation",
    "connect",
    "restore",
    "bulk_memory",
    "memory_history",
    "review_corrections",
    "pin_attention",
    "unpin_attention",
    "get_cost_report",
    "log_deep_work_quality",
    "browse",
    "delegate",
)
