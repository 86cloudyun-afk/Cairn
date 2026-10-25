from __future__ import annotations

import json
from concurrent.futures import Future

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import reason
from cairn.server.models import ExecutionPlan, Fact

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent, make_project
from test_scheduler_logic import _loop, _summary


class PlanClient(FakeClient):
    def __init__(self, project):
        super().__init__(project)
        self.events = []
        self.plan_status = 200

    def update_plan(self, project_id, plan, worker):
        self.events.append(("plan", project_id, plan, worker))
        if self.plan_status == 200:
            self.project.project.plan = ExecutionPlan.model_validate(plan)
        return ApiResult(self.plan_status, {})

    def create_intent(self, project_id, from_ids, description, creator, **metadata):
        self.events.append(("intent", description, metadata))
        return super().create_intent(project_id, from_ids, description, creator)

    def complete(self, project_id, from_ids, description, worker):
        self.events.append(("complete", description))
        return super().complete(project_id, from_ids, description, worker)


def _run_reason(monkeypatch, project, client, data, *, config=None):
    config = config or make_config()
    driver = FakeDriver()
    lease = FakeLease()
    monkeypatch.setattr(reason, "get_driver", lambda *_args: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *_args: lease)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(0, json.dumps({"accepted": True, "data": data}), ""),
    )
    outcome = reason.run_reason_task(
        config, client, FakeContainerManager(), project, "graph", config.workers[0], TaskCancellation()
    )
    return outcome, driver.execute_prompts[0]


def _plan(stage, next_steps=None, blockers=None):
    return {
        "current_stage": stage,
        "stages": ["Find the service", "Authenticate", "Verify the result"],
        "next_steps": next_steps or [],
        "blockers": blockers or [],
    }


def test_reason_persists_three_stage_progress_before_work_and_completion(monkeypatch):
    project = make_project()
    project.project.plan = ExecutionPlan.model_validate(_plan(0, ["Find the service"]))
    client = PlanClient(project)

    for stage, (description, evidence) in enumerate(
        [("Find the service", "Service discovered"), ("Authenticate", "Authenticated session confirmed"), ("Verify the result", "Result verified")]
    ):
        plan = _plan(stage, [description])
        outcome, prompt = _run_reason(
            monkeypatch,
            project,
            client,
            {"plan": plan, "intents": [{"from": ["f001"], "description": description}]},
        )
        assert outcome == "success"
        assert client.events[-2:] == [
            ("plan", "proj_001", plan, "test-worker"),
            ("intent", description, {}),
        ]
        assert project.project.plan.current_stage == stage
        assert '"current_stage"' in prompt
        project.facts.append(Fact(id=f"evidence_{stage}", description=evidence))

    final_plan = _plan(3)
    outcome, _ = _run_reason(
        monkeypatch,
        project,
        client,
        {"plan": final_plan, "complete": {"from": ["evidence_0", "evidence_1", "evidence_2"], "description": "All three stages verified"}},
    )

    assert outcome == "success"
    assert client.events[-2:] == [
        ("plan", "proj_001", final_plan, "test-worker"),
        ("complete", "All three stages verified"),
    ]
    assert project.project.plan.current_stage == 3


def test_reason_persists_blockers_on_noop(monkeypatch):
    project = make_project(intents=[make_intent()])
    client = PlanClient(project)
    plan = _plan(1, ["Authenticate"], ["Waiting for credentials"])

    outcome, _ = _run_reason(monkeypatch, project, client, {"plan": plan})

    assert outcome == "success"
    assert client.events == [("plan", "proj_001", plan, "test-worker")]
    assert project.project.plan.blockers == ["Waiting for credentials"]


@pytest.mark.parametrize(
    "plan,description",
    [
        (_plan(-1, ["next"]), "next"),
        (_plan(4, ["next"]), "next"),
        ({**_plan(0, ["next"]), "stages": "invalid"}, "next"),
        (_plan(0, ["listed action"]), "unlisted action"),
    ],
)
def test_reason_invalid_plan_or_unlisted_intent_causes_no_writes(monkeypatch, plan, description):
    project = make_project()
    client = PlanClient(project)

    outcome, _ = _run_reason(
        monkeypatch, project, client, {"plan": plan, "intents": [{"from": ["f001"], "description": description}]}
    )

    assert outcome == "failed"
    assert client.events == []
    assert client.released_reasons == [("proj_001", "test-worker")]


