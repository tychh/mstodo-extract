from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .errors import ExportError
from .exporter import export
from .graph import GraphClient


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="mstodo-extract",
        description="MSToDo Extract: export Microsoft To Do and attached files",
    )
    root.add_argument("--version", action="version", version=f"MSToDo Extract {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="Export lists, tasks, steps, and attachments")
    command.add_argument(
        "--type", choices=("superproductivity", "json"), default="superproductivity"
    )
    command.add_argument("--completed", action="store_true", help="Include completed tasks")
    command.add_argument("--output", "-o", type=Path, help="Output JSON file (must not exist)")
    command.add_argument("--attachments-dir", type=Path, help="File directory (must not exist)")
    tokens = command.add_mutually_exclusive_group()
    tokens.add_argument("--token-file", type=Path, help="File containing a Graph access token")
    tokens.add_argument("--token", "-t", help="Graph access token; prefer --token-file")
    return root


def read_token(args: argparse.Namespace) -> str:
    if args.token_file is not None:
        token = args.token_file.read_text(encoding="utf-8-sig")
    elif args.token is not None:
        token = args.token
    else:
        token = os.environ.get("MSTODO_EXTRACT_TOKEN", "")
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(char.isspace() for char in token):
        raise ExportError(
            "Provide a valid token via --token-file, --token, or MSTODO_EXTRACT_TOKEN"
        )
    return token


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    default_name = (
        "mstodo-extract.superproductivity.json"
        if args.type == "superproductivity"
        else "mstodo-extract.json"
    )
    output = args.output or Path(default_name)

    def progress(done: int, total: int) -> None:
        if done % 5 == 0 or done == total:
            print(f"Read lists: {done}/{total}", file=sys.stderr, flush=True)

    def task_progress(done: int, total: int) -> None:
        if done % 500 == 0 or done == total:
            print(f"Read task details: {done}/{total}", file=sys.stderr, flush=True)

    try:
        token = read_token(args)
        with GraphClient(token) as graph:
            counts = export(
                graph,
                output,
                output_type=args.type,
                completed=args.completed,
                attachments_dir=args.attachments_dir,
                progress=progress,
                task_progress=task_progress,
            )
    except (ExportError, OSError, UnicodeError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Export interrupted; no incomplete JSON was published", file=sys.stderr)
        return 130
    print(
        f"Exported {counts['lists']} lists, {counts['tasks']} tasks, "
        f"{counts['steps']} steps, {counts['attachments']} files to {output}",
        file=sys.stderr,
    )
    if counts["warnings"]:
        print(
            f"Warning: {counts['warnings']} recurrence rules could not be represented exactly; "
            "preserved in task notes and source JSON without an active repeat config.",
            file=sys.stderr,
        )
    if counts["graph_warnings"]:
        print(
            f"Warning: recovered {counts['graph_warnings']} tasks affected by null "
            "linkedResources in Graph. Task properties were preserved; the invalid "
            "related-resource response is recorded in source JSON as _exportWarnings.",
            file=sys.stderr,
        )
    if counts["attachment_warnings"]:
        print(
            f"Warning: {counts['attachment_warnings']} downloaded files differ in size from "
            "Graph metadata. Original and downloaded sizes are preserved in source JSON "
            "and the attachment manifest.",
            file=sys.stderr,
        )
    if args.type == "superproductivity":
        print(
            "Super Productivity backup import replaces the current application state. "
            "Save a backup before importing.",
            file=sys.stderr,
        )
    return 0
