import hashlib
import json
from threading import Barrier

import httpx
import pytest

from mstodo_extract import cli, exporter
from mstodo_extract.errors import ExportError
from mstodo_extract.graph import BASE_URL, GraphClient

FILE_BYTES = b"binary\x00attachment\xff"


def test_task_details_of_one_large_list_share_request_workers():
    simultaneous_requests = Barrier(2)

    def handle(request):
        path = request.url.path.removeprefix("/v1.0/me/todo/")
        if path == "lists":
            return httpx.Response(200, json={"value": [{"id": "list"}]})
        if path == "lists/list/tasks":
            return httpx.Response(200, json={"value": [{"id": "first"}, {"id": "second"}]})
        if path.endswith("/checklistItems"):
            simultaneous_requests.wait(timeout=5)
            task_id = path.split("/")[-2]
            return httpx.Response(200, json={"value": [{"id": task_id + "-step"}]})
        raise AssertionError("Unexpected endpoint")

    graph = GraphClient(
        "test", client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=lambda _: None
    )
    progress = []
    source = exporter.collect(
        graph, True, task_progress=lambda done, total: progress.append((done, total))
    )
    assert [task["id"] for task in source[0]["tasks"]] == ["first", "second"]
    assert [task["checklistItems"][0]["id"] for task in source[0]["tasks"]] == [
        "first-step",
        "second-step",
    ]
    assert progress == [(1, 2), (2, 2)]


def fixture_graph(*, fail_download=False, fail_steps=False, bad_size=False, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(request)
        path = request.url.path.removeprefix("/v1.0/me/todo/")
        if path == "lists":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "list", "displayName": "List", "unknownListField": {"keep": True}},
                        {"id": "empty", "displayName": "Empty"},
                    ]
                },
            )
        if path == "lists/empty/tasks":
            return httpx.Response(200, json={"value": []})
        if path == "lists/list/tasks":
            if "$skiptoken" in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": "done",
                                "title": "Done",
                                "status": "completed",
                                "hasAttachments": False,
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "task",
                            "title": "Task",
                            "status": "notStarted",
                            "hasAttachments": True,
                            "body": {"content": "Note", "contentType": "text"},
                            "unknownTaskField": ["preserve", "this"],
                        }
                    ],
                    "@odata.nextLink": BASE_URL + "lists/list/tasks?$skiptoken=opaque%2Fvalue",
                },
            )
        if path == "lists/list/tasks/task/checklistItems":
            if fail_steps:
                return httpx.Response(200, json={"unexpected": []})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "step",
                            "displayName": "Step",
                            "isChecked": True,
                        }
                    ]
                },
            )
        if path == "lists/list/tasks/done/checklistItems":
            return httpx.Response(200, json={"value": []})
        if path == "lists/list/tasks/task/attachments":
            if "$skiptoken" in request.url.params:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "file",
                            "name": "../../private\\same.bin",
                            "contentType": "application/octet-stream",
                            "size": 1 if bad_size else len(FILE_BYTES),
                        }
                    ],
                    "@odata.nextLink": BASE_URL
                    + "lists/list/tasks/task/attachments?$skiptoken=last",
                },
            )
        if path == "lists/list/tasks/task/attachments/file/$value":
            if fail_download:
                return httpx.Response(403)
            return httpx.Response(200, content=FILE_BYTES)
        raise AssertionError(f"Unexpected test endpoint: {path}")

    return GraphClient(
        "test-token",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        sleep=lambda _: None,
    )


def test_complete_export_with_real_file_bytes_and_source_companion(tmp_path):
    output = tmp_path / "todo.json"
    counts = exporter.export(fixture_graph(), output, completed=True)
    assert counts == {
        "lists": 2,
        "tasks": 2,
        "steps": 1,
        "attachments": 1,
        "attachment_warnings": 0,
        "warnings": 0,
        "graph_warnings": 0,
    }
    source = json.loads((tmp_path / "todo.source.json").read_text())
    assert source[0]["unknownListField"] == {"keep": True}
    assert source[0]["tasks"][0]["unknownTaskField"] == ["preserve", "this"]
    assert source[1]["tasks"] == []
    metadata = source[0]["tasks"][0]["attachments"][0]
    attachment_file = tmp_path / metadata["localPath"]
    assert attachment_file.is_relative_to(tmp_path)
    assert attachment_file.read_bytes() == FILE_BYTES
    assert metadata["sha256"] == hashlib.sha256(FILE_BYTES).hexdigest()
    manifest = json.loads((tmp_path / "todo.attachments" / "manifest.json").read_text())
    assert manifest[0]["listId"] == "list"
    assert manifest[0]["taskId"] == "task"
    assert manifest[0]["attachmentId"] == "file"
    assert (tmp_path / "todo.attachments" / manifest[0]["path"]).read_bytes() == FILE_BYTES
    backup = json.loads(output.read_text())
    task = next(
        task for task in backup["data"]["task"]["entities"].values() if task["title"] == "Task"
    )
    assert task["attachments"][0]["path"] == str(attachment_file.resolve())
    assert task["attachments"][0]["type"] == "FILE"
    assert not list(tmp_path.glob(".mstodo-*"))


