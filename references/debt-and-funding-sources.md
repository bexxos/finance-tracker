# Debt, funding sources and borrowed money

## Two different things

- **A transaction** (an expense or income event) belongs under a date section and
  moves the category buckets.
- **Account state** (a loan balance, an outstanding instalment) belongs in the
  `Loans:` block and moves nothing else.

Confusing the two is the most common way this ledger goes wrong: a debt payment
logged as an expense inflates spend, depresses remaining and hides the real
balance.

## Keeping the Loans block honest

- `setloan --name X --balance N` records a current balance. A balance is what the
  account says today, not a payment.
- `payloan --name X --amount N` reduces a balance (floored at zero) and recomputes
  the section total.
- Both leave income, budget, savings and expense sections untouched.

## Where the money came from

A payment is only half the story; the funding source decides which other number
moves, so record it when the user states it.

- **From savings** — `--funded-by savings` accumulates `Used for loans: <n>` under
  the Savings section, which lowers savings remaining. Repeating the flag
  accumulates rather than replacing, so several payments in a month add up.
- **From cash flow** — `--funded-by cash` accumulates
  `Cash-funded debt payments: <n>` in the cash reconciliation, which lowers
  confirmed cash while leaving the category buckets alone.
- **Unstated** — the balance still drops and the tool says so in a note. Do not
  invent a funding source: a phantom deduction and a missing deduction are both
  wrong, and the user can tell you which one it was.
- Never record both deductions for the same payment.
- If the sheet's format has no cash line yet, add one
  (`Cash-funded debt payments: 0`) rather than corrupting a category total.

## Borrowed money is not income

Money that arrived as a loan — from family, a friend, a cash advance — is not
income. If it was logged as income:

- Remove the income line, which shrinks the maximums with it. Borrowed money that
  stays in `In:` inflates every budget maximum with money that was never the
  user's.
- If it was already spent against the inflated maximums, say so plainly when
  presenting the corrected sheet rather than quietly showing different numbers.

## Pending and not-yet-posted charges

A charge the user marks pending or not posted is not confirmed activity:

- keep it out of income lines, date sections, category spend, remaining and
  confirmed cash;
- if the user wants it visible, keep it in a labelled pending note that is
  explicitly excluded from every confirmed total;
- never present it as a posted expense.

## Working with instalment-style debt

Instalment and buy-now-pay-later debt rewards good habits rather than clever
maths:

- **Due dates first.** A missed due date usually costs more than the interest
  saved by optimising balances, so pay the nearest due date, not the largest
  balance.
- **Check the app for the real number.** Minimum amounts, grace periods and fees
  change; work from what the provider shows today, and if a policy is not
  published, say so instead of stating a figure. Never invent a rate or a fee.
- **Partials before due dates beat missing the date entirely.**
- **Restructuring may exist** (converting an instalment plan, asking support for
  relief). Verify what the provider currently offers before recommending it, and
  do not tell the user to convert a balance that is already an instalment plan.
- **Protect the living floor.** Taking the last money for food and transport to
  pay debt forces new borrowing, which is the worse cycle. Suggest a family or
  zero-interest bridge over a late fee when one is available.
- **Keep repayment money out of the expense buckets** so the budget view stays
  true, and state clearly whether a figure is budget remaining or confirmed cash
  when a cash-funded payment makes the two differ.
