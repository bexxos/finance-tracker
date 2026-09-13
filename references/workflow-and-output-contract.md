# Workflow and output contract

## The contract

After any successful change, return the **complete** sheet verbatim. The tool
prints it under a `--- FULL SHEET ---` marker; relay all of it. Do not summarise,
truncate, reformat, or rebuild it from memory, and do not paste a sheet that came
out of another agent's summary — those summaries are frequently truncated in the
middle. Read the file and print what is in it.

A summary-only reply is acceptable only when the user explicitly asked for a
summary or for a specific number.

## One batch

1. Turn the request into `AMOUNT DESCRIPTION` lines. Several entries in one
   message are one command with repeated flags, so the whole batch is planned
   against one baseline and cannot interleave with another writer.
2. Classify, unless the user labelled the entry.
3. Resolve the date once, immediately before staging.
4. Run without `--apply`, read the projected lines and the derived totals, then
   repeat the identical command with `--apply`.
5. Re-read the sheet from disk. Check, for every requested line: it appears
   exactly once, in the intended date section, with the intended label. Check the
   derived blocks against the arithmetic in `references/ledger-format.md`.
6. Return the complete sheet.

Step 5 is not optional. A successful exit code proves only that the code ran.

## Catch-up batches

When several missed updates arrive at once with explicit dates:

- Read the live sheet first, then pin every supplied date with `--date` so a
  delayed run cannot re-date the entries.
- Group entries by date and keep them in one batch; never pass mixed dates under
  one plan.
- Process the whole batch sequentially and verify each requested line
  independently. Do not reset or rebuild the month, and do not add a second
  heading for a day that already exists.

## Corrections

- The user correcting themselves ("I meant 12") means the corrected value only.
  Log the last version, never both.
- If the wrong value is already on the sheet, correct it in place: `move` when
  only the date is wrong, or a direct line replacement with a fresh read.
- To replace a line, locate the exact line in the intended date section first. If
  it is absent, stop and ask which line to replace — do not delete a same-amount
  line from another date on a guess.
- Historical months are out of scope unless the user asks for them. A prior
  month's sheet may be read to confirm it was not modified; do not offer it as a
  correction target.

## Naming and file identity

- One sheet per month, one file per sheet. Never create a same-name duplicate and
  never upload a new copy of an existing sheet.
- The canonical file is the one the user identified. Keep its identity stable;
  if the expected file is missing or renamed, report that instead of creating a
  replacement.

## When a write is blocked

If a write is blocked waiting for explicit approval and the day rolls over, do
not silently move the queued entry to the approval date. Preserve the date from
the original message when it is known; otherwise ask. When one approval covers
entries for several explicit dates, group them by date and run separate plans.
