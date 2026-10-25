import json

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import Artifact, CreateArtifactRequest
from cairn.server.services import (
    build_artifacts,
    check_project_active,
    get_project_or_404,
    next_artifact_id,
    utcnow,
    validate_facts_exist,
)

router = APIRouter(tags=["artifacts"])


@router.post("/projects/{project_id}/artifacts", response_model=Artifact, status_code=201)
def create_artifact(project_id: str, body: CreateArtifactRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        if body.fact_id is not None:
            validate_facts_exist(conn, project_id, [body.fact_id])
        artifact = Artifact(
            id=next_artifact_id(conn, project_id), project_id=project_id,
            created_at=utcnow(), **body.model_dump(),
        )
        conn.execute(
            "INSERT INTO artifacts (id, project_id, fact_id, kind, name, value, file_path, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.id, project_id, artifact.fact_id, artifact.kind, artifact.name,
                json.dumps(artifact.value) if artifact.value is not None else None,
                artifact.file_path,
                artifact.expires_at.isoformat() if artifact.expires_at is not None else None,
                artifact.created_at.isoformat(),
            ),
        )
        return artifact


@router.get("/projects/{project_id}/artifacts", response_model=list[Artifact])
def list_artifacts(project_id: str, include_expired: bool = False):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_artifacts(conn, project_id, include_expired=include_expired)
