# Classification rules

The only categorisation is Need versus Want (`[Need]` / `[Wants]`).

## Order of precedence

1. An explicit `--kind need|want` from the caller.
2. Explicit wording inside the description: "wants" or "needs" wins over every
   keyword.
3. The keyword policy: a **Want** keyword match beats a Need keyword match, so
   `400 food delivery` is a Want even though `food` is a Need keyword.
4. Otherwise Need. Any unrecognised description defaults to Need, on the
   principle that an unclassified line is more likely an essential than a
   discretionary purchase.

Matching is case-insensitive and whitespace-normalised, and it is regex-based per
keyword, so a keyword must match as a whole word.

## Default policy

`WANT_KEYWORDS` and `NEED_KEYWORDS` in `scripts/finance_logger.py` are plain
tuples at the top of the file — the policy is data, not logic.

- Wants by default: cigarettes/vapes, supplements and vitamins, mobile
  data/load, drinks, coffee, juice, snacks, fries, treats, desserts, takeout,
  delivery, restaurants, alcohol, subscriptions/streaming, gym, clothing, shoes,
  games, concerts, movies, gifts.
- Needs by default: fare and public transport, plain food, rice, bread,
  groceries, market, meals, water, medicine and pharmacy, soap/shampoo/personal
  hygiene, rent, bills, electricity, utilities, school and tuition, books,
  uniforms, laundry, parking, banking/ATM/transfer fees, personal health
  supplies.

## What the rules cannot see

Keyword rules cannot tell that:

- a purchase was made **for somebody else** (a gift or a favour is usually a
  Want by policy, but the text gives no reliable signal),
- a drink or supplement was **medically required**,
- a line is a **reclassification** of an earlier entry,
- a description is a foreign-language or abbreviated form of a known category.

When any of these apply, say so in the request and pass `--kind`, or change the
policy tuples. Do not bend the keyword list to one incident: a keyword added for
a single purchase quietly reclassifies every future entry that matches it.

## Changing the policy

1. Add a failing test to `tests/test_finance_logger.py` for the description that
   is misclassified, and run it red.
2. Change the tuple.
3. Run the full suite green, then re-run the dry run on a copy of the sheet.

Watch for prefix overlap: `food` versus `food delivery`, or `data` versus
`mobile data top-up`. Verify with the exact whole rendered line
(`splitlines()` equality), never by substring counting.

## Worked examples

| Description | Result | Why |
| --- | --- | --- |
| `198 food` | Need | plain food |
| `198 food delivery` | Want | Want keywords win |
| `80 fare` | Need | transport |
| `99 mobile data` | Want | data/load |
| `350 rent` | Need | rent |
| `500 medicine` | Need | health |
| `120 gym` | Want | membership |
| `200 food wants` | Want | explicit wording |
| `50 beer needs` | Need | explicit wording |
| `500 thingamajig` | Need | unrecognised default |
