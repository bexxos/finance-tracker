---
name: finance-ledger
description: "Use when logging personal-finance ledger entries, reporting the budget sheet, or rolling a month over. Deterministic local-first Markdown ledger: Need/Want classification, local-timezone dating, a 50/30/20 budget view, and complete-sheet output."
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [finance, ledger, budget, need-want, 50-30-20, rollover, markdown, local-first]
---

# Finance ledger

A deterministic personal-finance ledger that lives in one Markdown file the user
owns. The user sends entries in chat; the tool parses them, classifies each as a
Need or a Want, dates them in the user's local timezone, keeps the 50/30/20
budget view and the monthly rollover consistent, and prints the **complete**
updated sheet.

The default storage backend is a local file: no account, no credentials, no
network. Standard library only. Optional cloud storage is a separate, opt-in
adapter (see *Storage backends*).

## Output contract

**Every successful update ends with the COMPLETE sheet, verbatim.** The tool
prints it after the status and summary lines; relay all of it. Never replace the
sheet with a summary-only receipt, a "logged it" confirmation, or a head/tail
excerpt, and never copy a sheet out of another agent's summary — read the file
and print what is in it. A one-line routine entry still returns the whole sheet.

Negative remaining is a normal, useful signal (the budget is overdrawn). Show it
rather than hiding it.

## Sheet shape

`templates/blank-finance-sheet.txt` is a complete, consistent, all-zero sheet.
Sections, in order:

```text
<Month> Expenses Log

Loans:                       # optional
<name> - <balance>
Total: <sum>

In:
MM/DD/YY - <amount>
Total: <sum>

50/30/20
Needs - <max> Max
Wants - <max> Max

Savings:
[Borrowed: <n>]              # optional
[Used for loans: <n>]        # optional
Total: <n>

Cash Reconciliation:         # optional
Confirmed income: <n>
Confirmed posted expenses: <n>
Cash-funded debt payments: <n>
Confirmed cash remaining: <n>

Pending / Not Posted:        # optional, informational, never counted
<label>: <amount> (excluded from confirmed cash)

Remaining:
Needs - <n>
Wants - <n>
Savings - <n>
Total: <n>

<Month> <day>
[Need] <amount> <description>
[Wants] <amount> <description>
```

Invariants the tool maintains: `Loans` total equals the sum of its balances, the
50/30/20 lines are 50/30/20 % of total income, `Savings` remaining subtracts
`Total`, `Borrowed` and `Used for loans`, `Remaining` per bucket is its maximum
minus its spend, `Remaining` total is the sum of the three buckets, and
`Confirmed cash remaining` is income minus posted expenses minus cash-funded debt
payments. Details: `references/ledger-format.md`.

## Commands

The ledger path comes from `--ledger`, else `$FINANCE_LEDGER_PATH`, else
`./finance-ledger.md`. Every command is a dry run that prints the full projected
sheet; `--apply` is the only switch that writes.

```bash
# Log a batch (repeat --entry). Default date is today in the local timezone.
python3 scripts/finance_logger.py add --ledger ledger.md \
  --entry '42 groceries' --entry '85 streaming subscription' --date 2026-09-12

# Log income (repeat --income); the budget maximums grow with it.
python3 scripts/finance_logger.py add --ledger ledger.md --income 1500 --apply

# Move an already-logged line to another date; spend and totals do not change.
python3 scripts/finance_logger.py move --ledger ledger.md \
  --from-date 2026-09-12 --to-date 2026-09-13 --entry '42 groceries'

# Debt: a balance or a payment never becomes an expense line.
python3 scripts/finance_logger.py setloan --ledger ledger.md --name 'Loan A' --balance 900
python3 scripts/finance_logger.py payloan --ledger ledger.md --name 'Loan A' \
  --amount 300 --funded-by savings

# Open the next month; loan balances carry forward, everything else resets.
python3 scripts/finance_logger.py rollover --ledger ledger.md \
  --to-month October --opening-income 12000
```

