import copy
from datetime import UTC, datetime

import pytest

from mstodo_extract.dates import milliseconds
from mstodo_extract.errors import ExportError
from mstodo_extract.superproductivity import build_backup, validate_backup


def source_task(**updates):
    return {
        "id": "task",
        "title": "Задача 🎯",
        "status": "notStarted",
        "createdDateTime": "2026-10-01T12:34:56.1234567Z",
        "body": {"content": "Сохранить заметку", "contentType": "text"},
        "categories": ["Category", "Today", "Category"],
        "checklistItems": [
            {
                "id": "step",
                "displayName": "Шаг",
                "isChecked": True,
                "createdDateTime": "2026-10-02T00:00:00Z",
            }
        ],
        **updates,
    }


def make_backup(task=None):
    return build_backup(
        [{"id": "list", "displayName": "Проект", "tasks": [task or source_task()]}],
        now=1_791_888_000_000,
    )


def test_projects_tags_subtasks_and_native_service_states():
    backup, warnings = make_backup()
    validate_backup(backup)
    data = backup["data"]
    assert warnings == []
    assert backup["crossModelVersion"] == 4.5
    assert set(data) >= {"section", "archiveYoung", "archiveOld", "reminders", "timeTracking"}
    assert "INBOX_PROJECT" in data["project"]["ids"]
    parent = next(task for task in data["task"]["entities"].values() if not task.get("parentId"))
    child = data["task"]["entities"][parent["subTaskIds"][0]]
    project = data["project"]["entities"][parent["projectId"]]
    assert project["taskIds"] == [parent["id"]]
    assert child["parentId"] == parent["id"]
    assert child["isDone"] is True
    assert child["created"] == milliseconds("2026-10-02T00:00:00Z")
    assert parent["notes"] == "Сохранить заметку"
    assert len(parent["tagIds"]) == 2
    assert "TODAY" not in parent["tagIds"]
    assert data["globalConfig"]["tasks"]["defaultProjectId"] == "INBOX_PROJECT"
    assert data["globalConfig"]["sync"]["isEnabled"] is False
    assert len(data["globalConfig"]) == 20


def test_same_list_names_still_produce_distinct_projects_and_empty_lists_survive():
    backup, _ = build_backup(
        [
            {"id": "one", "displayName": "Same", "tasks": []},
            {"id": "two", "displayName": "Same", "tasks": []},
        ]
    )
    validate_backup(backup)
    assert len(backup["data"]["project"]["ids"]) == 3


def test_completed_dates_reminders_and_date_only_due_dates():
    backup, _ = make_backup(
        source_task(
            status="completed",
            completedDateTime={
                "dateTime": "2026-10-13T12:00:00.0000000",
                "timeZone": "Romance Standard Time",
            },
            dueDateTime={"dateTime": "2026-10-14T00:00:00.0000000", "timeZone": "UTC"},
            isReminderOn=True,
            reminderDateTime={
                "dateTime": "2026-10-14T08:00:00.0000000",
                "timeZone": "Europe/Paris",
            },
        )
    )
    task = next(
        task for task in backup["data"]["task"]["entities"].values() if not task.get("parentId")
    )
    assert task["isDone"] is True
    assert task["doneOn"] == milliseconds("2026-10-13T10:00:00Z")
    assert task["dueDay"] == "2026-10-14"
    assert "dueWithTime" not in task
    assert task["remindAt"] == milliseconds("2026-10-14T06:00:00Z")


def test_timed_due_date_and_null_optional_fields():
    backup, _ = make_backup(
        source_task(
            body=None,
            categories=None,
            reminderDateTime=None,
            completedDateTime=None,
            dueDateTime={"dateTime": "2026-10-14T09:30:00Z", "timeZone": "Europe/Paris"},
        )
    )
    task = next(
        task for task in backup["data"]["task"]["entities"].values() if not task.get("parentId")
    )
    assert task["dueWithTime"] == milliseconds("2026-10-14T09:30:00Z")
    assert "dueDay" not in task
    assert task["notes"] == ""


@pytest.mark.parametrize(
    "zone,month,utc_hour", [("Romance Standard Time", "01", "11"), ("Europe/Paris", "07", "10")]
)
def test_timezone_conversion_and_fractional_seconds(zone, month, utc_hour):
    assert milliseconds(
        {"dateTime": f"2026-{month}-13T12:00:00.1234567", "timeZone": zone}
    ) == milliseconds(f"2026-{month}-13T{utc_hour}:00:00.123Z")


def test_explicit_timezone_offset_is_not_reinterpreted_using_the_zone_name():
    assert milliseconds(
        {"dateTime": "2026-10-13T12:00:00+03:00", "timeZone": "UTC"}
    ) == milliseconds("2026-10-13T09:00:00Z")


