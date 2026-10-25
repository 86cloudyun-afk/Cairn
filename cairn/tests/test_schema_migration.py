from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cairn.server import db


LEGACY_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);
CREATE TABLE facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);
CREATE TABLE intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);
"""


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_enabled, created_at) VALUES (?, ?, ?, ?)",
            ("proj_001", "legacy", 0, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", "proj_001", "  Existing\n\t detailed  evidence\r\n" + "x" * 150),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            ("i001", "proj_001", "existing work", "reason", "2026-01-01T00:00:00Z"),
        )


def test_runtime_migration_preserves_legacy_records_and_backfills_fact_summary(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    monkeypatch.setattr(db, "_db_path", None)

    db.configure(path)

    with db.get_conn() as conn:
        project = conn.execute("SELECT * FROM projects").fetchone()
        fact = conn.execute("SELECT * FROM facts").fetchone()
        intent = conn.execute("SELECT * FROM intents").fetchone()
    assert project["title"] == "legacy"
    assert project["bootstrap_enabled"] == 0
    assert json.loads(project["plan"]) == {}
    assert fact["description"] == "  Existing\n\t detailed  evidence\r\n" + "x" * 150
    assert fact["summary"] == "Existing detailed evidence " + "x" * 93
    assert fact["kind"] == "other"
    assert (fact["ip"], fact["port"], fact["url"], fact["confidence"]) == (None, None, None, None)
    assert intent["description"] == "existing work"
    assert (json.loads(intent["depends_on"]), intent["priority"], intent["session_key"]) == ([], 0, None)


def test_restart_keeps_saved_plan_metadata_dependencies_and_artifacts(tmp_path, monkeypatch) -> None:
    path = tmp_path / "restart.db"
    _legacy_database(path)
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET plan = ?", ('{"current_stage":1,"stages":["one","two"]}',))
        conn.execute("UPDATE facts SET summary = 'chosen summary', kind = 'endpoint', ip = '127.0.0.1', port = 8080, confidence = 90")
        conn.execute("UPDATE intents SET depends_on = '[\"i000\"]', priority = 9, session_key = 'topic' ")
        conn.execute(
            "INSERT INTO artifacts (id, project_id, fact_id, kind, name, value, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a001", "proj_001", "origin", "account", "login", '{"user":"saved"}', "2026-01-01T00:00:00Z"),
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        assert json.loads(conn.execute("SELECT plan FROM projects").fetchone()["plan"])["current_stage"] == 1
        fact = conn.execute("SELECT * FROM facts").fetchone()
        assert (fact["summary"], fact["kind"], fact["ip"], fact["port"], fact["confidence"]) == (
            "chosen summary", "endpoint", "127.0.0.1", 8080, 90
        )
        intent = conn.execute("SELECT * FROM intents").fetchone()
        assert (intent["depends_on"], intent["priority"], intent["session_key"]) == ('["i000"]', 9, "topic")
        assert json.loads(conn.execute("SELECT value FROM artifacts").fetchone()["value"]) == {"user": "saved"}


def test_artifact_foreign_keys_reject_other_project_fact_and_cascade(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "fresh.db")
    with db.get_conn() as conn:
        for project_id in ("proj_001", "proj_002"):
            conn.execute(
                "INSERT INTO projects (id, title, created_at) VALUES (?, 'test', '2026-01-01T00:00:00Z')",
                (project_id,),
            )
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('f001', 'proj_001', 'found')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO artifacts (id, project_id, fact_id, kind, name, created_at) VALUES ('bad', 'proj_002', 'f001', 'token', 'bad', 'now')"
            )
        conn.execute(
            "INSERT INTO artifacts (id, project_id, fact_id, kind, name, created_at) VALUES ('a001', 'proj_001', 'f001', 'token', 'login', 'now')"
        )
        conn.execute(
            "INSERT INTO artifacts (id, project_id, kind, name, created_at) VALUES ('a002', 'proj_001', 'session', 'browser', 'now')"
        )
        conn.execute("DELETE FROM facts WHERE id = 'f001' AND project_id = 'proj_001'")
        assert [row["id"] for row in conn.execute("SELECT id FROM artifacts")] == ["a002"]
        conn.execute("DELETE FROM projects WHERE id = 'proj_001'")
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_standalone_sql_migrates_original_schema_and_runtime_can_reopen_it(tmp_path, monkeypatch) -> None:
    path = tmp_path / "manual.db"
    _legacy_database(path)
    migration = Path(__file__).parents[1] / "migrations" / "001_plans_artifacts.sql"

    with sqlite3.connect(path) as conn:
        conn.executescript(migration.read_text())
        assert json.loads(conn.execute("SELECT plan FROM projects").fetchone()[0]) == {}
        fact = conn.execute("SELECT description, summary FROM facts").fetchone()
        assert fact[0] == "  Existing\n\t detailed  evidence\r\n" + "x" * 150
        assert fact[1] == "Existing detailed evidence " + "x" * 93
        conn.execute(
            "INSERT INTO artifacts (id, project_id, kind, name, created_at) VALUES ('a001', 'proj_001', 'endpoint', 'api', 'now')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


@pytest.mark.parametrize("migration_kind", ["runtime", "standalone"])
def test_summary_backfill_collapses_unicode_whitespace(tmp_path, monkeypatch, migration_kind) -> None:
    path = tmp_path / "unicode.db"
    _legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE facts SET description = ?", ("Alpha\u00a0\u2003Beta\u2028Gamma\x1cDelta",))
    if migration_kind == "runtime":
        monkeypatch.setattr(db, "_db_path", None)
        db.configure(path)
    else:
        migration = Path(__file__).parents[1] / "migrations" / "001_plans_artifacts.sql"
        with sqlite3.connect(path) as conn:
            conn.executescript(migration.read_text())
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT summary FROM facts").fetchone()[0] == "Alpha Beta Gamma Delta"
