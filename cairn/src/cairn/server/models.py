from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, JsonValue, ValidationInfo, field_validator, model_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


FactKind = Literal["finding", "credential", "endpoint", "negative", "other"]


class FactContent(BaseModel):
    description: str
    kind: FactKind = "other"
    summary: str = ""
    ip: str | None = None
    port: int | None = Field(default=None, ge=0, le=65535, strict=True)
    url: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100, strict=True)

    @model_validator(mode="after")
    def populate_summary(self):
        self.summary = " ".join((self.summary or self.description).split())[:120]
        return self


class Fact(FactContent):
    id: str


class ExecutionPlan(BaseModel):
    current_stage: int = Field(default=0, ge=0, strict=True)
    stages: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_current_stage(self):
        if self.current_stage > len(self.stages):
            raise ValueError("current_stage must not exceed the number of stages")
        return self


class CreateArtifactRequest(BaseModel):
    fact_id: str | None = None
    kind: Literal["account", "session", "token", "file", "endpoint"]
    name: str
    value: JsonValue | None = None
    file_path: str | None = None
    expires_at: AwareDatetime | None = None

    @field_validator("name", "fact_id", "file_path")
    @classmethod
    def validate_artifact_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_file_path(self):
        if self.kind == "file" and self.file_path is None:
            raise ValueError("file artifacts require file_path")
        return self


class Artifact(CreateArtifactRequest):
    id: str
    project_id: str
    created_at: AwareDatetime


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, strict=True)
    session_key: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None
    plan: ExecutionPlan = Field(default_factory=ExecutionPlan)


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    artifacts: list[Artifact] = Field(default_factory=list)


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str, info: ValidationInfo) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text if info.field_name == "title" else value


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, strict=True)
    session_key: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker", "session_key")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_", "depends_on")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(FactContent):
    worker: str
    artifact_ids: list[str] = Field(default_factory=list)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, value: list[str]) -> list[str]:
        cleaned = [artifact_id.strip() for artifact_id in value]
        if any(not artifact_id for artifact_id in cleaned):
            raise ValueError("artifact ids must not be empty")
        return cleaned

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str, info: ValidationInfo) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return value if info.field_name == "description" else text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectPlanRequest(HeartbeatRequest):
    plan: ExecutionPlan


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str, info: ValidationInfo) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return value if info.field_name == "description" else text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
