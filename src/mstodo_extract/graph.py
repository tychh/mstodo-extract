"""Read-only Microsoft Graph client with opaque pagination and bounded retries."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .errors import ExportError, NullLinkedResourceError

BASE_URL = "https://graph.microsoft.com/v1.0/me/todo/"
RETRY_STATUSES = {429, 500, 502, 503, 504}


def _null_link_error(text: str) -> tuple[int, dict[str, Any]] | None:
    for match in reversed(list(re.finditer(r'\{\s*"error"\s*:', text))):
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[match.start() :])
            error = payload["error"]
            message = error.get("message", "")
            if (
                error.get("code") == "InternalServerError"
                and "navigation property linkedResources" in message
                and "declaring type microsoft.graph.todoTask" in message
                and "Jtoken type - Null" in message
            ):
                return match.start(), error
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def _recover_single_task(text: str, offset: int, error: dict[str, Any]) -> dict[str, Any]:
    """Close the known interrupted navigation array, never arbitrary malformed JSON.

    Graph has already serialized structural task properties when it starts navigation
    properties. Recovery is allowed only for a one-task page with a complete core record.
    The original streaming error is preserved on the source task.
    """
    prefix = text[:offset].rstrip()
    if prefix.endswith(":"):
        prefix += "null"
    elif prefix.endswith(","):
        prefix = prefix[:-1]
    stack: list[str] = []
    in_string = escaped = False
    try:
        for char in prefix:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "{[":
                stack.append(char)
            elif char in "}]":
                expected = "{" if char == "}" else "["
                if stack.pop() != expected:
                    raise ValueError("Mismatched delimiters")
        if in_string:
            raise ValueError("Interrupted string")
        prefix += "".join("}" if char == "{" else "]" for char in reversed(stack))
        page = json.loads(prefix)
        values = page.get("value")
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError("Not a single-task page")
        task = values[0]
        required = {
            "id",
            "title",
            "status",
            "importance",
            "isReminderOn",
            "createdDateTime",
            "lastModifiedDateTime",
            "hasAttachments",
            "categories",
            "body",
            "linkedResources",
        }
        if not isinstance(task, dict) or not required.issubset(task):
            raise ValueError("Incomplete structural task data")
        if not isinstance(task["body"], dict) or not {"content", "contentType"}.issubset(
            task["body"]
        ):
            raise ValueError("Incomplete notes")
        if not isinstance(task["linkedResources"], list):
            raise ValueError("Not an interrupted linkedResources array")
        task["linkedResources"].append(None)
        task.setdefault("_exportWarnings", []).append(
            {
                "code": "GraphNullLinkedResource",
                "message": error["message"],
                "detail": (
                    "Task properties recovered. "
                    "Graph stopped returning linkedResources at a null entry."
                ),
            }
        )
        page["_recoveredLinkedResources"] = True
        return page
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ExportError(
            "Graph task recovery refused: structural task data is incomplete"
        ) from None


class GraphClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _url(url: str) -> str:
        url = urljoin(BASE_URL, url)
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise ExportError("Graph returned an invalid pagination/download port") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "graph.microsoft.com"
            or port not in (None, 443)
            or parsed.username
            or parsed.password
            or not parsed.path.startswith("/v1.0/me/todo/")
            or parsed.fragment
        ):
            raise ExportError("Graph returned an invalid or unexpected pagination/download URL")
        return url

    @staticmethod
    def _delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None and (retry_after := response.headers.get("Retry-After")):
            try:
                return max(0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    return max(0, (retry_at - datetime.now(UTC)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return float(2**attempt)

    @staticmethod
    def _status(response: httpx.Response, operation: str) -> None:
        if response.status_code == 200:
            return
        hint = {
            401: "Obtain a fresh Microsoft Graph access token",
            403: "Check delegated Tasks.Read or Tasks.ReadWrite permissions and list access",
            404: "The requested item no longer exists; retry the export",
            429: "Graph is throttling requests; retry later",
        }.get(response.status_code, "Retry the export or check Microsoft Graph availability")
        raise ExportError(f"Graph {operation}: HTTP {response.status_code}. {hint}.")

    def get_json(
        self,
        url: str,
        operation: str,
        *,
        params: dict[str, str] | None = None,
        recover_linked_resources: bool = False,
    ) -> dict[str, Any]:
        url = self._url(url)
        for attempt in range(4):
            try:
                response = self._client.get(url, params=params, headers=self._headers)
            except httpx.TransportError:
                if attempt == 3:
                    raise ExportError(
                        f"Graph {operation}: network request failed after 4 attempts"
                    ) from None
                self._sleep(self._delay(None, attempt))
                continue
            if response.status_code in RETRY_STATUSES and attempt < 3:
                self._sleep(self._delay(response, attempt))
                continue
            self._status(response, operation)
            try:
                data = response.json()
            except ValueError:
                if broken_link := _null_link_error(response.text):
                    if recover_linked_resources:
                        return _recover_single_task(response.text, *broken_link)
                    raise NullLinkedResourceError(
                        "Graph tasks: a null linkedResources entry interrupted serialization"
                    ) from None
                if attempt < 3:
                    self._sleep(self._delay(response, attempt))
                    continue
                raise ExportError(
                    f"Graph {operation}: response is not valid JSON after 4 attempts"
                ) from None
            if not isinstance(data, dict):
                raise ExportError(f"Graph {operation}: expected a JSON object")
            return data
        raise AssertionError("unreachable")

    def collection(
        self,
        url: str,
        operation: str,
        *,
        params: dict[str, str] | None = None,
        recover_linked_resources: bool = False,
    ) -> Iterator[dict[str, Any]]:
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        next_url: str | None = self._url(url)
        initial_url = next_url
        initial_params = dict(params or {})
        if recover_linked_resources and initial_params.get("$top") != "1":
            raise ExportError("Linked-resource recovery requires single-task pages")
        while next_url:
            if next_url in seen_urls:
                raise ExportError(f"Graph {operation}: repeated pagination link")
            seen_urls.add(next_url)
            data = self.get_json(
                next_url,
                operation,
                params=params,
                recover_linked_resources=recover_linked_resources,
            )
            params = None  # The server's nextLink already contains the complete query.
            values = data.get("value")
            if not isinstance(values, list):
                raise ExportError(f"Graph {operation}: missing or non-array 'value' field")
            for item in values:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                ):
                    raise ExportError(f"Graph {operation}: collection item is missing a valid ID")
                if item["id"] in seen_ids:
                    raise ExportError(
                        f"Graph {operation}: duplicate item across pages; retry export"
                    )
                seen_ids.add(item["id"])
                yield item
            next_link = data.get("@odata.nextLink")
            if data.get("_recoveredLinkedResources") and not next_link:
                # A streaming error can hide the footer. An explicit $skip probe verifies
                # exhaustion instead of silently treating the interrupted page as the end.
                recovery_params = {**initial_params, "$skip": str(len(seen_ids))}
                next_link = str(httpx.URL(initial_url).copy_merge_params(recovery_params))
            if next_link is not None and (not isinstance(next_link, str) or not next_link):
                raise ExportError(f"Graph {operation}: invalid '@odata.nextLink'")
            next_url = self._url(next_link) if next_link else None

    @staticmethod
    def task_path(list_id: str, task_id: str | None = None) -> str:
        path = f"lists/{quote(list_id, safe='')}/tasks"
        if task_id is not None:
            path += f"/{quote(task_id, safe='')}"
        return path

    def download(self, url: str, path: Path) -> tuple[int, str]:
        """Stream one file to a private staging path; restart cleanly on a failed read."""
        url = self._url(url)
        for attempt in range(4):
            try:
                with self._client.stream("GET", url, headers=self._headers) as response:
                    if response.status_code in RETRY_STATUSES and attempt < 3:
                        delay = self._delay(response, attempt)
                    else:
                        self._status(response, "attachment download")
                        digest = hashlib.sha256()
                        size = 0
                        with path.open("wb") as output:
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                        return size, digest.hexdigest()
            except httpx.TransportError:
                if attempt == 3:
                    raise ExportError(
                        "Graph attachment download: network failed after 4 attempts"
                    ) from None
                delay = self._delay(None, attempt)
            self._sleep(delay)
        raise AssertionError("unreachable")
