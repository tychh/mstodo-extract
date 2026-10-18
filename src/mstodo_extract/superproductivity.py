"""Super Productivity 19.2.0 backup conversion.

The backup envelope follows Microsoft-To-Do-Export's GPLv3 implementation;
entity defaults follow Super Productivity v19.2.0's GPLv3 models.
"""

from __future__ import annotations

import calendar
import json
import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from .dates import milliseconds, parse_datetime
from .errors import ExportError

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _entity_state() -> dict[str, Any]:
    return {"ids": [], "entities": {}}


def _theme(primary: str) -> dict[str, Any]:
    return {
        "primary": primary,
        "accent": "#ff4081",
        "warn": "#e11826",
        "isAutoContrast": True,
        "isDisableBackgroundTint": False,
        "huePrimary": "500",
        "hueAccent": "500",
        "hueWarn": "500",
        "backgroundImageDark": None,
        "backgroundImageLight": None,
        "backgroundOverlayOpacity": 20,
        "backgroundImageBlur": 0,
    }


def _context(identifier: str, title: str, *, project: bool, now: int) -> dict[str, Any]:
    context = {
        "id": identifier,
        "title": title,
        "taskIds": [],
        "icon": "list_alt" if project else None,
        "theme": _theme("#29a1aa" if project else "#a05db1"),
        "advancedCfg": {
            "worklogExportSettings": {
                "cols": ["DATE", "START", "END", "TIME_CLOCK", "TITLES_INCLUDING_SUB"],
                "groupBy": "DATE",
                "separateTasksBy": " | ",
                "roundWorkTimeTo": None,
                "roundStartTimeTo": None,
                "roundEndTimeTo": None,
            }
        },
    }
    if project:
        context.update(
            backlogTaskIds=[],
            noteIds=[],
            isHiddenFromMenu=False,
            isArchived=False,
            isDone=False,
            doneOn=None,
            isEnableBacklog=False,
        )
    else:
        context.update(color=None, created=now)
    return context


def _add(state: dict[str, Any], entity: dict[str, Any]) -> None:
    identifier = entity["id"]
    if identifier in state["entities"]:
        raise ExportError("Generated duplicate Super Productivity ID")
    state["ids"].append(identifier)
    state["entities"][identifier] = entity


def _initial_data(now: int) -> dict[str, Any]:
    config = json.loads(
        files("mstodo_extract").joinpath("resources/global-config-19.2.0.json").read_text("utf-8")
    )
    config["misc"]["isDisableInitialDialog"] = True
    config["sync"]["isEnabled"] = False
    config["sync"]["syncProvider"] = None
    data: dict[str, Any] = {
        name: _entity_state()
        for name in (
            "task",
            "project",
            "tag",
            "section",
            "simpleCounter",
            "taskRepeatCfg",
            "metric",
            "issueProvider",
        )
    }
    data["task"].update(
        currentTaskId=None,
        selectedTaskId=None,
        lastCurrentTaskId=None,
        isDataLoaded=False,
        taskDetailTargetPanel="Default",
        dismissedCalendarAutoImportEventIdsByProvider={},
    )
    data.update(
        globalConfig=config,
        note={**_entity_state(), "todayOrder": []},
        planner={"days": {}},
        boards={"boardCfgs": []},
        menuTree={"projectTree": [], "tagTree": []},
        timeTracking={"project": {}, "tag": {}},
        reminders=[],
        pluginMetadata=[],
        pluginUserData=[],
    )
    for archive in ("archiveYoung", "archiveOld"):
        data[archive] = {
            "task": _entity_state(),
            "timeTracking": {"project": {}, "tag": {}},
            "lastTimeTrackingFlush": 0,
        }
    inbox = _context("INBOX_PROJECT", "Inbox", project=True, now=now)
    inbox["icon"] = "inbox"
    _add(data["project"], inbox)
    today = _context("TODAY", "Today", project=False, now=now)
    today["icon"] = "wb_sunny"
    today["theme"]["primary"] = "#6495ED"
    _add(data["tag"], today)
    return data


def _month_anchor(start: date, day: int, interval: int) -> date:
    # Keep the original day-of-month anchor, including 29/30/31, in startDate.
    month_index = start.year * 12 + start.month - 1
    for _ in range(4800):
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        if day <= calendar.monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if candidate >= start:
                return candidate
        month_index += interval
    raise ValueError("No representable month anchor")


