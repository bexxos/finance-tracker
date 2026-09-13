# finance-ledger

A deterministic personal-finance ledger in a single Markdown file. Log an expense
or an income line in chat and the tool classifies it as a Need or a Want, dates it
in your local timezone, keeps a budget view built from your own Needs/Wants/Savings
split in step, and prints the **complete** updated sheet. The repo root *is* the agent skill: `SKILL.md` teaches
the agent, `scripts/` does the work, and the default storage backend is a plain
file on your machine — no account, no credentials, no network, standard library
only.

## Install it as a skill

Clone the repo into the skills directory your agent reads. For a Hermes profile:

```bash
git clone https://github.com/bexxos/finance-tracker.git ~/.hermes/skills/productivity/finance-ledger
```

Then start a new session: skills are scanned when a session loads, so a new chat
picks the skill up (some setups also expose a reload or refresh-skills command).
Use the same shape for other agents:

| Agent | Typical install location |
| --- | --- |
| Hermes | `~/.hermes/skills/<category>/finance-ledger/` |
| Other frameworks | `<agent-config>/skills/finance-ledger/`, e.g. `~/.claude/skills/finance-ledger/` |

Honest note on portability: `SKILL.md` carries Hermes-style YAML frontmatter
(`name`, `description`, plus a small `metadata.hermes` block for tags). Frameworks
with their own skill format — Claude Code, Cursor rules, and others — will usually
need only the frontmatter adapted; the instructions in the body, the scripts, the
tests and the examples are plain files with no framework dependency.

## First run

There is no default split. Decide the percentages that match your own budget and
write them into the sheet before the first entry:

```bash
python3 scripts/finance_logger.py config --ledger ledger.md \
  --needs 60 --wants 25 --savings 15           # dry run: prints the whole sheet
python3 scripts/finance_logger.py config --ledger ledger.md \
  --needs 60 --wants 25 --savings 15 --apply   # writes it
```

Needs are the essentials the month cannot avoid (rent, food, transport, bills,
medicine); Wants are discretionary (takeout, subscriptions, treats, games);
Savings is what is set aside. The three values are whole numbers from 0 to 100
that must add up to exactly 100.

The split is stored in the sheet's own budget heading (`60/25/15`), so the file
stays self-contained and nothing can drift away from what it displays. A sheet
whose heading is already a split (the fictional sample ledger still carries
`50/30/20`) keeps working exactly as it is. Every budget maximum and remaining
figure is computed from it, and any command that needs a maximum refuses to run
until a split exists, rather than assuming 50/30/20. Run `config` again to change
it: the maximums are recomputed, and entries already logged keep the Need/Want
label they were given.

## Try it

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # test suite, no network or credentials
python3 examples/demo.py                               # unconfigured run, then config, entries, rollover
```

The demo copies the fictional `examples/sample-ledger.md` into a temporary
directory with its budget heading reset to the placeholder a brand-new sheet
carries. It shows the refusal of an entry before any split is configured, sets a
non-default split with `config`, logs one Need, one Want and one income entry
through the real CLI, prints the complete resulting sheet, then rolls the sheet
over to a new month with the split and the loan balances carried forward and
prints that sheet too. It writes only inside a temporary directory that is
deleted on exit.

## Use it

The ledger path comes from `--ledger`, else `$FINANCE_LEDGER_PATH`, else
`./finance-ledger.md`. Every command is a dry run that prints the full projected
sheet; `--apply` is the only switch that writes.

```bash
# Set your own split once; every budget maximum is computed from it.
python3 scripts/finance_logger.py config --needs 60 --wants 25 --savings 15 --apply

# Log a batch (repeat --entry). The date defaults to today in your local timezone.
python3 scripts/finance_logger.py add --entry '42 groceries' \
  --entry '85 streaming subscription' --date 2026-09-12 --apply

# Income grows the budget maximums.
python3 scripts/finance_logger.py add --income 1500 --apply

# Fix a date; spend and totals do not change.
python3 scripts/finance_logger.py move --from-date 2026-09-12 --to-date 2026-09-13 \
  --entry '42 groceries' --apply

# Debt lives in the Loans block, never in the expense buckets.
python3 scripts/finance_logger.py setloan --name 'Loan A' --balance 900 --apply
python3 scripts/finance_logger.py payloan --name 'Loan A' --amount 300 \
  --funded-by savings --apply

# Open the next month: loan balances carry forward, the rest resets.
python3 scripts/finance_logger.py rollover --to-month October \
  --opening-income 12000 --apply
```

Start a sheet with `templates/blank-finance-sheet.txt`, and read `SKILL.md` for
the agent-facing workflow, the output contract and the pitfalls. The sheet's
structure and every derived number are documented in
`references/ledger-format.md`.

## Layout

```text
SKILL.md                       agent-facing instructions (install target)
README.md                      this file
scripts/finance_logger.py      core planner + default local-file backend
scripts/drive_adapter.py       OPT-IN cloud backend (your own credentials)
tests/test_finance_logger.py   111 standard-library tests, offline
references/*.md                format, workflow, classification, dates,
                               duplicates/concurrency, debt
templates/blank-finance-sheet.txt
examples/sample-ledger.md      fictional, internally consistent sheet
examples/demo.py               runnable end-to-end demo
```

## Storage backends

- **local** (default): one Markdown file. Two test classes prove the default path
  needs no Google library and keeps working with the socket layer disabled.
- **drive** (opt-in): the same sheet in one cloud file, so several machines share
  it. It needs your own OAuth credentials and the Google client libraries
  (`pip install google-api-python-client google-auth`), plus the file and folder
  ids:

  ```bash
  python3 scripts/drive_adapter.py add --file-id YOUR_FILE_ID \
    --parent-id YOUR_FOLDER_ID --file-name 'September Expenses Log.md' \
    --month September --entry '33 bread' --apply
  ```

  Both backends plan with the same core, update the file in place (never a second
  copy), re-read before writing to catch concurrent edits, and verify the result
  byte-for-byte afterwards.

## What this deliberately does not do

- **No bank or card connection.** Entries are what you type; nothing is scraped.
- **No automatic categorisation beyond its keyword rules.** A purchase for someone
  else, a medically needed item or a reclassification is not detectable from the
  text — override it per batch with `--kind`.
- **No assumed budget split.** Nothing is logged against percentages you never
  chose: the split is required before the first entry, and it is yours.
- **No cloud sync by default**, and no telemetry: without the opt-in adapter this
  tool performs no network calls at all.
- **No forecasting, multi-currency conversion, investing or tax features.** It is
  one ledger, one month, one currency.
- **No summary-only replies**: the complete sheet is the deliverable.

## Requirements

Python 3.9+ (uses `zoneinfo`, so 3.9 or newer with timezone data present).
Standard library only. Tests: `python3 -m unittest discover -s tests -p 'test_*.py'`.

## License

MIT — see `LICENSE`.