@pytest.mark.parametrize("bad_action", [
    {"from": ["missing"], "description": "next"},
    {"from": ["goal"], "description": "next"},
    {"from": ["f001"], "description": "next", "depends_on": ["missing"]},
])
def test_reason_invalid_reference_anywhere_in_batch_causes_no_writes(monkeypatch, bad_action):
    project = make_project()
    client = PlanClient(project)
    outcome, _ = _run_reason(monkeypatch, project, client, {
        "plan": _plan(1, ["next"]),
        "intents": [{"from": ["f001"], "description": "next"}, bad_action]})
    assert outcome == "failed"
    assert client.events == []


@pytest.mark.parametrize("complete", [
    {"from": ["missing"], "description": "done"},
    {"from": ["goal"], "description": "done"},
    {"from": ["f001"], "description": "done"},
])
def test_reason_invalid_completion_does_not_advance_plan(monkeypatch, complete):
    project = make_project()
    client = PlanClient(project)
    outcome, _ = _run_reason(monkeypatch, project, client, {"plan": _plan(1), "complete": complete})
    assert outcome == "failed"
    assert client.events == []


@pytest.mark.parametrize("status,outcome", [(409, "failed"), (500, "failed"), (403, "success")])
def test_reason_failed_plan_write_prevents_intent_write(monkeypatch, status, outcome):
    project = make_project()
    client = PlanClient(project)
    client.plan_status = status
    plan = _plan(0, ["next"])

    result, _ = _run_reason(
        monkeypatch, project, client, {"plan": plan, "intents": [{"from": ["f001"], "description": "next"}]}
    )

    assert result == outcome
    assert client.events == [("plan", "proj_001", plan, "test-worker")]


def test_reason_forwards_dependency_priority_and_session_metadata(monkeypatch):
    project = make_project(intents=[make_intent()])
    client = PlanClient(project)
    intent = {"from": ["f001"], "description": "next", "depends_on": ["i001"], "priority": -2, "session_key": "same-session"}

    outcome, _ = _run_reason(monkeypatch, project, client, {"intents": [intent]})

    assert outcome == "success"
    assert client.events == [("intent", "next", {"depends_on": ["i001"], "priority": -2, "session_key": "same-session"})]


def test_reason_planless_output_keeps_legacy_client_compatible_and_uses_fact_summaries(monkeypatch):
    project = make_project()
    project.facts[-1].kind = "finding"
    project.facts[-1].summary = "Useful concise evidence"
    client = FakeClient(project)

    outcome, prompt = _run_reason(monkeypatch, project, client, {"intents": [{"from": ["f001"], "description": "next"}]})

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next", "test-worker")]
    assert "f001|finding|Useful concise evidence" in prompt


def test_reason_mock_prompt_preserves_json_context(monkeypatch):
    config = make_config()
    config.runtime.prompt_group = "mock"
    project = make_project()
    client = FakeClient(project)

    outcome, prompt = _run_reason(
        monkeypatch, project, client, {"intents": [{"from": ["f001"], "description": "next"}]}, config=config
    )

    assert outcome == "success"
    context = json.loads(prompt)
    assert context["fact_ids"] == ["origin", "f001"]
    assert context["plan"] == project.project.plan.model_dump()


def _dispatch_loop(project):
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client", (), {"get_project": lambda _self, _project_id: project, "export_project": lambda _self, _project_id: "graph"}
    )()
    loop.reason_checkpoints["proj_001"] = reason_checkpoint(project)
    return loop


def reason_checkpoint(project):
    from cairn.dispatcher.models import ReasonCheckpoint

    return ReasonCheckpoint(
        fact_count=len(project.facts), hint_count=len(project.hints), open_intent_count=sum(intent.to is None for intent in project.intents)
    )


def _unclaimed(intent_id, *, dependencies=(), priority=0, created_at="2026-01-01T00:00:02Z"):
    intent = make_intent(intent_id)
    intent.worker = None
    intent.depends_on = list(dependencies)
    intent.priority = priority
    intent.created_at = created_at
    return intent


def test_scheduler_requires_completed_dependencies_then_runs_all_three_stages():
    first = _unclaimed("i001", priority=5)
    second = _unclaimed("i002", dependencies=["i001"], priority=-1)
    third = _unclaimed("i003", dependencies=["i002"], priority=-2)
    project = make_project(intents=[third, second, first])
    loop = _dispatch_loop(project)
    chosen = []
    loop._dispatch_explore = lambda _project, _graph, intent: chosen.append(intent.id) or True
    loop._dispatch_reason = lambda *_args: pytest.fail("ready work should be explored")

    for intent in (first, second, third):
        assert loop._try_dispatch_project(_summary("proj_001", "active"))
        assert chosen[-1] == intent.id
        intent.to = f"result_{intent.id}"
        loop.reason_checkpoints["proj_001"] = reason_checkpoint(project)

    assert chosen == ["i001", "i002", "i003"]


