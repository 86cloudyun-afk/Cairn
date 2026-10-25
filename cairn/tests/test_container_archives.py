from __future__ import annotations

import io
import stat
import tarfile
from types import SimpleNamespace

import pytest

from cairn.dispatcher.runtime.containers import ContainerManager


def test_text_file_archive_extracts_below_existing_top_level_directory() -> None:
    archive_path, payload = ContainerManager._text_file_archive(
        "/tmp/cairn-prompts/reason_execute-123/graph.yaml",
        "facts:\n- id: f001\n",
    )

    assert archive_path == "/tmp"
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = archive.getnames()
        assert names == [
            "cairn-prompts",
            "cairn-prompts/reason_execute-123",
            "cairn-prompts/reason_execute-123/graph.yaml",
        ]
        assert "tmp" not in names
        graph = archive.extractfile("cairn-prompts/reason_execute-123/graph.yaml")
        assert graph is not None
        assert graph.read() == b"facts:\n- id: f001\n"


@pytest.mark.parametrize("path", ["relative.txt", "/", "/tmp/../escape.txt"])
def test_text_file_archive_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        ContainerManager._text_file_archive(path, "content")


def test_private_text_file_archive_extracts_with_restricted_permissions(tmp_path) -> None:
    _, payload = ContainerManager._text_file_archive(
        "/tmp/cairn-prompts/explore-123/artifacts.json", '[{"value":"test-secret"}]', private=True,
    )
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        archive.extractall(tmp_path, filter="data")
    snapshot = tmp_path / "cairn-prompts" / "explore-123" / "artifacts.json"
    assert snapshot.read_text() == '[{"value":"test-secret"}]'
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700


def test_private_container_snapshot_belongs_to_configured_exec_user() -> None:
    captured = []

    class Container:
        def exec_run(self, command):
            return SimpleNamespace(exit_code=0, output=b"12345\n23456\n")
        def put_archive(self, path, payload):
            captured.append(payload)
            return True

    manager = ContainerManager.__new__(ContainerManager)
    manager._require_container = lambda _name: Container()
    manager.write_text_file("worker", "/tmp/cairn-prompts/task/artifacts.json", "[]", private=True)

    with tarfile.open(fileobj=io.BytesIO(captured[0])) as archive:
        assert [(entry.uid, entry.gid, entry.mode) for entry in archive.getmembers()] == [
            (12345, 23456, 0o700), (12345, 23456, 0o700), (12345, 23456, 0o600),
        ]


@pytest.mark.parametrize("exit_code,output", [(1, b""), (0, b"unknown\n23456\n")])
def test_private_container_snapshot_fails_closed_if_exec_user_is_unknown(exit_code, output) -> None:
    captured = []

    class Container:
        def exec_run(self, command):
            return SimpleNamespace(exit_code=exit_code, output=output)
        def put_archive(self, path, payload):
            captured.append(payload)
            return True

    manager = ContainerManager.__new__(ContainerManager)
    manager._require_container = lambda _name: Container()
    with pytest.raises(RuntimeError, match="snapshot owner"):
        manager.write_text_file("worker", "/tmp/cairn-prompts/task/artifacts.json", "[]", private=True)
    assert captured == []

