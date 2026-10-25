from __future__ import annotations

import pytest

from cairn.dispatcher.prompting import format_fact_ids
from cairn.server.models import Fact


def test_fact_rows_use_summary_and_legacy_description_fallback() -> None:
    rendered = format_fact_ids(
        [
            Fact(id="f001", description="Legacy evidence"),
            {
                "id": "f002",
                "kind": "endpoint",
                "summary": "HTTPS service found",
                "description": "Details should stay in the snapshot",
            },
            {"id": "f003", "description": "a" * 130},
        ]
    )

    assert rendered.splitlines() == [
        "f001|other|Legacy evidence",
        "f002|endpoint|HTTPS service found",
        "f003|other|" + "a" * 120,
    ]
    assert "Details should stay" not in rendered


def test_fact_rows_normalize_whitespace_and_escape_field_separators() -> None:
    rendered = format_fact_ids(
        [{"id": "f001", "kind": "finding", "summary": " line one\r\nline\ttwo | value\\path "}]
    )

    assert rendered == "f001|finding|line one line two \\| value\\\\path"


def test_four_negative_facts_fold_to_latest_and_hidden_count() -> None:
    rendered = format_fact_ids(
        [
            Fact(id="f009", description="details", kind="negative", summary="oldest", ip="192.0.2.1", port=443),
            {"id": "f008", "kind": "negative", "summary": "second", "ip": "192.0.2.1", "port": 443},
            {"id": "f007", "kind": "negative", "summary": "third", "ip": "192.0.2.1", "port": 443},
            Fact(id="f001", description="details", kind="negative", summary="latest", ip="192.0.2.1", port=443),
        ]
    )

    assert rendered.splitlines() == [
        "f001|negative|latest",
        "...|negative|3 earlier negative facts collapsed for 192.0.2.1:443",
    ]


def test_three_negative_facts_remain_individual_rows() -> None:
    rendered = format_fact_ids(
        [
            {"id": "f001", "kind": "negative", "summary": "one", "ip": "192.0.2.1", "port": 443},
            {"id": "f002", "kind": "negative", "summary": "two", "ip": "192.0.2.1", "port": 443},
            {"id": "f003", "kind": "negative", "summary": "three", "ip": "192.0.2.1", "port": 443},
        ]
    )

    assert rendered.splitlines() == ["f001|negative|one", "f002|negative|two", "f003|negative|three"]


def test_negative_folding_keeps_other_endpoints_and_incomplete_endpoints_separate() -> None:
    rendered = format_fact_ids(
        [
            {"id": "f001", "kind": "negative", "summary": "one", "ip": "192.0.2.1", "port": 443},
            {"id": "f002", "kind": "negative", "summary": "two", "ip": "192.0.2.1", "port": 443},
            {"id": "f003", "kind": "negative", "summary": "three", "ip": "192.0.2.1", "port": 443},
            {"id": "f004", "kind": "negative", "summary": "other port", "ip": "192.0.2.1", "port": 80},
            {"id": "f005", "kind": "negative", "summary": "other host", "ip": "192.0.2.2", "port": 443},
            {"id": "f006", "kind": "negative", "summary": "host only", "ip": "192.0.2.1"},
            {"id": "f007", "kind": "negative", "summary": "port only", "port": 443},
            {"id": "f008", "kind": "negative", "summary": "no endpoint"},
            {"id": "f009", "kind": "finding", "summary": "positive", "ip": "192.0.2.1", "port": 443},
            {"id": "f010", "kind": "negative", "summary": "latest", "ip": "192.0.2.1", "port": 443},
        ]
    )

    assert rendered.splitlines() == [
        "f004|negative|other port",
        "f005|negative|other host",
        "f006|negative|host only",
        "f007|negative|port only",
        "f008|negative|no endpoint",
        "f009|finding|positive",
        "f010|negative|latest",
        "...|negative|3 earlier negative facts collapsed for 192.0.2.1:443",
    ]


def test_legacy_fact_ids_still_render_without_descriptions() -> None:
    assert format_fact_ids(["origin", "f001"]) == "origin|other|\nf001|other|"
    assert format_fact_ids([]) == ""


@pytest.mark.parametrize("endpoint", [{}, {"ip": "192.0.2.1"}, {"port": 443}, {"ip": "", "port": 443}])
def test_repeated_negatives_without_complete_endpoint_are_never_folded(endpoint: dict) -> None:
    rendered = format_fact_ids(
        [{"id": fact_id, "kind": "negative", "summary": "No result", **endpoint} for fact_id in ("f001", "f002", "f003", "f004")]
    )

    assert rendered.splitlines() == [
        "f001|negative|No result",
        "f002|negative|No result",
        "f003|negative|No result",
        "f004|negative|No result",
    ]
