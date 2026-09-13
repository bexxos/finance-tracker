# Duplicates, retries and concurrency

## What counts as a duplicate

- Same amount **and** description **and** label **and** date section: a duplicate.
  It is rejected, not silently skipped.
- Same amount and description on a **different date**: a legitimate second
  purchase. Log it. Repeated everyday purchases are normal, so the date section is
  part of the identity.
- The same line repeated byte-for-byte inside **one** message: one transport or
  retry artefact. The tool collapses it and reports how many lines it collapsed.
- Income is identified by its rendered line (`MM/DD/YY - <amount>`), so two
  identical income amounts on the same day are a duplicate while the same amount
  on another day is not.

## `add` is not idempotent

Every retry writes again, so before retrying:

1. Re-read the sheet from disk.
2. Check the intended date section for each requested line.
3. If every line is present exactly once, the work already happened — verify the
   totals and finish. Do not retry.
4. If lines are missing, retry only the missing ones.
5. If a retry created extras, remove only the confirmed extra lines. Never
   blanket-deduplicate a sheet.

`--event-id` adds replay protection for a transport-level repeat: after a
successful write, re-running the same event id prints the current sheet and
writes nothing.

## Unknown outcome

A failed or missing result is **not** proof that nothing was written: a writer can
change the file and then fail while producing its report. Treat that as an unknown
transaction state and read the sheet before deciding anything.

## Optimistic concurrency

The write path is deliberately paranoid, and the local file backend mirrors the
cloud one:

1. Acquire an exclusive lock (a sibling `.lock` file; an abandoned lock is
   reclaimed after a few minutes).
2. Read the baseline bytes.
3. Plan the complete replacement in memory.
4. Re-read immediately before writing — if the bytes changed, abort with a
   concurrency error and write nothing.
5. Write atomically (temporary file plus a rename in the same directory).
6. Read back and require byte-for-byte equality with what was staged.
7. Release the lock, then return the complete sheet.

Because step 4 compares the whole file, an edit made by anything else in between
is detected instead of overwritten. The correct response to that error is to
re-read, re-plan, and ask if the intervening change looks like somebody else's
work.

## Serialized writer

Only one writer, one plan, one file at a time.

- Do not dispatch a second update while a first is still in flight. Concurrent
  read-modify-write cycles lose one of the two edits.
- If a queued update cannot wait, give it an explicit barrier: re-read until the
  previous batch's marker lines are present exactly once, then rebase on that
  content. If the marker never appears, write nothing and report the blocker.
- After any write, re-read and verify both batches.

## Verification is on whole lines

Compare exact rendered lines within the intended date section, never substring
counts: a shorter line is a prefix of a longer one, so substring matching reports
a hit for the wrong entry. Count `splitlines()` matches.

A payment-sized line sitting under a date section is a red flag: debt payments
belong in the `Loans` block (see
`references/debt-and-funding-sources.md`), and a payment wrongly logged as an
expense distorts spend. After any operation that involves a payment or a removal,
re-read the sheet and check that the remaining totals moved by the expected
amount.
