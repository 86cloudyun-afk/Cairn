"""Artifact context and writeback shared by both explore conclusion paths."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.tasks.common import best_effort_release, write_conclude_result_with_fact_id
from cairn.server.models import Artifact, Intent

LOG = logging.getLogger(__name__)


def active_artifacts(artifacts: list[Artifact], *, now: datetime | None = None) -> list[Artifact]:
    now = now or datetime.now(timezone.utc)
    active = []
    for artifact in artifacts:
        if artifact.expires_at is not None:
            expiry = datetime.fromisoformat(str(artifact.expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= now:
                continue
        active.append(artifact)
    return active


def format_artifacts(artifacts: list[Artifact]) -> str:
    return json.dumps([artifact.model_dump(mode="json") for artifact in active_artifacts(artifacts)], ensure_ascii=False, indent=2)


def session_artifact(session: str, intent: Intent, worker: WorkerConfig) -> dict[str, Any]:
    topic = intent.session_key or intent.description
    return {"kind": "session", "name": topic, "value": {
        "session_id": session, "topic": topic, "worker": worker.name, "backend": worker.type,
    }}


def select_session_artifact(artifacts: list[Artifact], intent: Intent, worker: WorkerConfig) -> Artifact | None:
    topic = intent.session_key or intent.description
    entries = active_artifacts(artifacts)
    for _, artifact in sorted(enumerate(entries), key=lambda item: (item[1].created_at, item[0]), reverse=True):
        value = artifact.value
        if artifact.kind != "session" or not isinstance(value, dict):
            continue
        if (value.get("topic") == topic and value.get("worker") == worker.name
                and value.get("backend") == worker.type and isinstance(value.get("session_id"), str)
                and value["session_id"].strip()):
            return artifact
    return None


def select_session(artifacts: list[Artifact], intent: Intent, worker: WorkerConfig) -> str | None:
    artifact = select_session_artifact(artifacts, intent, worker)
    return artifact.value["session_id"] if artifact is not None else None


def write_explore_result(
    client: CairnClient, project_id: str, intent: Intent, worker: WorkerConfig,
    result: dict[str, Any], *, source: str, phase_ms: int,
    session: str | None = None, resumed_artifact: Artifact | None = None,
    total_ms: int | None = None,
) -> str:
    artifacts = list(result["artifacts"])
    if session:
        saved_session = session_artifact(session, intent, worker)
        if (resumed_artifact is not None and isinstance(resumed_artifact.value, dict)
                and resumed_artifact.value.get("session_id") == session
                and resumed_artifact.expires_at is not None):
            saved_session["expires_at"] = resumed_artifact.expires_at.isoformat()
        artifacts.insert(0, saved_session)
    artifact_ids = []
    for artifact in artifacts:
        # Save reusable state before closing the intent. A failed POST leaves it
        # open for retry; already saved artifacts remain available to that retry.
        response = client.create_artifact(project_id, artifact)
        artifact_id = response.data.get("id") if isinstance(response.data, dict) else None
        if not response.ok or not isinstance(artifact_id, str) or not artifact_id:
            # Do not log response bodies: these may contain tokens or credentials.
            LOG.error("artifact write failed before conclusion project=%s intent=%s status=%s", project_id, intent.id, response.status_code)
            best_effort_release(client, project_id, intent.id, worker.name)
            return "failed"
        artifact_ids.append(artifact_id)
    fields = dict(result["fact"])
    if artifact_ids:
        fields["artifact_ids"] = artifact_ids
    # Fact creation, intent conclusion and artifact binding share one server
    # transaction. A crash before this call cannot hide saved reusable state.
    return write_conclude_result_with_fact_id(
        client, project_id, intent.id, worker.name, result["description"], source=source,
        phase_ms=phase_ms, total_ms=total_ms, fact_fields=fields,
    ).status