def test_invalid_timezone_is_actionable():
    with pytest.raises(ExportError, match="unsupported time zone"):
        milliseconds({"dateTime": "2026-10-13T12:00:00", "timeZone": "Unknown Time Zone"})


@pytest.mark.parametrize(
    "kind,extra,expected",
    [
        ("daily", {}, "DAILY"),
        ("weekly", {"daysOfWeek": ["monday", "friday"]}, "WEEKLY"),
        ("absoluteMonthly", {"dayOfMonth": 20}, "MONTHLY"),
        ("relativeMonthly", {"daysOfWeek": ["Thursday"], "index": "last"}, "MONTHLY"),
        ("absoluteYearly", {"month": 12, "dayOfMonth": 31}, "YEARLY"),
    ],
)
def test_real_graph_recurrence_names(kind, extra, expected):
    recurrence = {
        "pattern": {"type": kind, "interval": 2, **extra},
        "range": {"type": "noEnd", "startDate": "2026-10-13"},
    }
    backup, warnings = make_backup(source_task(recurrence=recurrence))
    validate_backup(backup)
    config = next(iter(backup["data"]["taskRepeatCfg"]["entities"].values()))
    assert config["repeatCycle"] == expected
    assert config["repeatEvery"] == 2
    assert config["quickSetting"] == "CUSTOM"
    assert config["waitForCompletion"] is True
    assert warnings == []
    if kind == "relativeMonthly":
        assert config["monthlyWeekOfMonth"] == -1
        assert config["monthlyWeekday"] == 4
    if kind == "absoluteYearly":
        assert config["startDate"] == "2026-12-31"


def test_completed_recurrences_are_paused_while_open_recurrences_remain_active():
    recurrence = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {"type": "noEnd", "startDate": "2026-10-13"},
    }
    backup, warnings = build_backup(
        [
            {
                "id": "list",
                "tasks": [
                    source_task(id="past", status="completed", recurrence=recurrence),
                    source_task(id="current", recurrence=recurrence),
                ],
            }
        ]
    )
    validate_backup(backup)
    assert warnings == []
    parents = [
        task for task in backup["data"]["task"]["entities"].values() if not task.get("parentId")
    ]
    assert len(parents) == 2
    for task in parents:
        config = backup["data"]["taskRepeatCfg"]["entities"][task["repeatCfgId"]]
        assert config["isPaused"] is task["isDone"]
        assert config["repeatCycle"] == "DAILY"
        assert config["repeatEvery"] == 1


@pytest.mark.parametrize(
    "recurrence",
    [
        {
            "pattern": {"type": "daily", "interval": 1},
            "range": {"type": "numbered", "numberOfOccurrences": 5, "startDate": "2026-10-13"},
        },
        {
            "pattern": {"type": "weekly", "interval": 1},
            "range": {"type": "endDate", "endDate": "2027-01-01", "startDate": "2026-10-13"},
        },
        {
            "pattern": {"type": "relativeYearly", "interval": 1},
            "range": {"type": "noEnd", "startDate": "2026-10-13"},
        },
        {
            "pattern": {"type": "unknown", "interval": 1},
            "range": {"type": "noEnd", "startDate": "2026-10-13"},
        },
    ],
)
def test_unrepresentable_recurrence_is_preserved_without_activation(recurrence):
    backup, warnings = make_backup(source_task(recurrence=recurrence))
    assert len(warnings) == 1
    assert backup["data"]["taskRepeatCfg"]["ids"] == []
    task = next(
        task for task in backup["data"]["task"]["entities"].values() if not task.get("parentId")
    )
    assert task["notes"].startswith("Сохранить заметку")
    assert '"pattern"' in task["notes"]
    assert "repeatCfgId" not in task


def test_conversion_does_not_mutate_or_remove_unknown_source_fields():
    source = [
        {"id": "list", "displayName": "List", "tasks": [source_task(unknown={"keep": [1, 2]})]}
    ]
    original = copy.deepcopy(source)
    build_backup(source)
    assert source == original


def test_broken_references_are_refused():
    backup, _ = make_backup()
    parent = next(
        task for task in backup["data"]["task"]["entities"].values() if not task.get("parentId")
    )
    backup["data"]["project"]["entities"][parent["projectId"]]["taskIds"].append(
        parent["subTaskIds"][0]
    )
    with pytest.raises(ExportError, match="Project contains"):
        validate_backup(backup)


def test_timestamp_uses_integer_millisecond_precision():
    expected = datetime(2026, 10, 13, 12, 0, 0, 123000, tzinfo=UTC)
    assert milliseconds("2026-10-13T12:00:00.1234567Z") == int(expected.timestamp() * 1000)


def test_zero_timestamp_is_not_replaced_with_export_time():
    backup, _ = make_backup(source_task(createdDateTime="1970-01-01T00:00:00Z"))
    task = next(
        task for task in backup["data"]["task"]["entities"].values() if not task.get("parentId")
    )
    assert task["created"] == 0
