# MSToDo Extract

**Export your Microsoft To Do tasks to [Super Productivity](https://super-productivity.com/) or JSON.**

MSToDo Extract is a command-line utility that downloads your Microsoft To Do lists,
tasks, completed tasks, checklist items, notes, categories, dates, and attachments.
It can create a backup ready to import into Super Productivity or preserve the
retrieved Microsoft Graph records in JSON.

- **Read-only:** it does not change or delete anything in Microsoft To Do.
- **Completed tasks and attachments:** include your history, not just what's open.
- **Built for imperfect data:** handles a known Microsoft Graph response failure
  and records conversion warnings rather than silently inventing values.
- **No programming required:** follow the steps below; you don't have to clone
  the repository or set up a Python project.

> [!IMPORTANT]
> Importing a backup into Super Productivity **replaces its current data and
> settings**. Back up anything you want to keep in Super Productivity before
> importing.

## Quick start: Microsoft To Do → Super Productivity

You'll need a Microsoft account with To Do data and a computer with
[uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Git](https://git-scm.com/downloads) installed. This version uses a Microsoft
Graph access token that you obtain through your browser.

### 1. Get a Microsoft Graph access token

1. Open [Microsoft Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer)
   and sign in **with the same Microsoft account you use for Microsoft To Do**.
2. Enter `GET https://graph.microsoft.com/v1.0/me/todo/lists` and run it.
3. If you're asked for permissions, open **Modify permissions** and grant
   `Tasks.Read` (read-only). `Tasks.ReadWrite` also works but isn't needed.
   Run the request again if necessary.
4. Open the **Access token** tab and copy the token.
5. Save the token as plain text in a file named `ms-token.txt`, in the folder
   where you'll run the export command. Don't add quotation marks or any other
   text to the file.

**Keep this token private.** It grants access to your Microsoft To Do data.
Don't post it in issues, share screenshots of it, or commit it to Git. Tokens
expire, so you may need to obtain a new one for a later export.

### 2. Export your tasks

Open a terminal in the folder containing `ms-token.txt`.

**Linux / macOS:**

```bash
uvx --from git+https://github.com/tychh/mstodo-extract.git \
  mstodo-extract export \
  --completed \
  --output todo-sp.json \
  --token-file ms-token.txt
```

**Windows PowerShell** (the same command on one line):

```powershell
uvx --from git+https://github.com/tychh/mstodo-extract.git mstodo-extract export --completed --output todo-sp.json --token-file ms-token.txt
```

`uvx` downloads and runs the tool in an isolated environment. You don't have
to install the tool permanently or check out the repository. `uv` selects a
compatible Python interpreter and can download one if needed. If you have
trouble obtaining Python automatically, see
[uv's Python documentation](https://docs.astral.sh/uv/guides/install-python/).

The command creates `todo-sp.json` and, where applicable, related source and
attachment files. It prints progress and warnings in the terminal. For a large
account, the export can take a while, and progress may pause when Microsoft
Graph is slow or requests are retried.

**Why `--completed`?** By default, only unfinished tasks are exported.
Adding this option includes your completed-task history too.

### 3. Import into Super Productivity

> [!WARNING]
> **Large migrations: experience with Super Productivity 19.2.0.** I successfully
> imported tens of thousands of tasks, but the import took **tens of minutes
> without progress messages**, making the application appear frozen. Afterwards,
> large lists were extremely slow to display on desktop, and on Android they
> made the application almost completely unresponsive. For a large migration,
> consider keeping the full history as JSON and importing only unfinished tasks
> into Super Productivity (omit `--completed`).

1. **Back up your existing Super Productivity data first.** Importing this file
   is a *replacement*, not a merge.
2. Open the **desktop** version of Super Productivity and go to its
   import/export settings.
3. Choose **Import from File** and select `todo-sp.json`.
4. Review your imported projects and tasks. If you have attachments, keep the
   accompanying attachment folder in its original location.

Your Microsoft To Do lists become Super Productivity projects, categories
become tags, and checklist items become subtasks. Inbox and Today are also
included.

> [!NOTE]
> Imported file attachments use absolute paths on the machine that ran the
> export. Moving the attachment folder, transferring the backup to a different
> computer, or using the web version of Super Productivity may prevent those
> files from opening. The JSON file alone does **not** contain the file bytes.

### Want just a JSON backup?

Instead of a Super Productivity backup, use `--type json`:

```bash
uvx --from git+https://github.com/tychh/mstodo-extract.git \
  mstodo-extract export \
  --type json \
  --completed \
  --output todo-source.json \
  --token-file ms-token.txt
```

This preserves Microsoft Graph data for archival or processing with other tools;
it is **not** a Super Productivity import file.

## Other ways to run it

**Install the command permanently** (useful for repeated exports):

```bash
uv tool install git+https://github.com/tychh/mstodo-extract.git
mstodo-extract export --help
```

If the installed command is not found, run `uv tool update-shell` and restart
your terminal. Once installed, the regular export command is:

```bash
mstodo-extract export --completed --output todo-sp.json --token-file ms-token.txt
```

**Run from a local checkout** (for development):

```bash
uv run mstodo-extract export --completed --output todo-sp.json --token-file ms-token.txt
```

You can also run `uv run python -m mstodo_extract export --help` from a checkout.
This project requires Python 3.11 or newer. For all CLI options, run
`mstodo-extract export --help` (or use the `uvx --from ...` prefix).

## Why I made this

MSToDo Extract started when I tried to export my own Microsoft To Do data
using [daylamtayari/Microsoft-To-Do-Export](https://github.com/daylamtayari/Microsoft-To-Do-Export),
the Go project on which this tool is based.

My account contained **more than 14,000 tasks**. The Go exporter failed with
`failed to retrieve the value of the values JSON field`. Investigation found
Graph responses with HTTP 200 and truncated JSON caused by null entries in
`linkedResources`. I haven't established a connection between this failure
and the total task count.

I built this Python utility to make the export usable for my data and to focus
on defensive handling of Graph API edge cases, clear warnings, and a reliable
Super Productivity backup. It is a focused alternative, not a full reimplementation
of all formats and commands supported by the original Go project.

## What gets exported?

| Data | Super Productivity backup | Source JSON |
| --- | --- | --- |
| Task lists and tasks | Lists become projects | Retrieved Graph properties preserved |
| Completed tasks | With `--completed` | With `--completed` |
| Checklist items | Subtasks | Original checklist items |
| Categories | Tags | Original categories |
| Dates, notes, and supported recurrences | Converted where representable | Source properties preserved |
| Attached files | Local file references | Metadata, local paths, and downloaded files |

The Super Productivity format has been checked against **Super Productivity
19.2.0**. The source JSON also retains task and list properties that have no
direct equivalent in Super Productivity, including importance, start dates,
and additional status values.

### Files created

For `--output todo-sp.json`, the tool produces:

- `todo-sp.json` — the Super Productivity backup (`crossModelVersion: 4.5`).
- `todo-sp.source.json` — the associated Microsoft Graph data, including
  checklist and attachment collections and any additional properties.
- `todo-sp.attachments/` — downloaded attachments plus `manifest.json`, if
  there are attachments.

For `--type json`, the main output file already holds the source data; no
additional `.source.json` is created. Without `--output`, the default filenames
are `mstodo-extract.superproductivity.json` and `mstodo-extract.json`.

Attachments are downloaded automatically. You can choose another directory with
`--attachments-dir /absolute/path/to/files`. Filenames contain ID hashes to
avoid collisions, and the manifest includes source IDs, filenames, paths,
downloaded sizes, and SHA-256 hashes. Binary file contents are kept separately
from JSON.

### Important behavior and limitations

- **No partial published backup.** Export data is assembled in temporary
  directories. If reading, downloading, or conversion fails, the tool stops
  without publishing an incomplete JSON backup.
- **No overwriting.** Existing output files and attachment directories are not
  replaced. Use a different `--output` name for another export.
- **Read-only API access.** Only GET requests are made. Tokens and task contents
  are not written to logs; progress, counts, and errors go to stderr.
- **Graph API edge case.** Microsoft Graph can occasionally return HTTP 200
  together with truncated JSON and an `InternalServerError` when a task has a
  `null` entry in `linkedResources`. The tool can recover by rereading the
  affected list task by task, provided the core task fields and notes were
  already received. It continues pagination, preserves what Graph returned,
  and records `_exportWarnings`. Links after the damaged entry may not be
  recoverable. This is **not** a general-purpose malformed-JSON repair tool.
- **Attachments.** If the downloaded file size disagrees with Graph metadata,
  both the original and actual sizes are preserved and a warning is recorded.
  Incomplete HTTP transfers still fail or are retried.
- **Recurring tasks.** Supported indefinite daily, weekly, absolute monthly,
  absolute yearly, and certain monthly Nth-weekday patterns become active
  Super Productivity repeats for unfinished tasks. Patterns that cannot be
  represented faithfully (for example, a repeat count, end date, relative
  yearly pattern, or February 29) are kept in task notes and source JSON,
  with a warning instead of a misleading active repeat. Completed recurring
  tasks retain their repeat settings in a paused state.
- **Dates.** Midnight due dates become calendar dates (`dueDay`); other due
  dates use `dueWithTime`. Windows and IANA time zones are handled without
  depending on the export machine's local time zone.
- **Notes.** Notes are kept as received, without converting HTML to Markdown.

Tasks are read from up to four lists concurrently. Checklist items and
attachment metadata are then collected with a shared pool of up to four workers.
Progress is reported every five lists, every 500 tasks with details collected,
and at stage completion.
Attachment downloads don't currently have a separate progress indicator.

## Troubleshooting

**GitHub authentication errors** — If the repository is private, Git must be
authenticated with an account that has access before `uvx` or `uv tool install`
can fetch it.

**HTTP 401 / “Unauthorized”** — Your token may have expired. Copy a fresh token
from Graph Explorer into `ms-token.txt` and rerun the command.

**HTTP 403 / “Forbidden”** — Check that you're signed into the intended account
and have granted the delegated `Tasks.Read` permission (or `Tasks.ReadWrite`).

**“Output already exists”** — Choose a new name using `--output`. Existing
backup files are deliberately not overwritten.

**Finished tasks are missing** — Add `--completed`. Without it, only unfinished
tasks are exported.

**The export seems stuck** — Microsoft Graph requests and retries can cause
pauses. Follow progress and warnings in the terminal. If it fails, do not assume
that an incomplete output is usable.

**Import looks wrong or an attachment won't open** — Ensure you're importing the
main `todo-sp.json` file, not `.source.json`. Check the attachment folder's
location and the absolute paths inside the exported backup.

## Roadmap

Planned improvements for future releases:

- [ ] **Microsoft sign-in with OAuth Device Code Flow.** Let the CLI display
  browser sign-in instructions and obtain Microsoft Graph tokens without
  requiring users to copy tokens manually from Graph Explorer.
- [ ] **Standalone binary releases built with Nuitka.** Target Linux
  (Ubuntu 22.04), Windows 10, and macOS on both Apple Silicon (arm64) and
  Intel (x86_64), so users can run the tool without installing Python or uv.

## Development

```bash
uv sync --locked
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv build
```

Tests use synthetic Graph responses to check pagination, API errors, retries,
time zones, entity relationships, recurrences, attachment downloads, and safe
publication of exports. They do not need a real Microsoft token and do not
perform a real Super Productivity import.

An optional validation against the `validateFull` function from an installed
Super Productivity 19.2.0 (requires Node) is available:

```bash
uv run python scripts/validate_sp_format.py todo-sp.json
```

It checks the export without launching the app, opening its profile, or
importing the data. Use `--asar /path/to/resources/app.asar` if needed.

## Credits and license

MSToDo Extract's implementation is based on the GPLv3 project
[daylamtayari/Microsoft-To-Do-Export](https://github.com/daylamtayari/Microsoft-To-Do-Export).

Super Productivity backup fields and defaults follow the GPLv3 project
[Super Productivity v19.2.0](https://github.com/super-productivity/super-productivity/tree/v19.2.0),
including its task, project, tag, and repeat models.
`src/mstodo_extract/resources/global-config-19.2.0.json` is derived from its
[`DEFAULT_GLOBAL_CONFIG`](https://github.com/super-productivity/super-productivity/blob/v19.2.0/src/app/features/config/default-global-config.const.ts),
configured for production with synchronization disabled and an empty OneDrive
client. Generating a backup does not require access to Super Productivity's
source code.

**License:** GPL-3.0-only — see [LICENSE](LICENSE).

This is an independent project, not affiliated with Microsoft or the
Super Productivity maintainers.
