from __future__ import annotations

import json
from importlib import resources
from typing import Any

from cairn.server.models import Fact


def load_prompt(group: str, name: str) -> str:
    return resources.files("cairn.dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    text = template
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return text


def format_fact_ids(facts: list[Fact | dict[str, Any] | str]) -> str:
    """Render concise fact rows, folding repeated negative endpoint results.

    Facts arrive in append order, so the last row for an endpoint is its latest
    result. Legacy callers may still supply bare IDs or unstructured fact dicts.
    """
    records = [
        {"id": fact} if isinstance(fact, str) else fact.model_dump() if isinstance(fact, Fact) else fact
        for fact in facts
    ]
    negative_groups: dict[tuple[str, int], list[int]] = {}
    for index, fact in enumerate(records):
        endpoint = _negative_endpoint(fact)
        if endpoint is not None:
            negative_groups.setdefault(endpoint, []).append(index)

    rows = []
    for index, fact in enumerate(records):
        endpoint = _negative_endpoint(fact)
        group = negative_groups.get(endpoint, []) if endpoint is not None else []
        if len(group) > 3 and index != group[-1]:
            continue
        summary = " ".join(str(fact.get("summary") or "").split())
        if not summary:
            summary = " ".join(str(fact.get("description") or "").split())[:120]
        rows.append(_fact_row(fact.get("id", ""), fact.get("kind") or "other", summary))
        if len(group) > 3:
            ip, port = endpoint
            rows.append(_fact_row("...", "negative", f"{len(group) - 1} earlier negative facts collapsed for {ip}:{port}"))
    return "\n".join(rows)


def _negative_endpoint(fact: dict[str, Any]) -> tuple[str, int] | None:
    ip, port = fact.get("ip"), fact.get("port")
    if fact.get("kind") == "negative" and isinstance(ip, str) and ip.strip() and isinstance(port, int):
        return ip.strip(), port
    return None


def _fact_row(fact_id: Any, kind: Any, summary: str) -> str:
    def escape(value: Any) -> str:
        return " ".join(str(value).split()).replace("\\", "\\\\").replace("|", "\\|")

    return "|".join(escape(value) for value in (fact_id, kind, summary))


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    return format_json_block(hints)


def format_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
