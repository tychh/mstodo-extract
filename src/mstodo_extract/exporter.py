"""Collect source data, stage downloads, and publish a complete export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import quote

from .errors import ExportError, NullLinkedResourceError
from .graph import GraphClient
from .superproductivity import build_backup, validate_backup


def collect(
    graph: GraphClient,
    completed: bool,
    progress: Callable[[int, int], None] | None = None,
    task_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    lists = list(graph.collection("lists", "lists"))
    abort = Event()

    def read_entry(entry: dict[str, Any]) -> dict[str, Any]:
        params = None if completed else {"$filter": "status ne 'completed'"}

        def read_tasks(single: bool = False) -> list[dict[str, Any]]:
            query = {**(params or {}), "$top": "1"} if single else params
            result = []
            for task in graph.collection(
                graph.task_path(entry["id"]),
                "tasks",
                params=query,
                recover_linked_resources=single,
            ):
                if abort.is_set():
                    raise ExportError("Export canceled after another request failed")
                result.append(task)
            return result

        try:
            tasks = read_tasks()
        except NullLinkedResourceError:
            # Restart the list: a broken larger page may hide tasks after the bad navigation.
            tasks = read_tasks(single=True)
        if not completed:
            tasks = [task for task in tasks if task.get("status") != "completed"]
        return {**entry, "tasks": tasks}

    results: list[dict[str, Any] | None] = [None] * len(lists)
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {executor.submit(read_entry, entry): index for index, entry in enumerate(lists)}
        try:
            for count, future in enumerate(as_completed(pending), 1):
                results[pending[future]] = future.result()
                if progress:
                    progress(count, len(lists))
        except BaseException:
            abort.set()
            for future in pending:
                future.cancel()
            raise
    source = [entry for entry in results if entry is not None]

    def read_details(list_id: str, task: dict[str, Any]) -> None:
        if abort.is_set():
            raise ExportError("Export canceled after another request failed")
        path = graph.task_path(list_id, task["id"])
        task["checklistItems"] = list(graph.collection(path + "/checklistItems", "steps"))
        task["attachments"] = (
            list(graph.collection(path + "/attachments", "attachments"))
            if task.get("hasAttachments")
            else []
        )

    # Share the same four request slots across tasks, including a single large list.
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending_details = [
            executor.submit(read_details, entry["id"], task)
            for entry in source
            for task in entry["tasks"]
        ]
        try:
            for count, future in enumerate(as_completed(pending_details), 1):
                future.result()
                if task_progress:
                    task_progress(count, len(pending_details))
        except BaseException:
            abort.set()
            for future in pending_details:
                future.cancel()
            raise
    return source


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")
    name = name.encode("utf-8")[:120].decode("utf-8", errors="ignore")
    return name.strip(" .") or "attachment"


def _hash_id(identifier: str) -> str:
    return hashlib.sha256(identifier.encode()).hexdigest()[:16]


def _write_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    path.chmod(0o600)


def _publish_file(staged: Path, destination: Path) -> None:
    # Hard-linking atomically creates a complete file and refuses to overwrite a racing writer.
    os.link(staged, destination)


def export(
    graph: GraphClient,
    output: Path,
    *,
    output_type: str = "superproductivity",
    completed: bool = False,
    attachments_dir: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    task_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    output = output.absolute()
    source_output = output.with_name(output.stem + ".source.json")
    attachments_dir = (
        attachments_dir.absolute() if attachments_dir else output.with_suffix(".attachments")
    )
    destinations = [output]
    if output_type == "superproductivity":
        destinations.append(source_output)
    for destination in [*destinations, attachments_dir]:
        if destination.exists() or destination.is_symlink():
            raise ExportError(f"Output already exists: {destination}. Choose another output path.")
    if any(path == attachments_dir or attachments_dir in path.parents for path in destinations):
        raise ExportError("The JSON output must be outside the attachments directory")

    output.parent.mkdir(parents=True, exist_ok=True)
    source = collect(graph, completed, progress, task_progress)
    counts = {
        "lists": len(source),
        "tasks": sum(len(entry["tasks"]) for entry in source),
        "steps": sum(len(task["checklistItems"]) for entry in source for task in entry["tasks"]),
        "attachments": 0,
        "attachment_warnings": 0,
        "warnings": 0,
        "graph_warnings": sum(
            bool(task.get("_exportWarnings")) for entry in source for task in entry["tasks"]
        ),
    }
    published: list[Path] = []
    # A separate staging directory in the attachment parent also supports another filesystem.
    has_attachments = any(task["attachments"] for entry in source for task in entry["tasks"])
    if has_attachments:
        attachments_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mstodo-extract-", dir=output.parent) as json_temp:
        attachment_parent = attachments_dir.parent if has_attachments else output.parent
        with tempfile.TemporaryDirectory(
            prefix=".mstodo-extract-files-", dir=attachment_parent
        ) as files_temp:
            stage = Path(json_temp)
            files_stage = Path(files_temp) / "files"
            manifest: list[dict[str, Any]] = []
            paths: dict[tuple[str, str, str], Path] = {}
            for entry in source:
                for task in entry["tasks"]:
                    for attachment in task["attachments"]:
                        relative = (
                            Path(_hash_id(entry["id"]))
                            / _hash_id(task["id"])
                            / (
                                _hash_id(attachment["id"])
                                + "-"
                                + safe_filename(attachment.get("name") or "attachment")
                            )
                        )
                        staged_file = files_stage / relative
                        if staged_file.exists():
                            raise ExportError("Attachment output path collision")
                        staged_file.parent.mkdir(parents=True, exist_ok=True)
                        attachment_url = (
                            graph.task_path(entry["id"], task["id"])
                            + "/attachments/"
                            + quote(attachment["id"], safe="")
                            + "/$value"
                        )
                        size, checksum = graph.download(attachment_url, staged_file)
                        staged_file.chmod(0o600)
                        attachment["downloadedSize"] = size
                        if attachment.get("size") is not None and size != attachment["size"]:
                            # Graph can serve a complete file whose size differs from its metadata.
                            # Keep both sizes; transport failures still abort/retry the download.
                            counts["attachment_warnings"] += 1
                            attachment.setdefault("_exportWarnings", []).append(
                                {
                                    "code": "GraphAttachmentSizeMismatch",
                                    "message": (
                                        "Downloaded size differs from Graph attachment metadata"
                                    ),
                                    "graphSize": attachment["size"],
                                    "downloadedSize": size,
                                }
                            )
                        final_path = attachments_dir / relative
                        paths[(entry["id"], task["id"], attachment["id"])] = final_path
                        attachment["localPath"] = os.path.relpath(final_path, output.parent)
                        attachment["sha256"] = checksum
                        manifest.append(
                            {
                                "listId": entry["id"],
                                "taskId": task["id"],
                                "attachmentId": attachment["id"],
                                "name": attachment.get("name"),
                                "contentType": attachment.get("contentType"),
                                "path": relative.as_posix(),
                                "size": size,
                                "graphSize": attachment.get("size"),
                                "sha256": checksum,
                            }
                        )
            counts["attachments"] = len(manifest)
            if has_attachments:
                _write_json(files_stage / "manifest.json", manifest)
            if output_type == "superproductivity":
                backup, warnings = build_backup(source, attachment_paths=paths)
                validate_backup(backup)
                counts["warnings"] = len(warnings)
                _write_json(stage / "primary.json", backup)
                _write_json(stage / "source.json", source)
            else:
                _write_json(stage / "primary.json", source)
            try:
                if has_attachments:
                    # mkdir is the no-overwrite guard; all downloaded bytes are already staged.
                    attachments_dir.mkdir(mode=0o700)
                    published.append(attachments_dir)
                    for child in files_stage.iterdir():
                        child.rename(attachments_dir / child.name)
                if output_type == "superproductivity":
                    _publish_file(stage / "source.json", source_output)
                    published.append(source_output)
                _publish_file(stage / "primary.json", output)
                published.append(output)
            except BaseException:
                for path in reversed(published):
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                raise
    return counts
