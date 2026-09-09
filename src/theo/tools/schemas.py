"""Strict Pydantic argument contracts for every model-visible Theo tool.

Schemas describe wire inputs only. Capability implementations, authorization
and durable replay policy are defined outside this module.
"""

from typing import Literal

from pydantic import Field

from theo.domain import StrictModel


class Empty(StrictModel):
    pass


class MessageArgs(StrictModel):
    text: str = Field(min_length=1, max_length=100000)
    reply_to: int | None = None
    target: str | None = None
    destination_id: str | None = None
    role: Literal["final", "progress"] = "progress"


class MessageIdArgs(StrictModel):
    message_id: int
    target: str | None = None
    destination_id: str | None = None


class EditArgs(MessageIdArgs):
    text: str = Field(min_length=1, max_length=4096)


class ForwardArgs(MessageIdArgs):
    from_chat_id: int


class MediaArgs(StrictModel):
    artifact_id: str
    caption: str | None = None
    reply_to: int | None = None
    target: str | None = None
    destination_id: str | None = None


class LocationArgs(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    target: str | None = None
    destination_id: str | None = None


class PollArgs(StrictModel):
    is_anonymous: bool = False
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=12)
    target: str | None = None
    destination_id: str | None = None


class Button(StrictModel):
    text: str = Field(min_length=1, max_length=64)
    url: str


class ButtonsArgs(MessageArgs):
    buttons: list[Button] = Field(min_length=1, max_length=8)


class ReactionArgs(MessageIdArgs):
    emoji: str


class AlbumItem(StrictModel):
    kind: Literal["photo", "video", "audio", "document"]
    artifact_id: str
    caption: str | None = None


class AlbumArgs(StrictModel):
    items: list[AlbumItem] = Field(min_length=2, max_length=10)
    target: str | None = None
    destination_id: str | None = None
    reply_to: int | None = None


class ContactArgs(StrictModel):
    phone_number: str
    first_name: str
    last_name: str | None = None
    target: str | None = None
    destination_id: str | None = None


class VenueArgs(LocationArgs):
    title: str
    address: str


class ScheduleArgs(StrictModel):
    text: str = Field(min_length=1)
    due_at: float | None = None
    cron: str | None = None
    interval_seconds: int | None = None
    timezone: str = "Europe/Dublin"


class IdArgs(StrictModel):
    id: str


class RememberArgs(StrictModel):
    body: str = Field(min_length=1)
    kind: Literal["entity", "episode", "procedure", "insight", "preference"] = "episode"
    source_message_id: str | None = None
    target_memory_id: str | None = None
    expected_revision: int | None = None


class RecallArgs(StrictModel):
    query: str
    limit: int = Field(default=20, ge=1, le=50)


class ConversationArgs(StrictModel):
    limit: int = Field(default=30, ge=1, le=100)


class ConnectArgs(StrictModel):
    source_id: str
    target_id: str
    relation: str


class RestoreArgs(IdArgs):
    revision: int | None = Field(default=None, ge=1)


class BulkArgs(StrictModel):
    memories: list[RememberArgs] = Field(min_length=1, max_length=30)


class AttentionArgs(StrictModel):
    body: str = Field(min_length=1, max_length=4000)
    expires_at: float | None = None


class QualityArgs(StrictModel):
    rating: int = Field(ge=1, le=5)
    rationale: str


class BrowseArgs(StrictModel):
    url: str
    screenshot: bool = False


class DelegateArgs(StrictModel):
    task: str = Field(min_length=1, max_length=50000)
    deadline_seconds: int = Field(default=1800, ge=60, le=5400)


class GoalStep(StrictModel):
    title: str
    next_action: str
    capabilities: list[str] = Field(default_factory=list)


class GoalArgs(StrictModel):
    title: str
    criteria: str
    steps: list[GoalStep] = Field(default_factory=lambda: list[GoalStep]())


class GoalUpdateArgs(IdArgs):
    status: Literal["proposed", "active", "blocked", "paused", "completed", "abandoned"]
    evidence: str | None = None
    blocker: str | None = None


class StepArgs(IdArgs):
    evidence: str


class FactArgs(StrictModel):
    subject: str
    predicate: str
    value: str
    source_message_id: str
    expected_revision: int = 0


class ArtifactArgs(StrictModel):
    path: str
    description: str


class FileReadArgs(StrictModel):
    path: str


class FileWriteArgs(FileReadArgs):
    content: str = Field(max_length=1000000)


class CommandArgs(StrictModel):
    argv: list[str] = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(default=60, ge=1, le=300)


class VoiceArgs(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    voice: str | None = None


class SkillArgs(StrictModel):
    name: str
    body: str
    triggers: list[str] = Field(min_length=1, max_length=20)
