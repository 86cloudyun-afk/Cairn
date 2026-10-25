from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
import yaml

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def create_project(client, goal="finish"):
    response = client.post("/projects", json={"title": "test", "origin": "start", "goal": goal})
    assert response.status_code == 201
    return response.json()["project"]["id"]


def create_intent(client, project_id, **fields):
    return client.post(f"/projects/{project_id}/intents", json={
        "from": ["origin"], "description": "investigate", "creator": "reasoner", **fields,
    })


def claim_reason(client, project_id):
    assert client.post(f"/projects/{project_id}/reason/claim", json={
        "worker": "reasoner", "trigger": "facts",
    }).status_code == 200


def test_multiline_stages_must_be_finished_in_order_before_completion(client):
    project_id = create_project(client, "first\n\nsecond\n third")
    plan = client.get(f"/projects/{project_id}").json()["project"]["plan"]
    assert plan == {"current_stage": 0, "stages": ["first", "second", "third"], "next_steps": [], "blockers": []}
    completion = {"from": ["origin"], "description": "done", "worker": "reasoner"}
    assert client.post(f"/projects/{project_id}/complete", json=completion).status_code == 409
    claim_reason(client, project_id)
    for stage in (1, 2):
        plan["current_stage"] = stage
        response = client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": plan})
        assert response.status_code == 200
        assert response.json()["plan"]["current_stage"] == stage
        assert client.post(f"/projects/{project_id}/complete", json=completion).status_code == 409
    plan["current_stage"] = 3
    assert client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": plan}).status_code == 200
    assert client.post(f"/projects/{project_id}/complete", json=completion).status_code == 200


def test_plan_requires_reason_owner_and_preserves_stage_order_and_progress(client):
    project_id = create_project(client, "first\nsecond\nthird")
    plan = {"current_stage": 1, "stages": ["first", "second", "third"], "next_steps": ["verify"], "blockers": []}
    route = f"/projects/{project_id}/plan"
    assert client.put(route, json={"worker": "reasoner", "plan": plan}).status_code == 409
    claim_reason(client, project_id)
    assert client.put(route, json={"worker": "other", "plan": plan}).status_code == 409
    assert client.put(route, json={"worker": "reasoner", "plan": plan}).status_code == 200
    for invalid in (
        {**plan, "current_stage": 0},
        {**plan, "current_stage": 4},
        {**plan, "stages": ["second", "first", "third"]},
        {**plan, "stages": []},
    ):
        assert client.put(route, json={"worker": "reasoner", "plan": invalid}).status_code in (409, 422)
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?", (project_id,))
    assert client.put(route, json={"worker": "reasoner", "plan": plan}).status_code == 409
    assert client.get(f"/projects/{project_id}").json()["project"]["plan"] == plan


def test_dependencies_block_both_claim_paths_until_prerequisite_concluded(client):
    project_id = create_project(client)
    prerequisite = create_intent(client, project_id).json()["id"]
    response = create_intent(client, project_id, depends_on=[prerequisite], priority=7, session_key="topic-a")
    assert response.status_code == 201
    dependent = response.json()
    assert dependent["depends_on"] == ["i001"]
    assert dependent["priority"] == 7
    assert dependent["session_key"] == "topic-a"
    assert client.post(f"/projects/{project_id}/intents/{dependent['id']}/heartbeat", json={"worker": "explorer"}).status_code == 409
    assert create_intent(client, project_id, depends_on=[prerequisite], worker="reasoner").status_code == 409
    assert client.post(f"/projects/{project_id}/intents/{prerequisite}/conclude", json={"worker": "explorer", "description": "ready"}).status_code == 200
    assert client.post(f"/projects/{project_id}/intents/{dependent['id']}/heartbeat", json={"worker": "explorer"}).status_code == 200
    assert create_intent(client, project_id, depends_on=[prerequisite], worker="reasoner").status_code == 201


def test_dependencies_reject_missing_cross_project_and_self_reference(client):
    project_id = create_project(client)
    other_project = create_project(client)
    create_intent(client, other_project)
    assert create_intent(client, project_id, depends_on=["i001"]).status_code in (400, 404)
    assert create_intent(client, project_id).json()["id"] == "i001"
    assert create_intent(client, project_id, depends_on=["i002"]).status_code == 400
    assert create_intent(client, project_id, depends_on=["missing"]).status_code == 404