def _repeat_config(
    task: dict[str, Any], project_id: str, tag_ids: list[str], now: int, today: str
) -> dict[str, Any] | None:
    recurrence = task["recurrence"]
    if not isinstance(recurrence, dict):
        return None
    pattern, recurrence_range = recurrence.get("pattern") or {}, recurrence.get("range") or {}
    if not isinstance(pattern, dict) or not isinstance(recurrence_range, dict):
        return None
    # SP has no endDate/numberOfOccurrences fields. Never activate an unbounded approximation.
    if recurrence_range.get("type") != "noEnd":
        return None
    interval = pattern.get("interval")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        return None
    try:
        start = date.fromisoformat(recurrence_range["startDate"])
        cycle = {
            "daily": "DAILY",
            "weekly": "WEEKLY",
            "absoluteMonthly": "MONTHLY",
            "relativeMonthly": "MONTHLY",
            "absoluteYearly": "YEARLY",
        }.get(pattern.get("type"))
        if cycle is None:
            return None
        config: dict[str, Any] = {
            "title": task.get("title") or "",
            "projectId": project_id,
            "tagIds": list(tag_ids),
            "repeatCycle": cycle,
            "repeatEvery": interval,
            "startDate": start.isoformat(),
            "quickSetting": "CUSTOM",
            "isPaused": task.get("status") == "completed",
            "order": 0,
            "lastTaskCreation": now,
            "lastTaskCreationDay": today,
            "repeatFromCompletionDate": False,
            "waitForCompletion": True,
            "shouldInheritSubtasks": True,
            "disableAutoUpdateSubtasks": False,
            "skipOverdue": True,
            "notes": (task.get("body") or {}).get("content") or "",
            **{day: False for day in WEEKDAYS},
        }
        if cycle == "WEEKLY":
            days = [day.lower() for day in pattern.get("daysOfWeek", [])]
            if not days or any(day not in WEEKDAYS for day in days):
                return None
            config.update({day: True for day in days})
        elif pattern["type"] == "absoluteMonthly":
            day = pattern["dayOfMonth"]
            if not isinstance(day, int) or not 1 <= day <= 31:
                return None
            config["startDate"] = _month_anchor(start, day, interval).isoformat()
        elif pattern["type"] == "relativeMonthly":
            days = pattern.get("daysOfWeek") or []
            index = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}.get(
                pattern.get("index")
            )
            if len(days) != 1 or days[0].lower() not in WEEKDAYS or index is None:
                return None
            config["monthlyWeekOfMonth"] = index
            config["monthlyWeekday"] = (WEEKDAYS.index(days[0].lower()) + 1) % 7
        elif cycle == "YEARLY":
            month, day = pattern["month"], pattern["dayOfMonth"]
            if not isinstance(month, int) or not isinstance(day, int):
                return None
            # Leap-day skip/clamp semantics differ between applications.
            if month == 2 and day == 29:
                return None
            candidate = date(start.year, month, day)
            if candidate < start:
                candidate = candidate.replace(year=start.year + interval)
            config["startDate"] = candidate.isoformat()
        return config
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def build_backup(
    lists: list[dict[str, Any]],
    *,
    attachment_paths: dict[tuple[str, str, str], Path] | None = None,
    now: int | None = None,
    id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(16)[:21],
) -> tuple[dict[str, Any], list[str]]:
    now = now if now is not None else int(datetime.now(UTC).timestamp() * 1000)
    today = datetime.fromtimestamp(now / 1000, UTC).astimezone().date().isoformat()
    data = _initial_data(now)
    warnings: list[str] = []
    category_ids: dict[str, str] = {}  # The virtual TODAY tag must never become a category.
    attachment_paths = attachment_paths or {}
    for entry in lists:
        project_id = id_factory()
        _add(
            data["project"],
            _context(project_id, entry.get("displayName") or "", project=True, now=now),
        )
        data["menuTree"]["projectTree"].append({"id": project_id, "k": "p"})
        project = data["project"]["entities"][project_id]
        for source in entry.get("tasks", []):
            identifier = id_factory()
            task: dict[str, Any] = {
                "id": identifier,
                "title": source.get("title") or "",
                "projectId": project_id,
                "subTaskIds": [],
                "timeSpentOnDay": {},
                "timeSpent": 0,
                "timeEstimate": 0,
                "isDone": source.get("status") == "completed",
                "tagIds": [],
                "created": milliseconds(source.get("createdDateTime")),
                "attachments": [],
                "notes": (source.get("body") or {}).get("content") or "",
            }
            if task["created"] is None:
                task["created"] = now
            for category in source.get("categories") or []:
                if category not in category_ids:
                    category_ids[category] = id_factory()
                    _add(
                        data["tag"],
                        _context(category_ids[category], category, project=False, now=now),
                    )
                    data["menuTree"]["tagTree"].append({"id": category_ids[category], "k": "t"})
                tag_id = category_ids[category]
                if tag_id not in task["tagIds"]:
                    task["tagIds"].append(tag_id)
                    data["tag"]["entities"][tag_id]["taskIds"].append(identifier)
            for original, converted in (
                ("lastModifiedDateTime", "modified"),
                ("completedDateTime", "doneOn"),
            ):
                if (value := milliseconds(source.get(original))) is not None:
                    task[converted] = value
            if due := source.get("dueDateTime"):
                value = milliseconds(due)
                if value is not None:
                    local = parse_datetime(due["dateTime"], due.get("timeZone") or "UTC")
                    if local.hour == local.minute == local.second == local.microsecond == 0:
                        task["dueDay"] = local.date().isoformat()
                    else:
                        task["dueWithTime"] = value
            if source.get("isReminderOn"):
                if (value := milliseconds(source.get("reminderDateTime"))) is not None:
                    task["remindAt"] = value
            if source.get("recurrence"):
                config = _repeat_config(source, project_id, task["tagIds"], now, today)
                if config is None:
                    warnings.append("Unrepresentable recurrence preserved without activation")
                    task["notes"] += (
                        "\n\nMicrosoft To Do recurrence "
                        "(not activated in Super Productivity):\n```json\n"
                        + json.dumps(source["recurrence"], ensure_ascii=False, indent=2)
                        + "\n```"
                    )
                else:
                    config["id"] = id_factory()
                    _add(data["taskRepeatCfg"], config)
                    task["repeatCfgId"] = config["id"]
            for attachment in source.get("attachments", []):
                path = attachment_paths.get((entry["id"], source["id"], attachment["id"]))
                if path is None:
                    raise ExportError("Attachment is missing its downloaded file mapping")
                task["attachments"].append(
                    {
                        "id": id_factory(),
                        "type": "FILE",
                        "path": str(path.resolve()),
                        "title": attachment.get("name") or "attachment",
                        "icon": "insert_drive_file",
                    }
                )
            _add(data["task"], task)
            project["taskIds"].append(identifier)
            if task.get("dueDay") == today and not task["isDone"]:
                data["tag"]["entities"]["TODAY"]["taskIds"].append(identifier)
            for step in source.get("checklistItems", []):
                child = {
                    "id": id_factory(),
                    "parentId": identifier,
                    "projectId": project_id,
                    "title": step.get("displayName") or "",
                    "isDone": bool(step.get("isChecked")),
                    "created": milliseconds(
                        step.get("createdDateTime") or step.get("createdDatetime")
                    ),
                    "subTaskIds": [],
                    "tagIds": [],
                    "attachments": [],
                    "timeSpentOnDay": {},
                    "timeSpent": 0,
                    "timeEstimate": 0,
                }
                if child["created"] is None:
                    child["created"] = task["created"]
                task["subTaskIds"].append(child["id"])
                _add(data["task"], child)
    backup = {"timestamp": now, "lastUpdate": now, "crossModelVersion": 4.5, "data": data}
    return backup, warnings


