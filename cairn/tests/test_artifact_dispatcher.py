from __future__ import annotations

import json

import pytest

from cairn.dispatcher import contracts
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import explore
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project


def test_optional_plan_contract_and_action_membership():
    data = {"plan": {"current_stage": 0, "stages": ["register", "login", "submit"],
                     "next_steps": ["register"], "blockers": []},
            "intents": [{"from": ["origin"], "description": "register", "priority": 2, "depends_on": []}]}
    payload = {"accepted": True, "data": data}
    assert contracts.validate_reason_plan(payload) == data["plan"]
    assert contracts.validate_reason_payload(payload, True, 3)[0] == "intents"
    data["intents"][0]["description"] = "unplanned"
    with pytest.raises(ValueError, match="next_steps"):
        contracts.validate_reason_payload(payload, True, 3)


@pytest.mark.parametrize("field,value", [("current_stage", True), ("current_stage", 4), ("stages", "bad"), ("next_steps", [3])])
def test_malformed_plan_rejected(field, value):
    plan = {"current_stage": 0, "stages": ["register"], "next_steps": ["register"], "blockers": []}
    plan[field] = value
    with pytest.raises(ValueError):
        contracts.validate_reason_plan({"accepted": True, "data": {"plan": plan}})


def test_complete_is_validated_before_plan_write():
    with pytest.raises(ValueError, match="complete"):
        contracts.validate_reason_payload({"accepted": True, "data": {
            "plan": {"stages": ["one"], "current_stage": 1},
            "complete": {"from": [], "description": 3}}}, True, 3)


def test_explicit_blockers_allow_plan_without_dispatchable_actions():
    assert contracts.validate_reason_payload({"accepted": True, "data": {
        "plan": {"stages": ["one"], "blockers": ["waiting for test environment"]},
        "intents": []}}, True, 3) == ("noop", None)


def test_structured_conclusion_validates_all_artifacts_before_writes():
    payload = {"accepted": True, "data": {
        "fact": {"description": "Created a test account", "kind": "credential", "summary": "test account", "confidence": 95},
        "artifacts": [{"kind": "account", "name": "test", "value": {"username": "tester"}}]}}
    kind, result = contracts.validate_explore_result(payload)
    assert kind == "fact"
    assert result["fact"]["kind"] == "credential"
    assert result["artifacts"][0]["value"] == {"username": "tester"}
    assert contracts.validate_explore_payload(payload) == ("fact", "Created a test account")
    payload["data"]["artifacts"].append({"kind": "invalid", "name": "bad"})
    with pytest.raises(ValueError):
        contracts.validate_explore_result(payload)


class ArtifactClient(FakeClient):
    def __init__(self, project):
        super().__init__(project)
        self.artifact_writes = []
        self.fact_fields = []

    def conclude(self, *args, **fields):
        self.fact_fields.append(fields)
        return super().conclude(*args)

    def create_artifact(self, project_id, artifact):
        self.artifact_writes.append((project_id, artifact))
        return ApiResult(201, {"id": f"a{len(self.artifact_writes):03d}", **artifact})


def run_explore(monkeypatch, payloads, *, project=None, driver=None):
    config = make_config()
    intent = make_intent()
    intent.session_key = "registration-flow"
    project = project or make_project(intents=[intent])
    client = ArtifactClient(project)
    driver = driver or FakeDriver()
    results = iter(ProcessResult(0, p if isinstance(p, str) else json.dumps(p), "") for p in payloads)
    monkeypatch.setattr(explore, "get_driver", lambda *_a: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *_a: FakeLease())
    monkeypatch.setattr(explore, "_run_process", lambda *_a, **_k: next(results))
    outcome = explore.run_explore_task(config, client, FakeContainerManager(), project, "facts: []", intent, config.workers[0], TaskCancellation())
    return outcome, client, driver