def test_raw_json_and_completed_filter(tmp_path):
    requests = []
    output = tmp_path / "raw.json"
    counts = exporter.export(fixture_graph(requests=requests), output, output_type="json")
    source = json.loads(output.read_text())
    assert [task["id"] for task in source[0]["tasks"]] == ["task"]
    assert counts["tasks"] == 1
    assert not (tmp_path / "raw.source.json").exists()
    first_tasks = next(request for request in requests if request.url.path.endswith("/list/tasks"))
    assert first_tasks.url.params["$filter"] == "status ne 'completed'"
    assert all(not request.url.path.endswith("done/checklistItems") for request in requests)


@pytest.mark.parametrize("failure", ["fail_download", "fail_steps"])
def test_failure_never_publishes_incomplete_json_or_files(tmp_path, failure):
    output = tmp_path / "todo.json"
    with pytest.raises(ExportError):
        exporter.export(fixture_graph(**{failure: True}), output)
    assert list(tmp_path.iterdir()) == []


def test_attachment_size_discrepancy_preserves_bytes_and_original_metadata(tmp_path):
    output = tmp_path / "todo.json"
    counts = exporter.export(fixture_graph(bad_size=True), output)
    assert counts["attachment_warnings"] == 1
    source = json.loads((tmp_path / "todo.source.json").read_text())
    attachment = source[0]["tasks"][0]["attachments"][0]
    assert attachment["size"] == 1
    assert attachment["downloadedSize"] == len(FILE_BYTES)
    assert attachment["_exportWarnings"][0]["code"] == "GraphAttachmentSizeMismatch"
    assert (tmp_path / attachment["localPath"]).read_bytes() == FILE_BYTES
    manifest = json.loads((tmp_path / "todo.attachments" / "manifest.json").read_text())
    assert manifest[0]["graphSize"] == 1
    assert manifest[0]["size"] == len(FILE_BYTES)
    assert manifest[0]["sha256"] == hashlib.sha256(FILE_BYTES).hexdigest()


@pytest.mark.parametrize("name", ["todo.json", "todo.source.json", "todo.attachments"])
def test_existing_outputs_are_preserved_without_making_api_requests(tmp_path, name):
    existing = tmp_path / name
    existing.write_bytes(b"keep existing data")
    requests = []
    with pytest.raises(ExportError, match="already exists"):
        exporter.export(fixture_graph(requests=requests), tmp_path / "todo.json")
    assert existing.read_bytes() == b"keep existing data"
    assert requests == []


def test_failed_publication_rolls_back_only_this_export(tmp_path, monkeypatch):
    original = exporter._publish_file

    def fail_primary(staged, target):
        if staged.name == "primary.json":
            # Simulate another writer winning the primary output path.
            target.write_text("other writer")
        original(staged, target)

    monkeypatch.setattr(exporter, "_publish_file", fail_primary)
    with pytest.raises(FileExistsError):
        exporter.export(fixture_graph(), tmp_path / "todo.json")
    assert [file.name for file in tmp_path.iterdir()] == ["todo.json"]
    assert (tmp_path / "todo.json").read_text() == "other writer"


def test_custom_attachment_directory_can_be_outside_the_json_directory(tmp_path):
    output = tmp_path / "json" / "todo.json"
    attachments = tmp_path / "files"
    exporter.export(fixture_graph(), output, attachments_dir=attachments)
    manifest = json.loads((attachments / "manifest.json").read_text())
    assert (attachments / manifest[0]["path"]).read_bytes() == FILE_BYTES
    source = json.loads((output.parent / "todo.source.json").read_text())
    relative = source[0]["tasks"][0]["attachments"][0]["localPath"]
    assert (output.parent / relative).resolve().is_relative_to(attachments)


