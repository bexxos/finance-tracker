# Ledger format

The ledger is one Markdown file. Headings, blank lines and the labels are load
bearing: the parser matches exact lines, so reformatting a heading or a label
turns a valid sheet into a rejected one.

## Line shapes

| Line | Shape | Notes |
| --- | --- | --- |
| Title | `<Month> Expenses Log` | First non-empty line; the month is inferred from it. |
| Loans header | `Loans:` | Optional section. |
| Loan balance | `<name> - <balance>` | Whole units. Any name without a colon. |
| Income line | `MM/DD/YY - <amount>` | Two-digit month/day/year, spaced hyphen. |
| Budget heading | `<needs>/<wants>/<savings>`, for example `60/25/15` | The sheet's own split. `Needs/Wants/Savings` means no split has been configured yet, and every command that needs a maximum refuses until `config` writes one. |
| Section total | `Total: <amount>` | Appears under Loans, In, Savings and Remaining. |
| Budget maximum | `Needs - <amount> Max` / `Wants - <amount> Max` | The word `Max` is preserved when present. |
| Savings adjustments | `Borrowed: <n>` / `Used for loans: <n>` | Optional; both reduce savings remaining. |
| Cash lines | `Confirmed income: <n>`, `Confirmed posted expenses: <n>`, `Cash-funded debt payments: <n>`, `Confirmed cash remaining: <n>` | All four are rewritten together when the section exists. |
| Date heading | `<Month> <day>` | No leading zero, e.g. `September 9`. |
| Expense line | `[Need] <amount> <description>` / `[Wants] <amount> <description>` | `[Needs]`/`[Want]` are accepted on read and normalised on write. |

## Derived values

Everything below is recomputed from the sheet after any change:

- `Needs - ` and `Wants - ` maxima, and the savings maximum: the sheet's own
  split, read from its budget heading (for example `60/25/15`), applied to total
  income. Amounts are whole units; each part rounds half-to-even on its own, so
  the three parts can differ from the income total by one unit.
- `Remaining` per bucket: its maximum minus the spend in that bucket.
- `Remaining` savings: savings maximum minus `Total:`, `Borrowed:` and
  `Used for loans:`.
- `Remaining` total: the three buckets added together.
- `Confirmed posted expenses`: Needs spend plus Wants spend.
- `Confirmed cash remaining`: confirmed income minus posted expenses minus
  cash-funded debt payments.

The `Loans` block is deliberately **not** touched by a normal recalculation:
withdrawal-free balances are account state, and only `setloan`, `payloan` and
`rollover` rewrite them (recomputing the section total from its components).

## Optional versus required

Required anchors — a sheet missing any of these is rejected with a clear error:
`In:`, the budget heading (a split such as `60/25/15`, or the
`Needs/Wants/Savings` placeholder a sheet carries until `config` runs),
`Savings:` with its `Total:`, `Remaining:` with `Needs - `,
`Wants - `, `Savings - ` and `Total:`, the `In:` total line, and both budget
maximum lines.

Optional — skipped when absent: `Loans:`, `Cash Reconciliation:`,
`Pending / Not Posted:`, `Borrowed:`, `Used for loans:`. A cash-funded debt
payment is refused with an explanatory error when the cash section has no
`Cash-funded debt payments:` line, because silently creating a second cash
convention is worse than asking for one line.

## Amounts

Amounts are whole units (`198`, `1,200`, `₱1,200`). Fractional amounts are
rejected rather than rounded: rounding money silently is how a ledger stops
matching a bank statement. Income is always positive.

## Structural care

- The date sections live after the summary blocks. A new day is appended, never
  inserted in the middle, so existing text stays byte-stable.
- A day is assumed to belong to the month in the title and the date headings.
  Logging a date from another month into a sheet is a mismatch, not a new month:
  roll the month over instead.
- The parser reads the year from the income lines (`MM/DD/YY`). A sheet with no
  income yet falls back to the current local year.
