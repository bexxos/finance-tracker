#!/usr/bin/env python3
"""OPT-IN storage adapter: keep the ledger in a single Google Drive file.

The local-file backend in ``finance_logger.py`` is the default and needs none of
this.  This adapter exists for users who want the same ledger on more than one
machine, and it is deliberately separate so that the default path stays free of
third-party packages and network access.

Requirements (yours, not the repo's):

* ``pip install google-api-python-client google-auth``
* your own OAuth credentials.  Point ``--credentials`` at an authorized-user JSON
  file produced by the standard Google OAuth installed-app flow, or set
  ``GOOGLE_APPLICATION_CREDENTIALS``.

The Google libraries are imported lazily inside the functions that need them, so
importing this module never fails just because they are missing.

Planning, classification and every derived number come from ``finance_logger``;
this module only reads one Drive file and writes it back in place under an
optimistic-concurrency guard plus a byte-for-byte readback:

    python3 scripts/drive_adapter.py add \\
        --file-id YOUR_FILE_ID --parent-id YOUR_FOLDER_ID \\
        --file-name 'September Expenses Log.md' --month September \\
        --entry '33 bread' --apply
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finance_logger import (  # noqa: E402
    ConcurrentModificationError,
    LedgerError,
    LedgerLock,
    collapse_batch_duplicates,
    format_plan,
    local_timezone,
    move_entries,
    parse_entry,
    parse_income,
    pay_loan,
    plan_update,
    set_loan_balance,
)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
MARKDOWN_MIME = "text/markdown"
_FILE_FIELDS = "id,name,mimeType,parents,modifiedTime,version,headRevisionId,size"

_MISSING_LIBRARIES = (
    "this adapter needs the Google client libraries: "
    "pip install google-api-python-client google-auth"
)


def load_credentials(path):
    """Load authorized-user credentials, refreshing them when they are expired."""
    if path is None or not Path(path).exists():
        raise LedgerError(
            f"no Google credentials found at {path!r}; create an authorized-user "
            "JSON file with the standard OAuth installed-app flow and pass it with "
            "--credentials, or set GOOGLE_APPLICATION_CREDENTIALS"
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LedgerError(_MISSING_LIBRARIES) from exc

    credentials = Credentials.from_authorized_user_file(str(path), [DRIVE_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    return credentials


def build_service(credentials):
    """Build the Drive v3 client.  Imported here so plain imports stay cheap."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LedgerError(_MISSING_LIBRARIES) from exc
    return build("drive", "v3", credentials=credentials)


class DriveLedger:
    """Guarded one-file Drive client: never uploads a second copy."""

    def __init__(self, service, file_id: str, parent_id: str, file_name: str):
        self.service = service
        self.file_id = file_id
        self.parent_id = parent_id
        self.file_name = file_name

    def metadata(self) -> dict:
        return (
            self.service.files()
            .get(fileId=self.file_id, fields=_FILE_FIELDS)
            .execute()
        )

    def assert_canonical(self) -> dict:
        meta = self.metadata()
        if meta.get("id") != self.file_id:
            raise LedgerError("Drive returned an unexpected file id")
        if meta.get("name") != self.file_name:
            raise LedgerError(f"unexpected file name: {meta.get('name')!r}")
        if meta.get("mimeType") != MARKDOWN_MIME:
            raise LedgerError(f"unexpected MIME type: {meta.get('mimeType')!r}")
        if self.parent_id not in meta.get("parents", []):
            raise LedgerError("the canonical parent is missing")
        if meta.get("trashed"):
            raise LedgerError("the canonical ledger is trashed")
        return meta

    def parent_scoped_exact_count(self) -> int:
        query = (
            f"name = '{self.file_name}' and '{self.parent_id}' in parents "
            "and trashed = false"
        )
        result = (
            self.service.files()
            .list(
                q=query,
                pageSize=100,
                fields="files(id,name,mimeType,parents,modifiedTime)",
            )
            .execute()
        )
        files = result.get("files", [])
        if len(files) != 1 or files[0].get("id") != self.file_id:
            raise LedgerError(
                f"parent-scoped canonical count is {len(files)}, expected one match"
            )
        return len(files)

    def read_bytes(self) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        request = self.service.files().get_media(fileId=self.file_id)
        output = io.BytesIO()
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return output.getvalue()

    def update_bytes(self, content: bytes) -> dict:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(
            io.BytesIO(content), mimetype=MARKDOWN_MIME, resumable=False
        )
        return (
            self.service.files()
            .update(fileId=self.file_id, media_body=media, fields=_FILE_FIELDS)
            .execute()
        )

    def apply(self, baseline: bytes, staged: bytes) -> dict:
        """Re-read, require an unchanged baseline, update in place, then read back."""
        live = self.read_bytes()
        if live != baseline:
            raise ConcurrentModificationError(
                "the Drive file changed while the update was staged"
            )
        result = self.update_bytes(staged)
        if self.read_bytes() != staged:
            raise LedgerError("post-write Drive readback differs from staged content")
        return result