def test_conclusion_persists_metadata_without_truncating_description(client):
    project_id = create_project(client)
    intent_id = create_intent(client, project_id).json()["id"]
    description = "Found endpoint\n" + "long evidence " * 30
    metadata = {"kind": "endpoint", "summary": "HTTP endpoint", "ip": "192.0.2.1", "port": 443, "url": "https://example.test", "confidence": 93}
    response = client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={"worker": "explorer", "description": description, **metadata})
    assert response.status_code == 200
    fact = response.json()["fact"]
    assert fact == {"id": "f001", "description": description, **metadata}
    assert client.get(f"/projects/{project_id}").json()["facts"][-1] == fact
    with db.get_conn() as conn:
        row = conn.execute("SELECT description, summary, confidence FROM facts WHERE project_id = ? AND id = 'f001'", (project_id,)).fetchone()
    assert tuple(row) == (description, "HTTP endpoint", 93)


def test_fact_summary_defaults_to_single_line_and_rejects_invalid_metadata(client):
    project_id = create_project(client)
    intent_id = create_intent(client, project_id).json()["id"]
    route = f"/projects/{project_id}/intents/{intent_id}/conclude"
    for metadata in ({"confidence": -1}, {"confidence": 101}, {"kind": "unknown"}, {"port": 65536}):
        assert client.post(route, json={"worker": "explorer", "description": "evidence", **metadata}).status_code == 422
    description = "first line\n\tsecond line " + "x" * 130
    fact = client.post(route, json={"worker": "explorer", "description": description}).json()["fact"]
    assert fact["description"] == description
    assert fact["summary"] == "first line second line " + "x" * 97
    assert fact["kind"] == "other"


def test_artifacts_are_json_persisted_and_expiry_compares_instants(client):
    project_id = create_project(client)
    route = f"/projects/{project_id}/artifacts"
    offset = timezone(timedelta(hours=9))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone(offset).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(offset).isoformat()
    response = client.post(route, json={"kind": "session", "name": "console", "value": {"cookie": "secret", "active": True}, "fact_id": "origin", "expires_at": future})
    assert response.status_code == 201
    active = response.json()
    assert active["id"] == "a001"
    assert active["project_id"] == project_id
    assert active["value"] == {"cookie": "secret", "active": True}
    assert active["expires_at"].endswith("Z")
    assert client.post(route, json={"kind": "token", "name": "expired", "value": "expired-secret", "expires_at": past}).status_code == 201
    assert client.post(route, json={"kind": "endpoint", "name": "permanent", "value": "https://example.test"}).status_code == 201
    assert [a["name"] for a in client.get(route).json()] == ["console", "permanent"]
    assert len(client.get(route + "?include_expired=true").json()) == 3
    assert len(client.get(f"/projects/{project_id}").json()["artifacts"]) == 2
    with db.get_conn() as conn:
        assert conn.execute("SELECT value FROM artifacts WHERE project_id = ? AND id = 'a001'", (project_id,)).fetchone()[0] == '{"cookie": "secret", "active": true}'


def test_artifacts_validate_fact_file_and_timezone_then_cascade(client):
    project_id = create_project(client)
    other_project = create_project(client)
    create_intent(client, other_project)
    client.post(f"/projects/{other_project}/intents/i001/conclude", json={"worker": "explorer", "description": "foreign fact"})
    route = f"/projects/{project_id}/artifacts"
    assert client.post(route, json={"kind": "token", "name": "foreign", "fact_id": "f001"}).status_code == 404
    assert client.post(route, json={"kind": "file", "name": "missing path"}).status_code == 422
    assert client.post(route, json={"kind": "token", "name": "naive", "expires_at": "2026-01-01T00:00:00"}).status_code == 422
    assert client.post(route, json={"kind": "file", "name": "report", "file_path": "/tmp/report", "fact_id": "origin"}).status_code == 201
    assert client.delete(f"/projects/{project_id}").status_code == 204
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE project_id = ?", (project_id,)).fetchone()[0] == 0