def test_scheduler_selects_priority_then_oldest_then_id():
    project = make_project(
        intents=[
            _unclaimed("i004", priority=1, created_at="2026-01-01T00:00:00Z"),
            _unclaimed("i003", priority=0, created_at="2026-01-01T00:00:03Z"),
            _unclaimed("i002", priority=0),
            _unclaimed("i001", priority=0),
        ]
    )
    loop = _dispatch_loop(project)
    chosen = []
    loop._dispatch_explore = lambda _project, _graph, intent: chosen.append(intent.id) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert chosen == ["i001"]


@pytest.mark.parametrize("outcome", ["success", "failed"])
def test_all_blocked_intents_trigger_reason_and_only_success_suppresses_repeat(outcome):
    project = make_project(intents=[_unclaimed("i001", dependencies=["missing"])])
    loop = _dispatch_loop(project)
    loop.runtime_project_ids = set()
    loop._select_worker = lambda *_args: type("Selection", (), {"worker": loop.config.workers[0]})()
    loop.client.claim_reason = lambda *_args: ApiResult(200, {})
    future = Future()
    loop.executor = type("Executor", (), {"submit": lambda *_args: future})()
    loop._dispatch_explore = lambda *_args: pytest.fail("blocked work must not be explored")

    assert loop._reason_trigger(project) == "blocked_dependencies"
    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    future.set_result(outcome)
    loop._reap_futures()

    if outcome == "success":
        assert loop._reason_trigger(project) is None
        assert not loop._try_dispatch_project(_summary("proj_001", "active"))
    else:
        assert loop._reason_trigger(project) == "blocked_dependencies"


@pytest.mark.parametrize("new_context", ["fact", "hint"])
def test_initial_plan_only_blockers_wait_for_new_context_after_success(monkeypatch, new_context):
    project = make_project()
    project.project.bootstrap_enabled = False
    project.facts = project.facts[:2]
    loop = _dispatch_loop(project)
    loop.reason_checkpoints.clear()
    loop.client.claim_reason = lambda *_args: ApiResult(200, {})
    future = Future()
    loop.executor = type("Executor", (), {"submit": lambda *_args: future})()
    client = PlanClient(project)
    plan = _plan(0, blockers=["Waiting for the target address"])

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    outcome, _ = _run_reason(monkeypatch, project, client, {"plan": plan})
    assert outcome == "success"
    assert client.events == [("plan", "proj_001", plan, "test-worker")]
    assert len(project.facts) == 2 and project.intents == []
    future.set_result(outcome)
    loop._reap_futures()

    assert not loop._try_dispatch_project(_summary("proj_001", "active"))
    assert not loop._try_dispatch_project(_summary("proj_001", "active"))
    if new_context == "fact":
        project.facts.append(Fact(id="f001", description="Target address confirmed"))
    else:
        project.hints.append(project.hints[0].model_copy(update={"id": "h002", "content": "Target address supplied"}))
    assert loop._try_dispatch_project(_summary("proj_001", "active"))


def test_initial_failed_plan_only_reasoning_remains_retryable(monkeypatch):
    project = make_project()
    project.project.bootstrap_enabled = False
    project.facts = project.facts[:2]
    loop = _dispatch_loop(project)
    loop.reason_checkpoints.clear()
    loop.client.claim_reason = lambda *_args: ApiResult(200, {})
    future = Future()
    loop.executor = type("Executor", (), {"submit": lambda *_args: future})()
    client = PlanClient(project)
    client.plan_status = 500

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    outcome, _ = _run_reason(monkeypatch, project, client, {"plan": _plan(0, blockers=["Waiting for the target address"])})
    assert outcome == "failed"
    future.set_result(outcome)
    loop._reap_futures()

    assert "proj_001" not in loop.reason_checkpoints
    assert loop._try_dispatch_project(_summary("proj_001", "active"))


def test_initial_bootstrap_still_dispatches_with_an_existing_checkpoint():
    intent = _unclaimed("i001")
    intent.description = "bootstrap"
    intent.creator = "dispatcher.bootstrap"
    intent.from_ = ["origin"]
    project = make_project(intents=[intent])
    project.facts = project.facts[:2]
    loop = _dispatch_loop(project)
    chosen = []
    loop._dispatch_bootstrap = lambda _project, selected: chosen.append(selected.id) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert chosen == ["i001"]


def test_new_evidence_retriggers_reason_while_dependencies_remain_blocked():
    project = make_project(intents=[_unclaimed("i001", dependencies=["missing"])])
    loop = _dispatch_loop(project)
    loop._reason_dependency_states = {"proj_001": loop._dependency_blocked_state(project)}
    project.facts.append(Fact(id="f002", description="A new clue"))

    assert loop._reason_trigger(project) == "facts:3->4"