def validate_backup(backup: dict[str, Any]) -> None:
    """Check referential integrity before publishing; this never imports into SP."""
    data = backup["data"]
    for name, state in data.items():
        if not isinstance(state, dict) or "ids" not in state:
            continue
        ids, entities = state["ids"], state["entities"]
        if len(ids) != len(set(ids)) or set(ids) != set(entities):
            raise ExportError(f"Invalid Super Productivity {name} entity state")
        if any(identifier != entity["id"] for identifier, entity in entities.items()):
            raise ExportError(f"Invalid Super Productivity {name} entity ID")
    tasks, projects, tags = (data[name]["entities"] for name in ("task", "project", "tag"))
    for identifier, task in tasks.items():
        if task["projectId"] not in projects:
            raise ExportError("Task references a missing project")
        parent_id = task.get("parentId")
        if parent_id:
            if parent_id not in tasks or identifier not in tasks[parent_id]["subTaskIds"]:
                raise ExportError("Subtask references a missing or inconsistent parent")
            if task["projectId"] != tasks[parent_id]["projectId"]:
                raise ExportError("Subtask belongs to a different project than its parent")
        elif identifier not in projects[task["projectId"]]["taskIds"]:
            raise ExportError("Top-level task is missing from its project")
        for child in task["subTaskIds"]:
            if child not in tasks or tasks[child].get("parentId") != identifier:
                raise ExportError("Task references a missing or inconsistent subtask")
        for tag in task["tagIds"]:
            if tag == "TODAY" or tag not in tags or identifier not in tags[tag]["taskIds"]:
                raise ExportError("Task references a missing or inconsistent category")
        if task.get("repeatCfgId") not in (None, *data["taskRepeatCfg"]["ids"]):
            raise ExportError("Task references a missing repeat configuration")
    for identifier, project in projects.items():
        for task_id in project["taskIds"]:
            if (
                task_id not in tasks
                or tasks[task_id].get("parentId")
                or tasks[task_id]["projectId"] != identifier
            ):
                raise ExportError("Project contains a missing task, subtask, or foreign task")
    for identifier, tag in tags.items():
        for task_id in tag["taskIds"]:
            if task_id not in tasks or (
                identifier != "TODAY" and identifier not in tasks[task_id]["tagIds"]
            ):
                raise ExportError("Tag references a missing or inconsistent task")
