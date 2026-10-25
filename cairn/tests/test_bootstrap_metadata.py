from __future__ import annotations

import json

import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import bootstrap
from cairn.server.models import ExecutionPlan, Fact

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project
from test_scheduler_logic import _loop, _summary


class MetadataClient(FakeClient):
    def __init__(self, project):
        super().__init__(project)
        self.fact_fields = []

    def conclude(self, project_id, intent_id, worker, description, **metadata):
        self.fact_fields.append(metadata)
        response = super().conclude(project_id, intent_id, worker, description)
        for intent in self.project.intents:
            if intent.id == intent_id:
                intent.to = "f002"
        self.project.facts.append(Fact(id="f002", description=description, **metadata))
        return response


def _run_bootstrap(monkeypatch, project, client, outputs, *, driver=None):
    config = make_config()
    driver = driver or FakeDriver()
    lease = FakeLease()
    results = iter(
        ProcessResult(0, output if isinstance(output, str) else json.dumps({"accepted": True, "data": output}), "")
        for output in outputs
    )
    monkeypatch.setattr(bootstrap, "get_driver", lambda *_args: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", lambda *_args: lease)
    monkeypatch.setattr(bootstrap, "run_worker_process", lambda *_args, **_kwargs: next(results))
    outcome = bootstrap.run_bootstrap_task(
        config, client, FakeContainerManager(), project, project.intents[0], config.workers[0], TaskCancellation()
    )
    assert lease.started and lease.stopped
    return outcome, driver


def _bootstrap_project():
    intent = make_intent()
    intent.description = "bootstrap"
    intent.creator = "dispatcher.bootstrap"
    intent.from_ = ["origin"]
    return make_project(intents=[intent])


@pytest.mark.parametrize("fallback", [False, True])
def test_bootstrap_forwards_structured_fact_fields_from_execute_and_conclude(monkeypatch, fallback):
    project = _bootstrap_project()
    client = MetadataClient(project)
    fact = {
        "description": "The service is reachable",
        "kind": "endpoint",
        "summary": "Service found on port 443",
        "ip": "192.0.2.1",
        "port": 443,
        "url": "https://example.test",
        "confidence": 95,
    }
    result = {"fact": fact}
    if not fallback:
        result["complete"] = {"description": "Goal met"}
    outputs = ["Continue investigating", result] if fallback else [result]

    outcome, _ = _run_bootstrap(monkeypatch, project, client, outputs)

    assert outcome == "success"
    assert client.fact_fields == [{key: value for key, value in fact.items() if key != "description"}]
    assert client.concluded == [("proj_001", "i001", "test-worker", "The service is reachable")]
    assert len(client.completed) == (0 if fallback else 1)


@pytest.mark.parametrize("fallback", [False, True])
def test_bootstrap_legacy_description_remains_compatible(monkeypatch, fallback):
    project = _bootstrap_project()
    client = FakeClient(project)
    result = {"fact": {"description": "Confirmed progress"}}
    if not fallback:
        result["complete"] = {"description": "Goal met"}

    outcome, _ = _run_bootstrap(monkeypatch, project, client, ["Continue investigating", result] if fallback else [result])

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "Confirmed progress")]


@pytest.mark.parametrize("field,value", [("kind", "imaginary"), ("port", 65536), ("confidence", 101)])
def test_bootstrap_rejects_invalid_metadata_without_writing_fact(monkeypatch, field, value):
    project = _bootstrap_project()
    client = MetadataClient(project)
    driver = FakeDriver()
    driver.supports_conclude = lambda: False

    outcome, _ = _run_bootstrap(
        monkeypatch,
        project,
        client,
        [{"fact": {"description": "Invalid evidence", field: value}, "complete": {"description": "Goal met"}}],
        driver=driver,
    )

    assert outcome == "failed"
    assert client.fact_fields == []
    assert client.completed == []
    assert client.released == [("proj_001", "i001", "test-worker")]


@pytest.mark.parametrize("with_complete", [False, True])
def test_multistage_bootstrap_saves_progress_then_dispatches_reason_without_repeating_bootstrap(monkeypatch, with_complete):
    project = _bootstrap_project()
    project.facts = project.facts[:2]
    project.project.plan = ExecutionPlan(stages=["Discover", "Authenticate", "Verify"], next_steps=["Discover"])
    client = MetadataClient(project)
    result = {"fact": {"description": "Service discovered", "kind": "endpoint", "summary": "Discovery complete"}}
    if with_complete:
        result["complete"] = {"description": "Premature completion"}

    outcome, driver = _run_bootstrap(monkeypatch, project, client, [result])

    assert outcome == "success"
    assert client.completed == []
    assert project.project.plan.current_stage == 0
    assert project.intents[0].to == "f002"
    assert '"stages"' in driver.execute_prompts[0]
    assert "Authenticate" in driver.execute_prompts[0]
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client", (), {"get_project": lambda _self, _project_id: project, "export_project": lambda _self, _project_id: "graph"}
    )()
    dispatched = []
    loop._dispatch_initial_project = lambda *_args: pytest.fail("bootstrap must not run again after concluding")
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(trigger) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == ["initial"]


def test_bootstrap_fact_only_output_preserves_progress_for_single_stage_too(monkeypatch):
    project = _bootstrap_project()
    client = FakeClient(project)

    outcome, _ = _run_bootstrap(monkeypatch, project, client, [{"fact": {"description": "Confirmed progress"}}])

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "Confirmed progress")]
    assert client.completed == []