@pytest.mark.parametrize("fallback", [False, True])
def test_artifacts_and_metadata_persist_on_both_conclusion_paths(monkeypatch, fallback):
    payload = {"accepted": True, "data": {"fact": {"description": "logged in", "kind": "finding", "confidence": 100},
        "artifacts": [{"kind": "token", "name": "access", "value": "test-token"}]}}
    outcome, client, _ = run_explore(monkeypatch, (["keep working"] if fallback else []) + [payload])
    assert outcome == "success"
    assert client.fact_fields == [{"kind": "finding", "confidence": 100, "artifact_ids": ["a001"]}]
    assert "fact_id" not in client.artifact_writes[0][1]
    assert client.artifact_writes[0][1]["value"] == "test-token"


def test_artifact_inventory_expiry_and_session_topic(monkeypatch):
    from cairn.dispatcher.artifacts import format_artifacts, select_session
    from cairn.server.models import Artifact
    config = make_config()
    intent = make_intent()
    intent.session_key = "registration-flow"
    def artifact(id, topic="registration-flow", expiry=None, worker="test-worker"):
        return Artifact(id=id, project_id="proj_001", kind="session", name=topic,
                        value={"session_id": id, "topic": topic, "worker": worker, "backend": "mock"},
                        expires_at=expiry, created_at="2026-01-01T00:00:00Z")
    entries = [artifact("valid"), artifact("expired", expiry="2000-01-01T00:00:00Z"),
               artifact("other-topic", topic="other"), artifact("wrong-worker", worker="other")]
    assert select_session(entries, intent, config.workers[0]) == "valid"
    inventory = format_artifacts(entries)
    assert "valid" in inventory and "expired" not in inventory
    assert "{artifacts}" not in run_explore(monkeypatch, [{"description": "done"}])[2].execute_prompts[0]


def test_latest_session_wins_when_created_at_ties():
    from cairn.dispatcher.artifacts import select_session
    from cairn.server.models import Artifact
    intent = make_intent()
    entries = [Artifact(id=s, project_id="proj_001", name="investigate", kind="session",
                       value={"session_id": s, "topic": "investigate", "worker": "test-worker", "backend": "mock"},
                       created_at="2026-01-01T00:00:00Z") for s in ("old", "new")]
    assert select_session(entries, intent, make_config().workers[0]) == "new"


@pytest.mark.parametrize("driver", [CodexDriver(local=True), ClaudeCodeDriver()])
def test_resume_driver_uses_resume_command(driver):
    command = driver.build_resume(make_config().workers[0], "continue", "session-old")
    assert command.session == "session-old"
    assert "session-old" in command.argv
    assert "resume" in command.argv or "-r" in command.argv
    assert "--session-id" not in command.argv


def test_artifact_client_posts_new_endpoint():
    class Session:
        def request(self, method, url, **kwargs):
            assert (method, url) == ("POST", "http://server/projects/proj_001/artifacts")
            assert kwargs["json"] == {"kind": "token", "name": "access", "value": "test"}
            class Response:
                headers = {"content-type": "application/json"}
                status_code = 201
                text = "{}"
                def json(self): return {}
            return Response()
    client = CairnClient("http://server")
    client._local.session = Session()
    assert client.create_artifact("proj_001", {"kind": "token", "name": "access", "value": "test"}).ok


