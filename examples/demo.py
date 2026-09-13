#!/usr/bin/env python3
"""Runnable demo for the finance-ledger skill.

It copies the fictional ``examples/sample-ledger.md`` into a temporary
directory, logs one Need, one Want and one income entry through the real CLI,
shows the COMPLETE resulting sheet, then rolls the ledger over into a new month
and prints that sheet too.

    python3 examples/demo.py

Nothing here touches the network or needs credentials, and the temporary
directory is removed on exit, so it is safe to run repeatedly.
"""

import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import finance_logger  # noqa: E402


def run(*arguments):
    """Call the real CLI entry point and capture what it printed."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = finance_logger.main(list(arguments))
    return code, buffer.getvalue()


def show(step, title, text):
    print()
    print("=" * 72)
    print(f"STEP {step}: {title}")
    print("=" * 72)
    print(text)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="finance-ledger-demo-") as temporary:
        work = Path(temporary)
        ledger = work / "september-expenses-log.md"
        shutil.copyfile(ROOT / "examples" / "sample-ledger.md", ledger)

        print("Working on a throwaway copy of the fictional sample ledger:")
        print(f"  {ledger}")

        common = ["--ledger", str(ledger), "--month", "September", "--date", "2026-09-12"]
        changes = [
            "--entry",
            "42 groceries",  # classified as a Need
            "--entry",
            "85 streaming subscription",  # classified as a Want
            "--income",
            "1500",
        ]

        code, output = run("add", *common, *changes)
        if code:
            return code
        show(
            1,
            "dry run: one Need, one Want and one income (nothing written yet)",
            output,
        )

        code, output = run("add", *common, *changes, "--apply")
        if code:
            return code
        show(2, "applied: the complete sheet now on disk", output)

        code, output = run(
            "rollover",
            "--ledger",
            str(ledger),
            "--to-month",
            "October",
            "--opening-income",
            "12000",
            "--apply",
        )
        if code:
            return code
        show(3, "rollover: the new month, with loan balances carried forward", output)

        october = work / "october-expenses-log.md"
        print()
        print(f"Both files were written inside {work}")
        print(f"  {ledger.name}  ({len(ledger.read_text().splitlines())} lines)")
        print(f"  {october.name}  ({len(october.read_text().splitlines())} lines)")
        print("The temporary directory disappears when this demo exits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
