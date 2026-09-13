#!/usr/bin/env python3
"""Runnable demo for the finance-ledger skill.

It copies the fictional ``examples/sample-ledger.md`` into a temporary directory
with its budget heading reset to the placeholder a brand-new sheet carries, so
the first-run refusal can be shown: an entry is attempted before any split exists
and is refused.  The demo then sets a deliberately non-default split with
``config`` (60/25/15, not 50/30/20), logs one Need, one Want and one income entry
through the real CLI, shows the COMPLETE resulting sheet, and finally rolls the
ledger over into a new month with the split and the loan balances carried
forward.

    python3 examples/demo.py

Nothing here touches the network or needs credentials, and the temporary
directory is removed on exit, so it is safe to run repeatedly.
"""

import contextlib
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import finance_logger  # noqa: E402

UNCONFIGURED_HEADING = finance_logger.BUDGET_PLACEHOLDER
SAMPLE_HEADING = "50/30/20"


def run(*arguments):
    """Call the real CLI the way the command-line wrapper does.

    A ledger, usage or split error prints ``ERROR: ...`` and exits 2 instead of
    raising, which is exactly what a user sees.
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = finance_logger.main(list(arguments))
    except (finance_logger.LedgerError, ValueError) as error:
        buffer.write(f"ERROR: {error}\n")
        return 2, buffer.getvalue()
    return code, buffer.getvalue()


def show(step, title, text):
    print()
    print("=" * 72)
    print(f"STEP {step}: {title}")
    print("=" * 72)
    print(text)


def unconfigured_sample(source):
    """Return the sample sheet with its budget heading reset to the placeholder.

    A sheet nobody has configured carries the placeholder, so this is what a
    first run sees.  Nothing else about the sample is touched.
    """
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if line == SAMPLE_HEADING:
            lines[index] = UNCONFIGURED_HEADING
            break
    else:
        raise SystemExit("the sample ledger has no budget heading to reset")
    return "\n".join(lines) + "\n"


def main():
    with tempfile.TemporaryDirectory(prefix="finance-ledger-demo-") as temporary:
        work = Path(temporary)
        ledger = work / "september-expenses-log.md"
        ledger.write_text(
            unconfigured_sample(
                (ROOT / "examples" / "sample-ledger.md").read_text(encoding="utf-8")
            ),
            encoding="utf-8",
        )

        print("Working on a throwaway copy of the fictional sample ledger, with its")
        print("budget heading reset to the placeholder a brand-new sheet carries:")
        print(f"  {ledger}")

        common = [
            "--ledger", str(ledger), "--month", "September", "--date", "2026-09-12",
        ]
        changes = [
            "--entry", "42 groceries",  # classified as a Need
            "--entry", "85 streaming subscription",  # classified as a Want
            "--income", "1500",
        ]

        code, output = run("add", *common, *changes)
        if code != 2:
            print(f"FAILED: expected the refusal to exit 2, got {code}")
            return 1
        if "config" not in output:
            print("FAILED: the refusal should point at the config command")
            return 1
        show(1, "refused: no split configured yet (exit 2, nothing written)", output)

        code, output = run(
            "config", "--ledger", str(ledger), "--needs", "60", "--wants", "25",
            "--savings", "15", "--apply",
        )
        if code:
            return code
        show(2, "config: the user's own 60/25/15 split, written into the sheet", output)

        code, output = run("add", *common, *changes)
        if code:
            return code
        show(
            3,
            "dry run: one Need, one Want and one income against that split",
            output,
        )

        code, output = run("add", *common, *changes, "--apply")
        if code:
            return code
        show(4, "applied: the complete sheet now on disk", output)

        code, output = run(
            "rollover", "--ledger", str(ledger), "--to-month", "October",
            "--opening-income", "12000", "--apply",
        )
        if code:
            return code
        show(
            5,
            "rollover: the new month keeps the split and carries the loans forward",
            output,
        )

        october = work / "october-expenses-log.md"
        print()
        print(f"Both files were written inside {work}")
        print(f"  {ledger.name}  ({len(ledger.read_text().splitlines())} lines)")
        print(f"  {october.name}  ({len(october.read_text().splitlines())} lines)")
        print("The temporary directory disappears when this demo exits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