def _lock_path(file_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"finance-ledger-{file_id}.lock"


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drive_adapter",
        description=(
            "Opt-in Drive storage for the finance ledger. Every command prints the "
            "COMPLETE resulting sheet; --apply is the only switch that writes."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def shared(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--file-id", required=True, help="the ledger's Drive file id")
        sub.add_argument("--parent-id", required=True, help="its expected parent folder")
        sub.add_argument(
            "--file-name", default="Expenses Log.md", help="its exact Drive file name"
        )
        sub.add_argument(
            "--credentials",
            default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
            help="authorized-user JSON (default: $GOOGLE_APPLICATION_CREDENTIALS)",
        )
        sub.add_argument("--month", default=None, help="sheet month name")
        sub.add_argument(
            "--apply", action="store_true", help="write; without it this is a dry run"
        )

    add = subparsers.add_parser("add", help="log expenses and/or income")
    shared(add)
    add.add_argument("--entry", action="append", default=[], metavar="'AMOUNT DESCRIPTION'")
    add.add_argument("--income", action="append", default=[], metavar="AMOUNT")
    add.add_argument("--kind", choices=["need", "want"], default=None)
    add.add_argument("--date", default="today", help="YYYY-MM-DD or 'today' (default)")
    add.add_argument("--event-id", default=None, help="idempotency key for the source event")
    add.add_argument("--state-path", default=None)

    move = subparsers.add_parser("move", help="relocate entries between dates")
    shared(move)
    move.add_argument("--entry", action="append", default=[], metavar="'AMOUNT DESCRIPTION'")
    move.add_argument("--kind", choices=["need", "want"], default=None)
    move.add_argument("--from-date", required=True)
    move.add_argument("--to-date", required=True)

    pay = subparsers.add_parser("payloan", help="reduce a loan balance")
    shared(pay)
    pay.add_argument("--name", required=True)
    pay.add_argument("--amount", required=True)
    pay.add_argument("--funded-by", choices=["savings", "cash"], default=None)

    setloan = subparsers.add_parser("setloan", help="set a loan balance to account state")
    shared(setloan)
    setloan.add_argument("--name", required=True)
    setloan.add_argument("--balance", required=True)

    return parser


def _parse_cli_date(value: str) -> date:
    if value == "today":
        from finance_logger import local_today

        return local_today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--date must be 'today' or YYYY-MM-DD") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)

    credentials = load_credentials(args.credentials)
    service = build_service(credentials)
    ledger = DriveLedger(service, args.file_id, args.parent_id, args.file_name)

    state_path = Path(
        args.state_path
        if getattr(args, "state_path", None)
        else Path(tempfile.gettempdir()) / f"finance-ledger-{args.file_id}.events.json"
    )
    state = {}
    if args.command == "add" and state_path.exists():
        state = json.loads(state_path.read_text())
        if args.event_id and args.event_id in state:
            current = ledger.read_bytes().decode("utf-8")
            print("REPLAYED EVENT (nothing written)")
            print("--- FULL SHEET ---")
            print(current, end="" if current.endswith("\n") else "\n")
            return 0

    with LedgerLock(_lock_path(args.file_id)):
        ledger.assert_canonical()
        ledger.parent_scoped_exact_count()
        baseline = ledger.read_bytes()
        try:
            baseline_text = baseline.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError("the Drive file is not UTF-8 Markdown") from exc

        if args.command == "add":
            entry_date = _parse_cli_date(args.date)
            entries = [parse_entry(raw, entry_date, args.kind) for raw in args.entry]
            incomes = [parse_income(raw, entry_date) for raw in args.income]
            if not entries and not incomes:
                parser.error("at least one --entry or --income is required")
            entries, collapsed = collapse_batch_duplicates(entries)
            plan = plan_update(baseline_text, entries, month=args.month, incomes=incomes)
        elif args.command == "move":
            if not args.entry:
                parser.error("at least one --entry is required")
            source_date = _parse_cli_date(args.from_date)
            target_date = _parse_cli_date(args.to_date)
            entries = [parse_entry(raw, source_date, args.kind) for raw in args.entry]
            collapsed = 0
            plan = move_entries(baseline_text, entries, to_date=target_date, month=args.month)
        elif args.command == "payloan":
            collapsed = 0
            plan = pay_loan(
                baseline_text,
                args.name,
                int(args.amount),
                funded_by=args.funded_by,
                month=args.month,
            )
        else:
            collapsed = 0
            plan = set_loan_balance(
                baseline_text, args.name, int(args.balance), month=args.month
            )

        if not args.apply:
            print(format_plan(plan, dry_run=True, collapsed=collapsed))
            return 0

        result = ledger.apply(baseline, plan.text.encode("utf-8"))
        if args.command == "add" and args.event_id:
            state[args.event_id] = {
                "applied_at": datetime.now(local_timezone()).isoformat(),
                "file_id": args.file_id,
            }
            state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"drive": result}, ensure_ascii=False))
        print(format_plan(plan, dry_run=False, collapsed=collapsed))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LedgerError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