@pytest.mark.parametrize("fallback", [False, True])
def test_matching_session_resumes_and_is_saved_after_conclude(monkeypatch, fallback):
    from cairn.server.models import Artifact
    from cairn.dispatcher.workers.base import DriverResult
    class ResumeDriver(FakeDriver):
        resumed = None
        def supports_resume(self): return True
        def build_resume(self, worker, prompt, session):
            self.resumed = session
            self.execute_prompts.append(prompt)
            return DriverResult(["resume", session], session=session)
    project = make_project()
    project.artifacts = [Artifact(id="a001", project_id=project.project.id, name="registration-flow", kind="session",
        value={"session_id": "existing-session", "topic": "registration-flow", "worker": "test-worker", "backend": "mock"},
        created_at="2026-01-01T00:00:00Z")]
    outcome, client, driver = run_explore(monkeypatch, (["continue"] if fallback else []) + [{"description": "done"}], project=project, driver=ResumeDriver())
    assert outcome == "success"
    assert driver.resumed == "existing-session"
    assert client.artifact_writes[-1][1]["value"]["session_id"] == "existing-session"
    assert client.fact_fields[-1]["artifact_ids"] == ["a001"]


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("session_source,response_session,expected_expiry,expected_after_expiry", [
    ("restored", "existing-session", "2030-01-01T00:00:00+00:00", None),
    ("replaced", "replacement-session", None, "replacement-session"),
    ("fresh", "session-001", None, "session-001"),
])
def test_session_writeback_preserves_expiry_only_for_restored_session(
    monkeypatch, fallback, session_source, response_session, expected_expiry, expected_after_expiry,
):
    from datetime import datetime, timezone

    from cairn.dispatcher.artifacts import active_artifacts, select_session
    from cairn.dispatcher.workers.base import DriverResult
    from cairn.server.models import Artifact

    class ResumeDriver(FakeDriver):
        def supports_resume(self): return True
        def build_resume(self, worker, prompt, session):
            return DriverResult(["resume", session], session=session)
        def extract_session(self, session, stdout, stderr): return response_session

    project = make_project()
    topic = "other-flow" if session_source == "fresh" else "registration-flow"
    project.artifacts = [Artifact(
        id="existing-artifact", project_id=project.project.id, name=topic, kind="session",
        value={"session_id": "existing-session", "topic": topic, "worker": "test-worker", "backend": "mock"},
        expires_at="2030-01-01T00:00:00Z", created_at="2026-01-01T00:00:00Z",
    )]
    outcome, client, _ = run_explore(
        monkeypatch, (["continue"] if fallback else []) + [{"description": "done"}],
        project=project, driver=ResumeDriver(),
    )
    assert outcome == "success"
    saved = Artifact(
        id="saved-artifact", project_id=project.project.id, created_at="2026-10-25T00:00:00Z",
        **client.artifact_writes[-1][1],
    )
    assert (saved.expires_at.isoformat() if saved.expires_at is not None else None) == expected_expiry
    intent = make_intent()
    intent.session_key = "registration-flow"
    inventory_after_expiry = active_artifacts(
        [*project.artifacts, saved], now=datetime(2031, 1, 1, tzinfo=timezone.utc),
    )
    assert select_session(inventory_after_expiry, intent, make_config().workers[0]) == expected_after_expiry


def test_artifact_write_failure_is_reported(monkeypatch):
    monkeypatch.setattr(ArtifactClient, "create_artifact", lambda *_a: ApiResult(503))
    outcome, client, _ = run_explore(monkeypatch, [{"description": "done", "artifacts": [{"kind": "token", "name": "t", "value": "x"}]}])
    assert outcome == "failed"
    assert client.concluded == []
    assert client.released == [("proj_001", "i001", "test-worker")]


def test_saved_artifacts_precede_fact_visibility(monkeypatch):
    events = []
    original_create = ArtifactClient.create_artifact
    original_conclude = ArtifactClient.conclude
    def create(client, *args, **kwargs):
        events.append("artifact")
        return original_create(client, *args, **kwargs)
    def conclude(client, *args, **kwargs):
        events.append("conclude")
        return original_conclude(client, *args, **kwargs)
    monkeypatch.setattr(ArtifactClient, "create_artifact", create)
    monkeypatch.setattr(ArtifactClient, "conclude", conclude)
    outcome, _, _ = run_explore(monkeypatch, [{"description": "done", "artifacts": [{"kind": "token", "name": "t", "value": "x"}]}])
    assert outcome == "success"
    assert events == ["artifact", "conclude"]


def test_partial_artifact_failure_keeps_successful_inventory_and_intent_open(monkeypatch):
    original = ArtifactClient.create_artifact
    def create(client, project_id, artifact):
        if artifact["name"] == "second":
            return ApiResult(503)
        return original(client, project_id, artifact)
    monkeypatch.setattr(ArtifactClient, "create_artifact", create)
    outcome, client, _ = run_explore(monkeypatch, [{"description": "done", "artifacts": [
        {"kind": "account", "name": "first", "value": {"username": "reusable"}},
        {"kind": "token", "name": "second", "value": "retry"}]}])
    assert outcome == "failed"
    assert client.concluded == []
    assert [a[1]["name"] for a in client.artifact_writes] == ["first"]
    assert client.released == [("proj_001", "i001", "test-worker")]
