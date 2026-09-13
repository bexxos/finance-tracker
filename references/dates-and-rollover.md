# Dates and monthly rollover

## Dating an entry

- The ledger dates undated entries in the **machine's local timezone**, or in the
  zone passed with `--timezone`. Verify the date at staging time
  (`date '+%Y-%m-%d %H:%M:%S %Z'`) rather than trusting a timestamp captured
  earlier in the conversation or in a brief.
- Relative dates ("yesterday", "on the 3rd", "for last Friday") are resolved once
  and then pinned as `--date YYYY-MM-DD`, so a delayed or retried run cannot land
  the entry on a different day.
- A batch that might cross midnight while waiting should also be pinned, because
  the day can roll over between planning and writing.
- Midnight starts a new day section. An entry dated after local midnight belongs
  under the new date heading, even if the conversation started the day before.

## Delayed approval

If a write is waiting for approval and the day changes:

- Keep the date from the original message when it is known. Do not silently move
  the entry to the approval date.
- If the intended date is unknown, ask instead of guessing.
- If one approval covers entries for several explicit dates, group them by date
  and run one plan per date; never pass mixed dates as one batch.

## Month boundary

A new month is a **new sheet**, not a new date section in the old one.

1. Take the opening income for the new month. A month with no income yet is fine
   to start, but the maximums stay at zero until income is logged.
2. Carry ongoing debt balances forward, because a balance is account state that
   survives the month.
3. Reset everything that belongs to the previous month: its expenses, its savings
   deposits, and its `Borrowed:` / `Used for loans:` deductions. Carrying those
   forward double-counts them.
4. Recompute the maximums and the remaining block from the new month's income.
   With one opening income and no expenses, remaining equals the income and the
   savings bucket equals 20 % of it.

`rollover` performs all of that:

```bash
python3 scripts/finance_logger.py rollover --ledger september-ledger.md \
  --to-month October --opening-income 12000 --apply
```

- The new file defaults to a sibling named after the target month
  (`october-expenses-log.md`); pass `--out` to choose another path.
- An existing target is never overwritten without `--force`, so a mistaken repeat
  cannot destroy a month of history.
- The year is taken from the source sheet's income lines. When the target month is
  earlier in the calendar than the source month (a December-to-January rollover,
  for example) the year advances by one.
- If the source file does not look like a ledger (no title month and no date
  headings), the command stops rather than producing an empty sheet.

The previous month's file is left untouched. That is the point of the design: the
sheet you roll over from stays the historical record.

## Reading the sheet's month and year

- The month comes from the title (`<Month> Expenses Log`), falling back to the
  first date heading. A sheet whose month cannot be detected needs `--month`.
- The year comes from the income lines, because date headings carry no year. A
  sheet with no income lines yet uses the current local year.
