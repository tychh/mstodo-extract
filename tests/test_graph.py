import json
from pathlib import Path

import httpx
import pytest

from mstodo_extract.errors import ExportError
from mstodo_extract.graph import BASE_URL, GraphClient


def graph_for(handler, sleeps=None):
    return GraphClient(
        "secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_full_nextlink_is_followed_even_after_an_empty_page():
    next_link = BASE_URL + "lists?$skiptoken=opaque%2Ftoken%3D&$top=1"
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer secret-token"
        if len(requests) == 1:
            return httpx.Response(200, json={"value": [], "@odata.nextLink": next_link})
        assert str(request.url) == next_link
        return httpx.Response(200, json={"value": [{"id": "list-id"}]})

    graph = graph_for(handle)
    assert list(graph.collection("lists", "lists", params={"$top": "1"})) == [{"id": "list-id"}]
    assert len(requests) == 2


def test_response_body_is_read_even_with_zero_content_length():
    graph = graph_for(
        lambda _: httpx.Response(
            200, content=b'{"value":[{"id":"list"}]}', headers={"Content-Length": "0"}
        )
    )
    assert list(graph.collection("lists", "lists")) == [{"id": "list"}]


@pytest.mark.parametrize("payload", [{}, {"value": None}, {"value": {}}, {"value": "[]"}])
def test_bad_value_is_not_treated_as_an_empty_collection(payload):
    graph = graph_for(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(ExportError, match="non-array 'value'"):
        list(graph.collection("lists", "lists"))


def test_non_json_response_has_a_safe_error():
    graph = graph_for(lambda _: httpx.Response(200, text="Private task or token here"))
    with pytest.raises(ExportError, match="not valid JSON") as error:
        graph.get_json("lists", "lists")
    assert "Private" not in str(error.value)


def test_empty_success_body_is_retried_instead_of_becoming_an_empty_list():
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"value": [{"id": "recovered"}]})

    assert list(graph_for(handle).collection("lists", "lists")) == [{"id": "recovered"}]
    assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 302])
def test_errors_do_not_echo_credentials_or_response_contents(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "secret-token private title"}})

    with pytest.raises(ExportError, match=f"HTTP {status}") as error:
        graph_for(handle).get_json("lists", "lists")
    assert "secret-token" not in str(error.value)
    assert "private title" not in str(error.value)
    assert len(requests) == 1


def test_throttling_and_transient_server_errors_are_retried():
    statuses = iter([429, 503, 200])
    sleeps = []

    def handle(_):
        status = next(statuses)
        return httpx.Response(status, headers={"Retry-After": "2"}, json={"value": []})

    assert list(graph_for(handle, sleeps).collection("lists", "lists")) == []
    assert sleeps == [2, 2]


def test_retry_limit():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(503)

    sleeps = []
    with pytest.raises(ExportError, match="HTTP 503"):
        graph_for(handle, sleeps).get_json("lists", "lists")
    assert len(requests) == 4
    assert sleeps == [1, 2, 4]


def test_network_failure_is_retried_without_echoing_the_exception():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectError("secret-token", request=request)

    with pytest.raises(ExportError, match="after 4 attempts") as error:
        graph_for(handle).get_json("lists", "lists")
    assert len(calls) == 4
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize("link", [BASE_URL + "lists", "https://other.example/lists"])
def test_pagination_loop_or_untrusted_host_is_rejected(link):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"value": [], "@odata.nextLink": link})

    with pytest.raises(ExportError):
        list(graph_for(handle).collection("lists", "lists"))
    assert len(requests) == 1


def test_duplicate_items_across_pages_are_rejected():
    calls = []

    def handle(request):
        calls.append(request)
        payload = {"value": [{"id": "same"}]}
        if len(calls) == 1:
            payload["@odata.nextLink"] = BASE_URL + "lists?$skip=1"
        return httpx.Response(200, json=payload)

    with pytest.raises(ExportError, match="duplicate item"):
        list(graph_for(handle).collection("lists", "lists"))


def test_download_restarts_after_a_partial_network_read(tmp_path: Path):
    attempts = []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("connection interrupted")

    def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(200, stream=Interrupted())
        return httpx.Response(200, content=b"complete file")

    path = tmp_path / "attachment"
    size, digest = graph_for(handle).download("lists/l/tasks/t/attachments/a/$value", path)
    assert path.read_bytes() == b"complete file"
    assert size == len(b"complete file")
    assert len(digest) == 64
    assert len(attempts) == 2


def test_resource_ids_are_encoded_as_path_segments():
    assert GraphClient.task_path("list/id=", "task/id=") == "lists/list%2Fid%3D/tasks/task%2Fid%3D"


def test_malformed_pagination_port_is_an_actionable_error():
    graph = graph_for(lambda _: httpx.Response(200, json={"value": []}))
    with pytest.raises(ExportError, match="invalid pagination/download port"):
        list(graph.collection("https://graph.microsoft.com:invalid/v1.0/me/todo/lists", "lists"))


def test_file_download_retries_throttling_and_supports_empty_files(tmp_path):
    attempts = []
    sleeps = []

    def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"})
        return httpx.Response(200, content=b"")

    path = tmp_path / "empty"
    size, digest = graph_for(handle, sleeps).download("lists/l/tasks/t/attachments/a/$value", path)
    assert size == 0
    assert path.read_bytes() == b""
    assert digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sleeps == [0.5]


def test_unknown_streaming_error_is_never_repaired():
    text = (
        '{"value":[{"id":"task"},{"error":{"code":"InternalServerError","message":"Other failure"}}'
    )
    graph = graph_for(lambda _: httpx.Response(200, text=text))
    with pytest.raises(ExportError, match="not valid JSON"):
        list(
            graph.collection(
                "lists/l/tasks", "tasks", params={"$top": "1"}, recover_linked_resources=True
            )
        )


def test_link_recovery_refuses_a_task_with_incomplete_notes():
    message = (
        "Invalid object within the collection response from workload for navigation property "
        "linkedResources with declaring type microsoft.graph.todoTask. "
        "Expected a JObject, but got Jtoken type - Null"
    )
    text = '{"value":[{"id":"task","linkedResources":[' + json.dumps(
        {"error": {"code": "InternalServerError", "message": message}}
    )
    graph = graph_for(lambda _: httpx.Response(200, text=text))
    with pytest.raises(ExportError, match="structural task data is incomplete"):
        list(
            graph.collection(
                "lists/l/tasks", "tasks", params={"$top": "1"}, recover_linked_resources=True
            )
        )
