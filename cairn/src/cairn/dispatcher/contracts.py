from __future__ import annotations

from typing import Any

from cairn.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload) - {"plan"}
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact")) and ("complete" not in payload or _is_dict(payload.get("complete")))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and ("description" in payload or "fact" in payload) and not (set(payload) - {"description", "fact", "artifacts"})


def validate_reason_plan(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate an optional plan without changing the legacy action return type."""
    if payload.get("accepted") is False:
        return None
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict) or "plan" not in data:
        return None
    plan = data["plan"]
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    stage = plan.get("current_stage", 0)
    if type(stage) is not int or stage < 0:
        raise ValueError("plan.current_stage must be a nonnegative integer")
    result = {"current_stage": stage}
    for key in ("stages", "next_steps", "blockers"):
        values = plan.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"plan.{key} must be an array of nonempty strings")
        result[key] = values
    if stage > len(result["stages"]):
        raise ValueError("plan.current_stage exceeds stages")
    return result


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    plan = validate_reason_plan(payload)
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        sources = complete["from"]
        if (not isinstance(sources, list) or not sources
                or any(not isinstance(fid, str) or not fid.strip() for fid in sources)
                or not isinstance(complete["description"], str) or not complete["description"].strip()):
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
            if not isinstance(intent["description"], str) or not intent["description"].strip():
                raise ValueError(f"invalid intent description at index {i}")
            sources = intent["from"]
            if not isinstance(sources, list) or not sources or any(not isinstance(fid, str) or not fid.strip() for fid in sources):
                raise ValueError(f"invalid intent from at index {i}")
            dependencies = intent.get("depends_on", [])
            if not isinstance(dependencies, list) or any(not isinstance(d, str) or not d.strip() for d in dependencies):
                raise ValueError("depends_on must be an array of intent IDs")
            if type(intent.get("priority", 0)) is not int:
                raise ValueError("priority must be an integer")
            if "session_key" in intent and intent["session_key"] is not None and (not isinstance(intent["session_key"], str) or not intent["session_key"].strip()):
                raise ValueError("session_key must be a nonempty string")
            if plan is not None and intent["description"] not in plan["next_steps"]:
                raise ValueError("intent.description must correspond to plan.next_steps")
        if not intents and open_intents_empty and not (plan is not None and plan["blockers"]):
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty and not (plan is not None and plan["blockers"]):
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


def validate_reason_references(kind: str, data: Any, *, fact_ids: set[str], intent_ids: set[str], plan: dict[str, Any] | None) -> None:
    """Check the complete proposed action set before persisting its plan."""
    actions = data if kind == "intents" else [data] if kind == "complete" else []
    for action in actions:
        if any(fid not in fact_ids or fid == "goal" for fid in action["from"]):
            raise ValueError("action references an unavailable source fact")
        if any(iid not in intent_ids for iid in action.get("depends_on", [])):
            raise ValueError("action references an unavailable dependency")
    if plan is not None and plan["stages"]:
        finished = plan["current_stage"] == len(plan["stages"])
        if kind == "complete" and not finished:
            raise ValueError("complete requires all plan stages to finish")
        if kind == "intents" and finished:
            raise ValueError("cannot propose actions after all plan stages finish")


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    validate_bootstrap_fact(payload)

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        return "fact", result
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    validate_bootstrap_fact(payload)
    return "fact", fact_description.strip()


def validate_bootstrap_fact(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if payload.get("accepted") is True else payload
    if payload.get("accepted") is False:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("fact"), dict):
        raise ValueError("fact is required")
    return _validate_fact_fields(data["fact"])


def _validate_fact_fields(fact: dict[str, Any]) -> dict[str, Any]:
    from cairn.server.models import ConcludeRequest
    fields = {key: fact[key] for key in ("kind", "summary", "ip", "port", "url", "confidence") if key in fact}
    for key in ("confidence", "port"):
        if fields.get(key) is not None and type(fields[key]) is not int:
            raise ValueError(f"fact.{key} must be an integer")
    validated = ConcludeRequest.model_validate({"worker": "dispatcher", "description": fact.get("description"), **fields})
    return {key: getattr(validated, key) for key in fields}


def validate_explore_result(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    fact = data.get("fact")
    if fact is None:
        fact = {"description": data.get("description")}
    if not isinstance(fact, dict):
        raise ValueError("fact must be an object")
    description = fact.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    from cairn.server.models import CreateArtifactRequest

    fields = _validate_fact_fields(fact)
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError("artifacts must be an array")
    clean_artifacts = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("each artifact must be an object")
        if artifact.get("fact_id") is not None:
            raise ValueError("artifact.fact_id is assigned from the concluded fact")
        clean_artifacts.append(CreateArtifactRequest.model_validate(artifact).model_dump(mode="json", exclude_none=True))
    return "fact", {"description": description.strip(), "fact": fields, "artifacts": clean_artifacts}


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    kind, result = validate_explore_result(payload)
    return kind, result["description"] if result is not None else None