def test_token_file_overrides_environment_and_accepts_bom_and_bearer(tmp_path, monkeypatch):
    token_file = tmp_path / "token.txt"
    token_file.write_text("\ufeffBearer from-file\n")
    monkeypatch.setenv("MSTODO_EXTRACT_TOKEN", "from-env")
    args = cli.parser().parse_args(["export", "--token-file", str(token_file)])
    assert cli.read_token(args) == "from-file"
    args = cli.parser().parse_args(["export", "--token", "from-argument"])
    assert cli.read_token(args) == "from-argument"


def test_missing_token_has_an_actionable_error(monkeypatch, capsys):
    monkeypatch.delenv("MSTODO_EXTRACT_TOKEN", raising=False)
    assert cli.main(["export"]) == 1
    assert "Provide a valid token" in capsys.readouterr().err


def test_cli_runs_export_and_reports_counts_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "GraphClient", lambda _: fixture_graph())
    output = tmp_path / "todo.json"
    assert (
        cli.main(["export", "--token", "test-token", "--completed", "--output", str(output)]) == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "2 lists, 2 tasks, 1 steps, 1 files" in captured.err
    assert "test-token" not in captured.err
    assert output.exists()


def test_cli_refuses_mutually_exclusive_token_sources(tmp_path):
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(
            ["export", "--token", "test-token", "--token-file", str(tmp_path / "token")]
        )
    assert error.value.code == 2


def test_graph_null_link_streaming_error_recovers_all_tasks_without_duplicates(tmp_path):
    message = (
        "Invalid object within the collection response from workload for navigation property "
        "linkedResources with declaring type microsoft.graph.todoTask. "
        "Expected a JObject, but got Jtoken type - Null"
    )
    tasks = [
        {
            "id": identifier,
            "title": identifier,
            "status": "notStarted",
            "importance": "normal",
            "createdDateTime": "2026-10-01T00:00:00Z",
            "lastModifiedDateTime": "2026-10-02T00:00:00Z",
            "isReminderOn": False,
            "hasAttachments": False,
            "categories": [],
            "body": {"content": 'Keep notes: "quoted" [braces] \\ АБВ', "contentType": "text"},
        }
        for identifier in ("before", "broken", "after")
    ]

    def broken_page(single):
        prefix = '{"value":['
        if not single:
            prefix += json.dumps(tasks[0]) + ","
        prefix += json.dumps(tasks[1])[:-1] + ',"linkedResources":['
        return prefix + json.dumps({"error": {"code": "InternalServerError", "message": message}})

    def handle(request):
        path = request.url.path.removeprefix("/v1.0/me/todo/")
        if path == "lists":
            return httpx.Response(200, json={"value": [{"id": "list", "displayName": "List"}]})
        if path.endswith("checklistItems"):
            return httpx.Response(200, json={"value": []})
        assert path == "lists/list/tasks"
        assert request.url.params["$filter"] == "status ne 'completed'"
        if request.url.params.get("$top") != "1":
            return httpx.Response(200, text=broken_page(False))
        skip = int(request.url.params.get("$skip", "0"))
        if skip == 0:
            next_link = str(
                httpx.URL(BASE_URL + path).copy_merge_params(
                    {"$top": "1", "$skip": "1", "$filter": "status ne 'completed'"}
                )
            )
            return httpx.Response(200, json={"value": [tasks[0]], "@odata.nextLink": next_link})
        if skip == 1:
            return httpx.Response(200, text=broken_page(True))  # No footer: must probe skip=2.
        assert skip == 2
        return httpx.Response(200, json={"value": [tasks[2]]})

    graph = GraphClient(
        "test", client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=lambda _: None
    )
    output = tmp_path / "recovered.json"
    counts = exporter.export(graph, output)
    assert counts["tasks"] == 3
    assert counts["graph_warnings"] == 1
    source = json.loads((tmp_path / "recovered.source.json").read_text())
    assert [task["id"] for task in source[0]["tasks"]] == ["before", "broken", "after"]
    recovered = source[0]["tasks"][1]
    assert recovered["body"] == tasks[1]["body"]
    assert recovered["linkedResources"] == [None]
    assert recovered["_exportWarnings"][0]["code"] == "GraphNullLinkedResource"
    native = json.loads(output.read_text())
    assert len(native["data"]["task"]["ids"]) == 3