def test_yaml_exports_full_evidence_plan_and_dependencies_without_artifact_secrets(client):
    project_id = create_project(client, "first\nsecond")
    claim_reason(client, project_id)
    plan = {"current_stage": 1, "stages": ["first", "second"], "next_steps": ["continue"], "blockers": []}
    assert client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": plan}).status_code == 200
    create_intent(client, project_id)
    description = "evidence\n" + "z" * 400
    client.post(f"/projects/{project_id}/intents/i001/conclude", json={"worker": "explorer", "description": description, "kind": "finding", "confidence": 80})
    create_intent(client, project_id, depends_on=["i001"], priority=4)
    assert client.post(f"/projects/{project_id}/artifacts", json={"kind": "token", "name": "auth", "value": "never-export-me"}).status_code == 201
    response = client.get(f"/projects/{project_id}/export?format=yaml")
    exported = yaml.safe_load(response.text)
    assert exported["project"]["plan"] == plan
    assert exported["facts"][-1]["description"] == description
    assert exported["facts"][-1]["confidence"] == 80
    assert exported["intents"][0]["id"] == "i001"
    assert exported["intents"][-1]["id"] == "i002"
    assert exported["intents"][-1]["depends_on"] == ["i001"]
    assert exported["intents"][-1]["priority"] == 4
    assert "never-export-me" not in response.text


def test_restart_preserves_plan_dependencies_artifacts_and_legacy_completion(client, monkeypatch):
    project_id = create_project(client, "first\nsecond")
    claim_reason(client, project_id)
    plan = {"current_stage": 1, "stages": ["first", "second"], "next_steps": [], "blockers": ["waiting"]}
    assert client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": plan}).status_code == 200
    create_intent(client, project_id)
    create_intent(client, project_id, depends_on=["i001"], priority=2)
    assert client.post(f"/projects/{project_id}/artifacts", json={"kind": "account", "name": "user", "value": {"username": "test"}}).status_code == 201
    path = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["plan"] == plan
    assert detail["intents"][-1]["depends_on"] == ["i001"]
    assert detail["intents"][-1]["priority"] == 2
    assert detail["artifacts"][0]["value"] == {"username": "test"}
    legacy_id = create_project(client)
    assert client.post(f"/projects/{legacy_id}/complete", json={"from": ["origin"], "description": "done", "worker": "legacy"}).status_code == 200


def test_fact_deletion_cascades_only_linked_artifacts(client):
    project_id = create_project(client)
    route = f"/projects/{project_id}/artifacts"
    assert client.post(route, json={"kind": "token", "name": "linked", "fact_id": "origin"}).status_code == 201
    assert client.post(route, json={"kind": "token", "name": "unlinked"}).status_code == 201
    with db.get_conn() as conn:
        conn.execute("DELETE FROM facts WHERE project_id = ? AND id = 'origin'", (project_id,))
    assert [artifact["name"] for artifact in client.get(route).json()] == ["unlinked"]


def test_origin_goal_and_reopen_fact_preserve_original_descriptions(client):
    response = client.post("/projects", json={"title": "test", "origin": "  original start\n", "goal": " final goal\n"})
    assert response.status_code == 201
    detail = response.json()
    project_id = detail["project"]["id"]
    assert detail["facts"][0]["description"] == "  original start\n"
    assert detail["facts"][1]["description"] == " final goal\n"
    assert client.post(f"/projects/{project_id}/complete", json={"from": ["origin"], "description": "done", "worker": "reasoner"}).status_code == 200
    feedback = "  feedback detail\n"
    response = client.post(f"/projects/{project_id}/reopen", json={"description": feedback, "creator": "human"})
    assert response.status_code == 200
    assert response.json()["fact"]["description"] == feedback
    assert response.json()["fact"]["summary"] == "feedback detail"


def test_reopening_staged_project_resets_progress_for_reason_reevaluation(client):
    goal = "first\nsecond\nthird"
    project_id = create_project(client, goal)
    claim_reason(client, project_id)
    completed_plan = {
        "current_stage": 3, "stages": ["first", "second", "third"],
        "next_steps": ["old action"], "blockers": ["old blocker"],
    }
    assert client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": completed_plan}).status_code == 200
    complete = {"from": ["origin"], "description": "done", "worker": "reasoner"}
    assert client.post(f"/projects/{project_id}/complete", json=complete).status_code == 200

    response = client.post(f"/projects/{project_id}/reopen", json={"description": "new contrary evidence", "creator": "human"})
    assert response.status_code == 200
    plan = response.json()["project"]["plan"]
    assert plan == {
        "current_stage": 0, "stages": ["first", "second", "third"],
        "next_steps": [], "blockers": [],
    }
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["plan"] == plan
    assert next(f for f in detail["facts"] if f["id"] == "goal")["description"] == goal
    assert client.post(f"/projects/{project_id}/complete", json=complete).status_code == 409
    claim_reason(client, project_id)
    plan["current_stage"] = 1
    response = client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": plan})
    assert response.status_code == 200
    assert response.json()["plan"]["current_stage"] == 1