Add `--kind need|want` to override the classifier for a whole batch, `--month` to
state the sheet month explicitly, `--event-id` for replay protection, and
`--timezone` to date undated entries outside the machine's local zone. `--apply`
on `rollover` writes a new file and refuses to overwrite an existing month unless
`--force` is given.

## Workflow

1. Parse each request as `AMOUNT DESCRIPTION`. Several lines in one message are
   one batch (`--entry` per line, one command). Collapse a byte-for-byte repeated
   line inside one message to a single entry.
2. Classify unless the user labelled it. Explicit "need"/"want" wording inside a
   description wins; otherwise the keyword policy in
   `scripts/finance_logger.py` decides; anything unrecognised defaults to Need.
   Rules and how to extend them: `references/classification-rules.md`.
3. Date it. Undated entries use the local date; check the machine's local date
   immediately before staging, because a task deferred across midnight must not
   silently land on the next day. Pass `--date YYYY-MM-DD` for "yesterday", a
   named day, or any historical batch. Details:
   `references/dates-and-rollover.md`.
4. Dry run first, confirm the projected lines and the derived totals, then repeat
   with `--apply`.
5. Re-read the ledger from disk and verify: each requested rendered line appears
   exactly once in its intended date section, and the derived blocks match the
   arithmetic. Then return the complete sheet.
6. On a failed or unknown-outcome run, read the sheet before retrying: `add` is
   not idempotent. `references/duplicates-and-concurrency.md`.

## Scope rules worth stating up front

- Correct a wrong amount or description by moving or replacing the exact line,
  never by logging both versions. The last message wins; when the line to remove
  cannot be identified exactly, stop and ask instead of guessing.
- A charge the user marks pending or not posted is not confirmed activity: keep
  it out of income, category spend, remaining and confirmed cash.
- A debt payment is not a Need/Want expense. It belongs in the `Loans` block, and
  its funding source (savings vs cash) is recorded separately:
  `references/debt-and-funding-sources.md`.
- Never rewrite or rebuild the sheet from memory, and never create a second file
  with the same month's name. Read the live file, change only what was asked.

## Storage backends

- `local` (default, in `scripts/finance_logger.py`): one Markdown file on this
  machine. Works offline with nothing installed.
- `drive` (opt-in, `scripts/drive_adapter.py`): keeps the same sheet in one cloud
  file so several machines share it. It needs the user's own credentials and the
  Google client libraries; it is never imported by the default path.

## References

- `references/ledger-format.md` — section-by-section line shapes and invariants.
- `references/workflow-and-output-contract.md` — the per-entry workflow, batching,
  catch-up entry, corrections, and verification.
- `references/classification-rules.md` — the Need/Want policy and how to change it.
- `references/dates-and-rollover.md` — local dating, pinned dates, month boundary.
- `references/duplicates-and-concurrency.md` — duplicate handling, retries,
  optimistic concurrency, serialized writes.
- `references/debt-and-funding-sources.md` — loans, funding sources, borrowed
  money, pending charges, instalment-style debt.

## Pitfalls

- **Scope income insertion to the `In:` block.** The sheet contains more than one
  `Total:` line (loans, income, savings, remaining), so a naive "replace the first
  total" edit puts an income line inside `Loans:` and still exits 0.
- **Update the file in place.** Creating a second copy of the sheet is a data
  split, not a backup.
- **Test recomputation with a changing input.** Recomputing with unchanged values
  can coincide with the old numbers and hide a broken rule; change income and
  check the maximums move.
- **Verify against whole rendered lines, not substrings.** `[Wants] 99 mobile data`
  is a prefix of `[Wants] 99 mobile data top-up`; compare `splitlines()` entries.
- **Do not hardcode "today".** A brief written for a later run goes stale across
  midnight; pass an explicit date or let the tool date it at staging time.
- **Escape replacement strings correctly.** When a file rewrite is built with
  format strings plus a regex substitution, a group reference needs one level of
  escaping; verify with a dry run on a copy first.
- **A sheet with no income yet is normal** (a fresh month): entries are accepted,
  the maximums sit at zero, and remaining goes negative until income lands.