@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
def test_numeric_api_fields_reject_coercion_from_non_integer_values(client, value):
    project_id = create_project(client, "first\nsecond")
    claim_reason(client, project_id)
    plan = {"current_stage": value, "stages": ["first", "second"], "next_steps": [], "blockers": []}
    assert client.put(f"/projects/{project_id}/plan", json={"worker": "reasoner", "plan": plan}).status_code == 422
    assert create_intent(client, project_id, priority=value).status_code == 422
    intent_id = create_intent(client, project_id).json()["id"]
    route = f"/projects/{project_id}/intents/{intent_id}/conclude"
    for field in ("confidence", "port"):
        assert client.post(route, json={"worker": "explorer", "description": "evidence", field: value}).status_code == 422


def test_conclusion_atomically_attaches_durable_artifacts_to_its_fact(client):
    project_id = create_project(client)
    intent_id = create_intent(client, project_id).json()["id"]
    route = f"/projects/{project_id}/artifacts"
    artifact_ids = []
    for name in ("access", "refresh"):
        response = client.post(route, json={"kind": "token", "name": name, "value": f"{name}-value"})
        assert response.status_code == 201
        assert response.json()["fact_id"] is None
        artifact_ids.append(response.json()["id"])

    response = client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={
        "worker": "explorer", "description": "authenticated", "artifact_ids": artifact_ids,
    })

    assert response.status_code == 200
    fact_id = response.json()["fact"]["id"]
    assert fact_id == "f001"
    assert response.json()["intent"]["to"] == fact_id
    assert [artifact["fact_id"] for artifact in client.get(route).json()] == [fact_id, fact_id]


@pytest.mark.parametrize("invalid_artifact", ["missing", "foreign", "linked"])
def test_invalid_artifact_attachment_leaves_intent_fact_and_artifacts_unchanged(client, invalid_artifact):
    project_id = create_project(client)
    intent_id = create_intent(client, project_id).json()["id"]
    artifact_route = f"/projects/{project_id}/artifacts"
    reusable = client.post(artifact_route, json={"kind": "session", "name": "reusable", "value": "saved"}).json()
    if invalid_artifact == "foreign":
        other_project = create_project(client)
        other_route = f"/projects/{other_project}/artifacts"
        client.post(other_route, json={"kind": "token", "name": "first"})
        invalid_id = client.post(other_route, json={"kind": "token", "name": "foreign"}).json()["id"]
        expected_status = 404
    elif invalid_artifact == "linked":
        invalid_id = client.post(artifact_route, json={"kind": "token", "name": "linked", "fact_id": "origin"}).json()["id"]
        expected_status = 409
    else:
        invalid_id = "missing"
        expected_status = 404

    response = client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={
        "worker": "explorer", "description": "must not be saved", "artifact_ids": [reusable["id"], invalid_id],
    })

    assert response.status_code == expected_status
    detail = client.get(f"/projects/{project_id}").json()
    assert [fact["id"] for fact in detail["facts"]] == ["origin", "goal"]
    assert detail["intents"][0]["to"] is None
    assert detail["intents"][0]["concluded_at"] is None
    assert detail["artifacts"][0]["fact_id"] is None
    retry = client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={
        "worker": "explorer", "description": "recovered", "artifact_ids": [reusable["id"]],
    })
    assert retry.status_code == 200
    assert retry.json()["fact"]["id"] == "f001"


@pytest.mark.parametrize("artifact_id", ["", "   "])
def test_conclusion_rejects_empty_artifact_identifiers(client, artifact_id):
    project_id = create_project(client)
    intent_id = create_intent(client, project_id).json()["id"]
    response = client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={
        "worker": "explorer", "description": "evidence", "artifact_ids": [artifact_id],
    })
    assert response.status_code == 422
