#!/usr/bin/env python3
"""Deterministic, local-first personal-finance ledger updater.

The ledger is a single plain Markdown file that the user owns.  This module
parses it, classifies each new expense as a Need or a Want, dates it in the
user's local timezone, keeps the budget view built from the user's own
Needs/Wants/Savings split and the monthly rollover consistent, and always
renders the COMPLETE updated sheet.

The split is not built in.  The sheet's budget heading *is* the split, written as
``60/25/15``, and a sheet that has never been configured carries the
``Needs/Wants/Savings`` placeholder instead.  Any command that needs a budget
maximum refuses to run until the user's own percentages are set with ``config``,
so nothing silently assumes somebody else's 50/30/20.

Storage is an edge concern:

* ``local`` (the default) reads and writes one Markdown file on this machine.
  No account, no credentials, no network.  The parsing/reconciliation core in
  this module imports nothing beyond the Python standard library.
* ``drive`` is handled by the separate, opt-in ``drive_adapter.py`` module,
  which needs the user's own Google credentials and third-party client
  libraries.  This module never imports it.

Nothing here connects to a bank, and no category is invented beyond the default
keyword policy below.

Sheet shape (see ``templates/blank-finance-sheet.txt``)::

    <Month> Expenses Log

    Loans:                     # optional; carried across a rollover
    <name> - <balance>
    Total: <sum>

    In:
    MM/DD/YY - <amount>
    Total: <sum>

    <needs>/<wants>/<savings>  # the user's split; unconfigured until `config`
    Needs - <max> Max
    Wants - <max> Max

    Savings:
    [Borrowed: <n>]            # optional
    [Used for loans: <n>]      # optional
    Total: <n>

    Cash Reconciliation:       # optional
    Confirmed income: <n>
    Confirmed posted expenses: <n>
    Cash-funded debt payments: <n>
    Confirmed cash remaining: <n>

    Pending / Not Posted:      # optional, informational, never counted
    <label>: <amount> (excluded from confirmed cash)

    Remaining:
    Needs - <n>
    Wants - <n>
    Savings - <n>
    Total: <n>

    <Month> 1
    [Need] 80 fare
    [Wants] 99 mobile data
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Default classification policy
# --------------------------------------------------------------------------- #
# These two tuples *are* the classification policy, and they are deliberately
# plain data rather than code: edit them to match how you think about your own
# spending.  A Want keyword always beats a Need keyword, and a description that
# matches neither list is treated as a Need, so an unrecognised entry is assumed
# to be an essential rather than discretionary.
#
# The rules cannot tell that a purchase was made for somebody else, that a
# drink was medically required, or that a line is a reclassification.  When they
# get it wrong, say so in the request and pass ``--kind need``/``--kind want``.
WANT_KEYWORDS = (
    r"\bcig(?:arette)?s?\b",
    r"\bvap(?:e|es|ing)\b",
    r"\bsupplement(?:s)?\b",
    r"\bvitamin(?:s)?\b",
    r"\bfish oil\b",
    r"\bmobile[ -]?data\b",
    r"\b(?:phone|data|load)[ -]?top[ -]?up\b",
    r"\bdrink(?:s)?\b",
    r"\bjuice\b",
    r"\bsoda\b",
    r"\bmilk tea\b",
    r"\bcoffee\b",
    r"\bsnack(?:s)?\b",
    r"\bfries\b",
    r"\btreat(?:s)?\b",
    r"\bdessert(?:s)?\b",
    r"\bice cream\b",
    r"\btakeout\b",
    r"\bdelivery\b",
    r"\brestaurant\b",
    r"\balcohol\b",
    r"\bbeer\b",
    r"\bwine\b",
    r"\bsubscription(?:s)?\b",
    r"\bstreaming\b",
    r"\bgym\b",
    r"\bclothes\b",
    r"\bclothing\b",
    r"\bshirt\b",
    r"\bhoodie\b",
    r"\bshoes?\b",
    r"\bsneakers?\b",
    r"\bgame(?:s)?\b",
    r"\bconcert\b",
    r"\bmovie(?:s)?\b",
    r"\bgift(?:s)?\b",
)

NEED_KEYWORDS = (
    r"\bfare\b",
    r"\btransport\b",
    r"\bbus\b",
    r"\bjeep\b",
    r"\btrain\b",
    r"\bmrt\b",
    r"\blrt\b",
    r"\btaxi\b",
    r"\btricycle\b",
    r"\bfood\b",
    r"\brice\b",
    r"\bbread\b",
    r"\bgrocer(?:y|ies)\b",
    r"\bmarket\b",
    r"\bmeal(?:s)?\b",
    r"\bwater\b",
    r"\bmedicine(?:s)?\b",
    r"\bmeds?\b",
    r"\bpharmacy\b",
    r"\bsoap\b",
    r"\bshampoo\b",
    r"\btoothpaste\b",
    r"\bdeodorant\b",
    r"\bhygiene\b",
    r"\btissue(?:s)?\b",
    r"\bpads\b",
    r"\bsanitary\b",
    r"\bcondoms?\b",
    r"\brent\b",
    r"\bbill(?:s)?\b",
    r"\belectric(?:ity)?\b",
    r"\butilit(?:y|ies)\b",
    r"\bschool\b",
    r"\btuition\b",
    r"\bbook(?:s)?\b",
    r"\buniform\b",
    r"\blaundry\b",
    r"\bparking\b",
    r"\bbank(?:ing)?\s+fee\b",
    r"\batm\s+fee\b",
    r"\btransfer\s+fee\b",
)

# --------------------------------------------------------------------------- #
# Sheet vocabulary
# --------------------------------------------------------------------------- #
TITLE_SUFFIX = "Expenses Log"
LOANS_SECTION = "Loans:"
IN_SECTION = "In:"
SAVINGS_SECTION = "Savings:"
CASH_SECTION = "Cash Reconciliation:"
PENDING_SECTION = "Pending / Not Posted:"
REMAINING_SECTION = "Remaining:"

TOTAL_PREFIX = "Total: "
NEEDS_PREFIX = "Needs - "
WANTS_PREFIX = "Wants - "
SAVINGS_LINE_PREFIX = "Savings - "
BORROWED_PREFIX = "Borrowed: "
USED_FOR_LOANS_PREFIX = "Used for loans: "
CONFIRMED_INCOME_PREFIX = "Confirmed income: "
CONFIRMED_EXPENSES_PREFIX = "Confirmed posted expenses: "
CASH_FUNDED_PREFIX = "Cash-funded debt payments: "
CONFIRMED_CASH_PREFIX = "Confirmed cash remaining: "

# The budget heading is not a fixed string: it *is* the user's own split, written
# as NN/NN/NN.  A sheet nobody has configured yet carries this placeholder
# instead, which is deliberately not a split, so no default is ever assumed.
BUDGET_PLACEHOLDER = "Needs/Wants/Savings"

_MAJOR_HEADINGS = frozenset(
    {
        LOANS_SECTION,
        IN_SECTION,
        SAVINGS_SECTION,
        CASH_SECTION,
        PENDING_SECTION,
        REMAINING_SECTION,
    }
)

# The command that sets a split, quoted in the refusal messages.
CONFIG_EXAMPLE = (
    "python3 scripts/finance_logger.py config "
    "--needs 60 --wants 25 --savings 15 --apply"
)

# --------------------------------------------------------------------------- #
# Lexical patterns
# --------------------------------------------------------------------------- #
_AMOUNT_RE = re.compile(r"^\s*[₱$Pp]?\s*([\d,]+(?:\.\d+)?)\s+(.+?)\s*$")
_SPLIT_LABEL_RE = re.compile(r"^(\d{1,3})/(\d{1,3})/(\d{1,3})$")
_INCOME_RE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{2})\s+-\s+([\d,]+)\s*$")
_EXPENSE_RE = re.compile(r"^\s*\[(Need|Needs|Want|Wants)\]\s+([\d,]+)\s+(.+?)\s*$")
_DATE_HEADING_RE = re.compile(r"^([A-Za-z]+) (\d{1,2})$")
_TRAILING_NUMBER_RE = re.compile(r"(-?\d+)\s*$")
_TOTAL_LINE_RE = re.compile(r"Total: -?\d+")
_LOAN_LINE_RE = re.compile(r"^(?P<name>[^:]+?)\s+-\s+(?P<balance>\d+)$")
_TITLE_RE = re.compile(r"^([A-Za-z]+)\s+" + TITLE_SUFFIX + r"$")
_EXPLICIT_WANT_RE = re.compile(r"\bwants?\b")
_EXPLICIT_NEED_RE = re.compile(r"\bneeds?\b")
_WANT_PATTERNS = tuple(re.compile(pattern) for pattern in WANT_KEYWORDS)
_NEED_PATTERNS = tuple(re.compile(pattern) for pattern in NEED_KEYWORDS)


class LedgerError(RuntimeError):
    """Base class for safe planning and storage errors."""


class LedgerFormatError(LedgerError):
    """The ledger file does not match the supported sheet structure."""


class DuplicateEntryError(LedgerError):
    """An exact same-date entry already exists or is repeated in a batch."""


class BudgetNotConfiguredError(LedgerError):
    """The sheet has no Needs/Wants/Savings split yet, so no maximum exists."""


class ConcurrentModificationError(LedgerError):
    """The ledger changed while an update was staged."""


@dataclass(frozen=True)
class Entry:
    """One rendered expense line: ``[Need] 80 fare``."""

    date: date
    amount: int
    description: str
    kind: str

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        if self.kind not in {"need", "want"}:
            raise ValueError("kind must be 'need' or 'want'")
        if not self.description.strip():
            raise ValueError("description must not be empty")

    @property
    def rendered(self) -> str:
        label = "Need" if self.kind == "need" else "Wants"
        return f"[{label}] {self.amount} {self.description}"

    @property
    def identity_key(self) -> tuple[str, int, str, str]:
        return (
            self.date.isoformat(),
            self.amount,
            self.kind,
            normalize_description(self.description),
        )


@dataclass(frozen=True)
class IncomeEntry:
    """One rendered income line: ``09/01/26 - 10000``."""

    date: date
    amount: int

    @property
    def rendered(self) -> str:
        return f"{self.date:%m/%d/%y} - {self.amount}"

    @property
    def identity_key(self) -> tuple[str, int]:
        return self.date.isoformat(), self.amount


@dataclass(frozen=True)
class BudgetSplit:
    """The user's own Needs/Wants/Savings percentages.

    Whole numbers from 0 to 100 that add up to exactly 100.  They live in the
    sheet's budget heading, so the file explains itself and no sidecar state can
    drift away from what the sheet displays.
    """

    needs: int
    wants: int
    savings: int

    def __post_init__(self) -> None:
        for name in ("needs", "wants", "savings"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"the {name} percentage must be a whole number between 0 and 100"
                )
            if not 0 <= value <= 100:
                raise ValueError(
                    f"the {name} percentage must be between 0 and 100 (got {value})"
                )
        total = self.needs + self.wants + self.savings
        if total != 100:
            raise ValueError(
                "the Needs/Wants/Savings percentages must add up to exactly 100 "
                f"(got {self.needs} + {self.wants} + {self.savings} = {total}); "
                f"for example: {CONFIG_EXAMPLE}"
            )

    @property
    def label(self) -> str:
        """The budget heading that carries this split, for example ``60/25/15``."""
        return f"{self.needs}/{self.wants}/{self.savings}"

    def maxima(self, income_total: int) -> tuple[int, int, int]:
        """Whole-unit (needs, wants, savings) maximums for a total income.

        Each part rounds half-to-even on its own, so the three parts can differ
        from the income total by a unit.
        """
        return (
            _round_units(Decimal(income_total) * self.needs / 100),
            _round_units(Decimal(income_total) * self.wants / 100),
            _round_units(Decimal(income_total) * self.savings / 100),
        )


@dataclass(frozen=True)
class LedgerSummary:
    """Every derived number the tool maintains on the sheet."""

    income_total: int
    needs_max: int
    wants_max: int
    savings_max: int
    needs_spent: int
    wants_spent: int
    needs_remaining: int
    wants_remaining: int
    savings_remaining: int
    remaining_total: int
    posted_expenses: int
    cash_funded_debt: int
    confirmed_cash: int


@dataclass(frozen=True)
class LedgerPlan:
    """A complete projected sheet plus the numbers behind it."""

    text: str
    summary: LedgerSummary
    added: tuple[Entry, ...]
    added_income: tuple[IncomeEntry, ...] = ()
    note: str = ""


# --------------------------------------------------------------------------- #
# Classification and parsing
# --------------------------------------------------------------------------- #
def normalize_description(description: str) -> str:
    """Normalize whitespace and case for identity comparisons only."""
    return " ".join(description.strip().split()).casefold()


def classify_description(description: str, explicit_kind: str | None = None) -> str:
    """Return ``"need"`` or ``"want"`` for a description, deterministically."""
    if explicit_kind is not None:
        value = explicit_kind.casefold().strip()
        if value in {"need", "needs"}:
            return "need"
        if value in {"want", "wants"}:
            return "want"
        raise ValueError("explicit_kind must be need(s) or want(s)")

    text = normalize_description(description)
    if _EXPLICIT_WANT_RE.search(text):
        return "want"
    if _EXPLICIT_NEED_RE.search(text):
        return "need"
    if any(pattern.search(text) for pattern in _WANT_PATTERNS):
        return "want"
    # Needs, including anything unrecognised: ambiguous entries default to Need.
    return "need"


def parse_entry(raw: str, entry_date: date, explicit_kind: str | None = None) -> Entry:
    """Parse ``AMOUNT DESCRIPTION`` into a normalized ledger entry."""
    match = _AMOUNT_RE.fullmatch(raw)
    if not match:
        raise ValueError(
            "entry must look like 'AMOUNT DESCRIPTION', for example '198 food'"
        )

    amount_text, description = match.groups()
    try:
        amount_decimal = Decimal(amount_text.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {amount_text}") from exc
    if amount_decimal != amount_decimal.to_integral_value():
        raise ValueError("ledger amounts must be whole units")

    description = " ".join(description.split())
    kind = classify_description(description, explicit_kind)
    return Entry(entry_date, int(amount_decimal), description, kind)


def parse_income(raw_amount: str, entry_date: date) -> IncomeEntry:
    """Parse a whole-unit income amount."""
    cleaned = raw_amount.strip().replace("₱", "").replace("$", "").replace(",", "")
    try:
        amount_decimal = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"invalid income amount: {raw_amount}") from exc
    if amount_decimal <= 0 or amount_decimal != amount_decimal.to_integral_value():
        raise ValueError("income must be a positive whole-unit amount")
    return IncomeEntry(entry_date, int(amount_decimal))


def _round_units(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))


def month_number(month: str) -> int:
    """Return 1-12 for a full English month name."""
    for number in range(1, 13):
        if calendar.month_name[number].casefold() == month.strip().casefold():
            return number
    raise LedgerFormatError(f"invalid month name: {month}")


def month_name(number: int) -> str:
    return calendar.month_name[number]


def canonical_month(month: str) -> str:
    return month_name(month_number(month))


# --------------------------------------------------------------------------- #
# Sheet navigation
# --------------------------------------------------------------------------- #
def _split_text(text: str) -> tuple[list[str], str]:
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), newline


def _join_lines(lines: Sequence[str], newline: str, trailing_newline: bool) -> str:
    result = newline.join(lines)
    if trailing_newline:
        result += newline
    return result


def _unique_index(lines: Sequence[str], value: str) -> int:
    matches = [index for index, line in enumerate(lines) if line == value]
    if len(matches) != 1:
        raise LedgerFormatError(
            f"expected exactly one {value!r} line, found {len(matches)}"
        )
    return matches[0]


def _is_budget_heading(line: str) -> bool:
    """True for the budget heading: the sheet's split, or the placeholder."""
    return line == BUDGET_PLACEHOLDER or _SPLIT_LABEL_RE.fullmatch(line) is not None


def _is_major_heading(line: str) -> bool:
    return line in _MAJOR_HEADINGS or _is_budget_heading(line)


def _major_heading_indices(lines: Sequence[str]) -> list[int]:
    return [index for index, line in enumerate(lines) if _is_major_heading(line)]


def _budget_heading_index(lines: Sequence[str]) -> int:
    """Return the index of the sheet's single budget heading line."""
    matches = [index for index, line in enumerate(lines) if _is_budget_heading(line)]
    if len(matches) != 1:
        raise LedgerFormatError(
            "expected exactly one budget heading line (a split like '60/25/15', or "
            f"the {BUDGET_PLACEHOLDER!r} placeholder), found {len(matches)}"
        )
    return matches[0]


def parse_split_label(label: str) -> BudgetSplit | None:
    """Return the split a budget heading encodes, or None for the placeholder."""
    match = _SPLIT_LABEL_RE.fullmatch(label)
    if match is None:
        return None
    try:
        return BudgetSplit(
            needs=int(match.group(1)),
            wants=int(match.group(2)),
            savings=int(match.group(3)),
        )
    except ValueError as exc:
        raise LedgerFormatError(
            f"the budget heading {label!r} is not a usable split: {exc}"
        ) from exc


def read_budget_split(lines: Sequence[str]) -> BudgetSplit | None:
    """Return the sheet's configured split, or None when it has none yet."""
    return parse_split_label(lines[_budget_heading_index(lines)])


def require_budget_split(
    lines: Sequence[str], *, where: str = "this ledger"
) -> BudgetSplit:
    """Return the sheet's split, refusing when the user has not set one."""
    split = read_budget_split(lines)
    if split is None:
        raise BudgetNotConfiguredError(
            f"{where} has no Needs/Wants/Savings split yet, so no budget maximum "
            "can be computed.  Run the config command first, for example: "
            f"{CONFIG_EXAMPLE}"
        )
    return split


def _section_end(lines: Sequence[str], start: int) -> int:
    later = [index for index in _major_heading_indices(lines) if index > start]
    return min(later) if later else len(lines)


def _section_span(lines: Sequence[str], heading: str) -> tuple[int, int] | None:
    """Return the (start, end) span of an optional section, or None if absent."""
    matches = [index for index, line in enumerate(lines) if line == heading]
    if not matches:
        return None
    if len(matches) > 1:
        raise LedgerFormatError(f"duplicate section heading: {heading}")
    start = matches[0]
    return start, _section_end(lines, start)


def _date_headings(lines: Sequence[str], month: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _DATE_HEADING_RE.fullmatch(line)
        if match and match.group(1).casefold() == month.casefold():
            result.append((index, int(match.group(2))))
    return result


def _date_bounds(lines: list[str], target_date: date, month: str) -> tuple[int, int]:
    heading = f"{month} {target_date.day}"
    matches = [index for index, line in enumerate(lines) if line == heading]
    if len(matches) > 1:
        raise LedgerFormatError(f"duplicate date heading: {heading}")
    if matches:
        start = matches[0]
        following = [index for index, _ in _date_headings(lines, month) if index > start]
        return start, min(following) if following else len(lines)

    # A new date section is appended at the end.  Existing text is never
    # removed; only formatting blanks are trimmed from the insertion boundary.
    while lines and lines[-1] == "":
        lines.pop()
    if lines:
        lines.append("")
    lines.append(heading)
    return len(lines) - 1, len(lines)


def sheet_month(lines: Sequence[str]) -> str | None:
    """Infer the sheet's month from its title, then from its date headings."""
    for index in range(min(3, len(lines))):
        match = _TITLE_RE.fullmatch(lines[index].strip())
        if match:
            try:
                return canonical_month(match.group(1))
            except LedgerFormatError:
                pass
    for line in lines:
        match = _DATE_HEADING_RE.fullmatch(line)
        if match:
            try:
                return canonical_month(match.group(1))
            except LedgerFormatError:
                return None
    return None


def sheet_year(lines: Sequence[str]) -> int:
    """Infer the sheet's year from its income lines, else the local year.

    Date headings carry no year, so the income lines are authoritative.  An old
    file with no income yet falls back to the current local year.
    """
    span = _section_span(lines, IN_SECTION)
    if span is not None:
        for line in lines[span[0] + 1 : span[1]]:
            match = _INCOME_RE.fullmatch(line)
            if match:
                return 2000 + int(match.group(3))
    return datetime.now(local_timezone()).year


def _prefixed_index(
    lines: Sequence[str], start: int, end: int, prefix: str
) -> int | None:
    matches = [index for index in range(start, end) if lines[index].startswith(prefix)]
    if len(matches) > 1:
        raise LedgerFormatError(
            f"expected at most one {prefix.strip()!r} line, found {len(matches)}"
        )
    return matches[0] if matches else None


def _read_prefixed_int(
    lines: Sequence[str], start: int, end: int, prefix: str, default: int | None = 0
) -> int | None:
    index = _prefixed_index(lines, start, end, prefix)
    if index is None:
        return default
    match = _TRAILING_NUMBER_RE.search(lines[index][len(prefix) :])
    if not match:
        raise LedgerFormatError(f"expected a number after {prefix!r}")
    return int(match.group(1))


def _set_prefixed(
    lines: list[str],
    start: int,
    end: int,
    prefix: str,
    value: int,
    *,
    keep_max_suffix: bool = False,
) -> None:
    index = _prefixed_index(lines, start, end, prefix)
    if index is None:
        raise LedgerFormatError(
            f"expected one line beginning {prefix.strip()!r} in this section, found none"
        )
    suffix = lines[index][len(prefix) :]
    if keep_max_suffix and suffix.strip().endswith("Max"):
        lines[index] = f"{prefix}{value} Max"
    else:
        lines[index] = f"{prefix}{value}"


def _bump_prefixed(
    lines: list[str],
    span: tuple[int, int],
    prefix: str,
    delta: int,
    *,
    insert_before: str | None = None,
) -> None:
    """Add ``delta`` to a prefixed line, creating it when it is absent."""
    start, end = span
    index = _prefixed_index(lines, start, end, prefix)
    if index is None:
        position = start + 1
        if insert_before is not None:
            before = _prefixed_index(lines, start, end, insert_before)
            if before is not None:
                position = before
        lines.insert(position, f"{prefix}{delta}")
        return
    current = _read_prefixed_int(lines, start, end, prefix, 0) or 0
    lines[index] = f"{prefix}{current + delta}"


def _parse_expenses(lines: Sequence[str], month: str, year: int) -> list[Entry]:
    date_sections = _date_headings(lines, month)
    expenses: list[Entry] = []
    for position, (start, day) in enumerate(date_sections):
        end = (
            date_sections[position + 1][0]
            if position + 1 < len(date_sections)
            else len(lines)
        )
        for line in lines[start + 1 : end]:
            match = _EXPENSE_RE.fullmatch(line)
            if not match:
                continue
            label, amount_text, description = match.groups()
            kind = "need" if label.startswith("Need") else "want"
            expenses.append(
                Entry(
                    date=date(year, month_number(month), day),
                    amount=int(amount_text.replace(",", "")),
                    description=description,
                    kind=kind,
                )
            )
    return expenses


def _entry_key_from_rendered(
    line: str, entry_date: date
) -> tuple[str, int, str, str] | None:
    match = _EXPENSE_RE.fullmatch(line)
    if not match:
        return None
    label, amount_text, description = match.groups()
    kind = "need" if label.startswith("Need") else "want"
    return (
        entry_date.isoformat(),
        int(amount_text.replace(",", "")),
        kind,
        normalize_description(description),
    )


def _parse_loans(
    lines: Sequence[str],
) -> tuple[list[tuple[int, str, int]], int | None]:
    """Return ([(line_index, name, balance)], total) for the optional Loans block."""
    span = _section_span(lines, LOANS_SECTION)
    if span is None:
        return [], None
    start, end = span
    components: list[tuple[int, str, int]] = []
    for index in range(start + 1, end):
        match = _LOAN_LINE_RE.fullmatch(lines[index])
        if match:
            components.append(
                (index, match.group("name").strip(), int(match.group("balance")))
            )
    return components, _read_prefixed_int(lines, start, end, TOTAL_PREFIX, None)


def collapse_batch_duplicates(entries: Iterable[Entry]) -> tuple[list[Entry], int]:
    """Collapse exact duplicate entries within one source message."""
    unique: list[Entry] = []
    seen: set[tuple[str, int, str, str]] = set()
    collapsed = 0
    for entry in entries:
        if entry.identity_key in seen:
            collapsed += 1
            continue
        seen.add(entry.identity_key)
        unique.append(entry)
    return unique, collapsed


def _resolve_month(
    lines: Sequence[str], explicit: str | None, inferred: set[str]
) -> str:
    """Resolve the sheet month from an explicit flag, the entries, or the sheet."""
    canonical = canonical_month(explicit) if explicit is not None else None
    if inferred:
        if canonical is not None:
            mismatched = [
                value for value in inferred if value.casefold() != canonical.casefold()
            ]
            if mismatched:
                raise ValueError("transaction date does not match the ledger month")
            return canonical
        if len(inferred) != 1:
            raise ValueError("all transactions must belong to one month")
        return canonical_month(next(iter(inferred)))
    if canonical is not None:
        return canonical
    detected = sheet_month(lines)
    if detected is None:
        raise LedgerFormatError("cannot infer the ledger month; pass --month")
    return detected


def _insert_entries(lines: list[str], entries: Sequence[Entry], month: str) -> None:
    grouped: dict[date, list[Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.date, []).append(entry)

    # Process dates in reverse so earlier insertions do not invalidate the
    # bounds of later dates.  Every entry for a date is placed together.
    for entry_date in sorted(grouped, reverse=True):
        start, end = _date_bounds(lines, entry_date, month)
        insert_at = end
        while insert_at > start + 1 and lines[insert_at - 1] == "":
            insert_at -= 1
        lines[insert_at:insert_at] = [entry.rendered for entry in grouped[entry_date]]


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #
def _recalculate_and_replace(lines: list[str], month: str) -> LedgerSummary:
    """Rewrite every derived number in place and return the summary."""
    # Fail closed before touching anything: without the user's own split there is
    # no budget maximum, and inventing one would be somebody else's budget.
    split = require_budget_split(lines)

    in_start = _unique_index(lines, IN_SECTION)
    in_end = _section_end(lines, in_start)
    income_lines = [
        line for line in lines[in_start + 1 : in_end] if _INCOME_RE.fullmatch(line)
    ]
    income_total = sum(
        int(_INCOME_RE.fullmatch(line).group(4).replace(",", ""))
        for line in income_lines
    )
    _set_prefixed(lines, in_start, in_end, TOTAL_PREFIX, income_total)

    year = sheet_year(lines)
    expenses = _parse_expenses(lines, month, year)
    needs_spent = sum(
        entry.amount
        for entry in expenses
        if entry.kind == "need"
        and "(from savings)" not in normalize_description(entry.description)
    )
    wants_spent = sum(
        entry.amount
        for entry in expenses
        if entry.kind == "want"
        and "(from savings)" not in normalize_description(entry.description)
    )

    needs_max, wants_max, savings_max = split.maxima(income_total)

    budget_start = _budget_heading_index(lines)
    budget_end = _section_end(lines, budget_start)
    _set_prefixed(
        lines, budget_start, budget_end, NEEDS_PREFIX, needs_max, keep_max_suffix=True
    )
    _set_prefixed(
        lines, budget_start, budget_end, WANTS_PREFIX, wants_max, keep_max_suffix=True
    )

    savings_start = _unique_index(lines, SAVINGS_SECTION)
    savings_end = _section_end(lines, savings_start)
    savings_total = (
        _read_prefixed_int(lines, savings_start, savings_end, TOTAL_PREFIX, 0) or 0
    )
    borrowed = (
        _read_prefixed_int(lines, savings_start, savings_end, BORROWED_PREFIX, 0) or 0
    )
    used_for_loans = (
        _read_prefixed_int(lines, savings_start, savings_end, USED_FOR_LOANS_PREFIX, 0)
        or 0
    )
    savings_remaining = savings_max - savings_total - borrowed - used_for_loans

    cash_span = _section_span(lines, CASH_SECTION)
    cash_funded_debt = 0
    if cash_span is not None:
        cash_funded_debt = (
            _read_prefixed_int(lines, cash_span[0], cash_span[1], CASH_FUNDED_PREFIX, 0)
            or 0
        )
    posted_expenses = needs_spent + wants_spent
    confirmed_cash = income_total - posted_expenses - cash_funded_debt

    if cash_span is not None:
        cash_start, cash_end = cash_span
        _set_prefixed(lines, cash_start, cash_end, CONFIRMED_INCOME_PREFIX, income_total)
        _set_prefixed(
            lines, cash_start, cash_end, CONFIRMED_EXPENSES_PREFIX, posted_expenses
        )
        _set_prefixed(lines, cash_start, cash_end, CASH_FUNDED_PREFIX, cash_funded_debt)
        _set_prefixed(lines, cash_start, cash_end, CONFIRMED_CASH_PREFIX, confirmed_cash)

    remaining_start = _unique_index(lines, REMAINING_SECTION)
    remaining_end = _section_end(lines, remaining_start)
    needs_remaining = needs_max - needs_spent
    wants_remaining = wants_max - wants_spent
    remaining_total = needs_remaining + wants_remaining + savings_remaining
    _set_prefixed(lines, remaining_start, remaining_end, NEEDS_PREFIX, needs_remaining)
    _set_prefixed(lines, remaining_start, remaining_end, WANTS_PREFIX, wants_remaining)
    _set_prefixed(
        lines, remaining_start, remaining_end, SAVINGS_LINE_PREFIX, savings_remaining
    )
    _set_prefixed(lines, remaining_start, remaining_end, TOTAL_PREFIX, remaining_total)

    return LedgerSummary(
        income_total=income_total,
        needs_max=needs_max,
        wants_max=wants_max,
        savings_max=savings_max,
        needs_spent=needs_spent,
        wants_spent=wants_spent,
        needs_remaining=needs_remaining,
        wants_remaining=wants_remaining,
        savings_remaining=savings_remaining,
        remaining_total=remaining_total,
        posted_expenses=posted_expenses,
        cash_funded_debt=cash_funded_debt,
        confirmed_cash=confirmed_cash,
    )


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def plan_update(
    text: str,
    entries: Sequence[Entry],
    *,
    month: str | None = None,
    incomes: Sequence[IncomeEntry] = (),
) -> LedgerPlan:
    """Return a complete projected sheet without performing any external write."""
    if not entries and not incomes:
        raise ValueError("at least one entry or income is required")

    lines, newline = _split_text(text)
    trailing_newline = text.endswith(("\n", "\r"))
    inferred = {entry.date.strftime("%B") for entry in entries}
    inferred.update(income.date.strftime("%B") for income in incomes)
    month = _resolve_month(lines, month, inferred)

    unique_entries, collapsed = collapse_batch_duplicates(entries)
    if collapsed:
        # A repeated line inside one source event is a transport/message
        # duplicate, so it collapses to a single logical entry.
        entries = unique_entries

    # Reject an operation that is already on the sheet.  The same amount and
    # description on a different date is a legitimate separate purchase.
    year = sheet_year(lines)
    existing_keys: set[tuple[str, int, str, str]] = set()
    for index, day in _date_headings(lines, month):
        end = target_date_end(lines, month, index)
        section_date = date(year, month_number(month), day)
        for line in lines[index + 1 : end]:
            key = _entry_key_from_rendered(line, section_date)
            if key is not None:
                existing_keys.add(key)
    duplicate_keys = [
        entry.identity_key for entry in entries if entry.identity_key in existing_keys
    ]
    if duplicate_keys:
        raise DuplicateEntryError(
            "exact same-date entry already exists: "
            + ", ".join(str(key) for key in duplicate_keys)
        )

    batch_keys = [entry.identity_key for entry in entries]
    if len(batch_keys) != len(set(batch_keys)):
        raise DuplicateEntryError("duplicate entry repeated in one batch")

    in_start = _unique_index(lines, IN_SECTION)
    in_end = _section_end(lines, in_start)
    existing_income = {
        line for line in lines[in_start + 1 : in_end] if _INCOME_RE.fullmatch(line)
    }
    income_keys: set[tuple[str, int]] = set()
    for income in incomes:
        if income.identity_key in income_keys:
            raise DuplicateEntryError(f"income repeated in one batch: {income.rendered}")
        income_keys.add(income.identity_key)
        if income.rendered in existing_income:
            raise DuplicateEntryError(f"income already exists: {income.rendered}")

    # Income is inserted inside the In: block, immediately above its total.
    if incomes:
        total_lines = [
            index
            for index in range(in_start + 1, in_end)
            if _TOTAL_LINE_RE.fullmatch(lines[index])
        ]
        if len(total_lines) != 1:
            raise LedgerFormatError("expected one formatted In: total line")
        lines[total_lines[0] : total_lines[0]] = [
            income.rendered for income in incomes
        ]

    if entries:
        _insert_entries(lines, entries, month)
    summary = _recalculate_and_replace(lines, month)
    projected = _join_lines(lines, newline, trailing_newline)
    return LedgerPlan(projected, summary, tuple(entries), tuple(incomes))


def target_date_end(lines: Sequence[str], month: str, start: int) -> int:
    following = [index for index, _ in _date_headings(lines, month) if index > start]
    return min(following) if following else len(lines)


def move_entries(
    text: str,
    entries: Sequence[Entry],
    *,
    to_date: date,
    month: str | None = None,
) -> LedgerPlan:
    """Move rendered entries between date sections without duplicating spend."""
    if not entries:
        raise ValueError("at least one entry is required")
    source_dates = {entry.date for entry in entries}
    if len(source_dates) != 1:
        raise ValueError("all moved entries must share one source date")
    source_date = next(iter(source_dates))
    if source_date == to_date:
        raise ValueError("source and target dates must differ")

    months = {source_date.strftime("%B"), to_date.strftime("%B")}
    if month is None:
        if len(months) != 1:
            raise ValueError("source and target dates must belong to one month")
        month = source_date.strftime("%B")
    elif any(value.casefold() != month.casefold() for value in months):
        raise ValueError("move dates do not match the ledger month")
    month = canonical_month(month)

    lines, newline = _split_text(text)
    trailing_newline = text.endswith(("\n", "\r"))
    source_start, source_end = _date_bounds(lines, source_date, month)
    target_heading = f"{month} {to_date.day}"
    target_matches = [
        index for index, line in enumerate(lines) if line == target_heading
    ]
    if len(target_matches) > 1:
        raise LedgerFormatError(f"duplicate date heading: {target_heading}")
    target_start = target_matches[0] if target_matches else None
    target_end = (
        target_date_end(lines, month, target_start)
        if target_start is not None
        else None
    )

    source_indexes: list[int] = []
    for entry in entries:
        matches = [
            index
            for index in range(source_start + 1, source_end)
            if lines[index] == entry.rendered
        ]
        if len(matches) != 1:
            raise LedgerFormatError(
                f"expected one source line {entry.rendered!r}, found {len(matches)}"
            )
        source_indexes.append(matches[0])

        if target_start is not None:
            already_there = [
                index
                for index in range(target_start + 1, target_end)
                if lines[index] == entry.rendered
            ]
            if already_there:
                raise DuplicateEntryError(
                    f"target already contains entry: {entry.rendered}"
                )

    for index in sorted(source_indexes, reverse=True):
        del lines[index]

    moved = [
        Entry(to_date, entry.amount, entry.description, entry.kind) for entry in entries
    ]
    _insert_entries(lines, moved, month)
    summary = _recalculate_and_replace(lines, month)
    projected = _join_lines(lines, newline, trailing_newline)
    return LedgerPlan(projected, summary, tuple(moved), ())


def _edit_loans(
    text: str,
    name: str,
    balance_fn: Callable[[int], int],
    *,
    funded_by: str | None = None,
    delta: int = 0,
    month: str | None = None,
) -> LedgerPlan:
    """Rewrite one loan balance, optionally recording where the money came from."""
    lines, newline = _split_text(text)
    trailing_newline = text.endswith(("\n", "\r"))
    components, _total = _parse_loans(lines)
    if not components:
        raise LedgerFormatError("the ledger has no 'Loans:' entries to edit")
    matches = [item for item in components if item[1].casefold() == name.casefold()]
    if len(matches) != 1:
        raise LedgerError(
            f"expected exactly one loan named {name!r}, found {len(matches)}"
        )
    index, entry_name, current = matches[0]
    new_balance = int(balance_fn(current))
    if new_balance < 0:
        raise ValueError("a loan balance cannot be negative")
    lines[index] = f"{entry_name} - {new_balance}"

    span = _section_span(lines, LOANS_SECTION)
    assert span is not None  # _parse_loans found the section
    updated = [new_balance if item[0] == index else item[2] for item in components]
    _set_prefixed(lines, span[0], span[1], TOTAL_PREFIX, sum(updated))

    note = ""
    if funded_by == "savings":
        savings = _section_span(lines, SAVINGS_SECTION)
        if savings is None:
            raise LedgerFormatError(
                "the ledger has no 'Savings:' section to record a savings-funded payment"
            )
        _bump_prefixed(
            lines,
            savings,
            USED_FOR_LOANS_PREFIX,
            delta,
            insert_before=TOTAL_PREFIX,
        )
    elif funded_by == "cash":
        cash = _section_span(lines, CASH_SECTION)
        if cash is None:
            raise LedgerFormatError(
                "the ledger has no 'Cash Reconciliation:' section; add a "
                f"'{CASH_FUNDED_PREFIX}0' line to record a cash-funded payment"
            )
        _bump_prefixed(lines, cash, CASH_FUNDED_PREFIX, delta)
    else:
        note = (
            "no funding source recorded: the loan balance dropped, while savings "
            "and confirmed cash were left untouched"
        )

    resolved = _resolve_month(lines, month, set())
    summary = _recalculate_and_replace(lines, resolved)
    projected = _join_lines(lines, newline, trailing_newline)
    return LedgerPlan(projected, summary, (), (), note)


def set_loan_balance(
    text: str, name: str, balance: int, *, month: str | None = None
) -> LedgerPlan:
    """Replace one loan balance.

    A current balance is account state, not a transaction: it belongs in the
    Loans block and never becomes an expense line.
    """
    if balance < 0:
        raise ValueError("a loan balance cannot be negative")
    return _edit_loans(text, name, lambda _current: balance, month=month)


def pay_loan(
    text: str,
    name: str,
    amount: int,
    *,
    funded_by: str | None = None,
    month: str | None = None,
) -> LedgerPlan:
    """Reduce a loan balance, optionally recording where the money came from.

    ``funded_by="savings"`` accumulates a ``Used for loans:`` line under Savings,
    which lowers savings remaining.  ``funded_by="cash"`` accumulates a
    ``Cash-funded debt payments:`` line in cash reconciliation, which lowers
    confirmed cash.  Either way the payment never becomes a Need/Want expense.
    """
    if amount <= 0:
        raise ValueError("payment amount must be positive")
    if funded_by not in {None, "savings", "cash"}:
        raise ValueError("funded_by must be 'savings', 'cash' or None")
    return _edit_loans(
        text,
        name,
        lambda current: max(0, current - amount),
        funded_by=funded_by,
        delta=amount,
        month=month,
    )


def build_blank_sheet(month: str, year: int, split: BudgetSplit | None = None) -> str:
    """Return a complete, all-zero sheet for a month, ready for its first income.

    Without a ``split`` the budget heading is the unconfigured placeholder, so a
    new sheet never silently inherits somebody else's percentages.
    """
    number = month_number(month)
    canonical = month_name(number)
    days = calendar.monthrange(year, number)[1]
    lines = [
        f"{canonical} {TITLE_SUFFIX}",
        "",
        LOANS_SECTION,
        f"{TOTAL_PREFIX}0",
        "",
        IN_SECTION,
        f"{TOTAL_PREFIX}0",
        "",
        split.label if split is not None else BUDGET_PLACEHOLDER,
        f"{NEEDS_PREFIX}0 Max",
        f"{WANTS_PREFIX}0 Max",
        "",
        SAVINGS_SECTION,
        f"{TOTAL_PREFIX}0",
        "",
        CASH_SECTION,
        f"{CONFIRMED_INCOME_PREFIX}0",
        f"{CONFIRMED_EXPENSES_PREFIX}0",
        f"{CASH_FUNDED_PREFIX}0",
        f"{CONFIRMED_CASH_PREFIX}0",
        "",
        PENDING_SECTION,
        "",
        REMAINING_SECTION,
        f"{NEEDS_PREFIX}0",
        f"{WANTS_PREFIX}0",
        f"{SAVINGS_LINE_PREFIX}0",
        f"{TOTAL_PREFIX}0",
    ]
    for day in range(1, days + 1):
        lines.append("")
        lines.append(f"{canonical} {day}")
    lines.append("")
    return "\n".join(lines)


def plan_rollover(
    text: str, to_month: str, opening_income: int, *, year: int | None = None
) -> LedgerPlan:
    """Open a fresh month, carrying ongoing loan balances forward.

    Current-month expenses, savings deposits, borrowed amounts and budget
    deductions are deliberately reset: a rollover opens a new month rather than
    resetting the old one.  The user's own split is the exception: it carries
    forward, because it is a preference rather than a monthly figure.  ``year``
    defaults to the source sheet's year, plus one when the target month comes
    earlier in the calendar than the source month.
    """
    if opening_income <= 0:
        raise ValueError("opening income must be positive")
    source_lines, _newline = _split_text(text)
    source_month = sheet_month(source_lines)
    if source_month is None:
        raise LedgerFormatError(
            "the source does not look like a ledger sheet (no '<Month> ...' title "
            "or date heading was found)"
        )
    split = require_budget_split(source_lines, where="the source ledger")
    if year is None:
        source_year = sheet_year(source_lines)
        target_number = month_number(to_month)
        if target_number < month_number(source_month):
            year = source_year + 1
        else:
            year = source_year

    target_month = canonical_month(to_month)
    lines, newline = _split_text(build_blank_sheet(target_month, year, split))

    carried, _total = _parse_loans(source_lines)
    if carried:
        loans = _section_span(lines, LOANS_SECTION)
        assert loans is not None
        block = [LOANS_SECTION]
        block.extend(f"{name} - {balance}" for _index, name, balance in carried)
        block.append(f"{TOTAL_PREFIX}{sum(balance for _, _, balance in carried)}")
        lines[loans[0] : loans[1]] = block + [""]

    income = IncomeEntry(date(year, month_number(target_month), 1), opening_income)
    in_span = _section_span(lines, IN_SECTION)
    assert in_span is not None
    lines.insert(in_span[0] + 1, income.rendered)

    summary = _recalculate_and_replace(lines, target_month)
    projected = _join_lines(lines, newline, True)
    return LedgerPlan(projected, summary, (), (income,))


def recompute(text: str, *, month: str | None = None) -> str:
    """Return the sheet with every derived number rewritten, changing nothing else.

    Useful as a self-check: on a consistent sheet the result is byte-identical to
    the input, which is how the example ledgers in this repo are validated.  The
    sheet must carry a configured split, because every maximum depends on it.
    """
    lines, newline = _split_text(text)
    trailing_newline = text.endswith(("\n", "\r"))
    resolved = _resolve_month(lines, month, set())
    _recalculate_and_replace(lines, resolved)
    return _join_lines(lines, newline, trailing_newline)


def configure_split(
    text: str, split: BudgetSplit, *, month: str | None = None
) -> LedgerPlan:
    """Write the user's own split onto the sheet, then recompute from it.

    The budget heading *is* the split, so the sheet stays self-contained and no
    state can drift away from what the file displays.  Entries already on the
    sheet keep the Need/Want label they were logged with; only the budget
    maximums and the remaining figures move.
    """
    lines, newline = _split_text(text)
    trailing_newline = text.endswith(("\n", "\r"))
    heading = _budget_heading_index(lines)
    note = ""
    if lines[heading] == split.label:
        note = (
            f"the budget split was already {split.label}; "
            "the maximums were recomputed"
        )
    lines[heading] = split.label
    resolved = _resolve_month(lines, month, set())
    summary = _recalculate_and_replace(lines, resolved)
    projected = _join_lines(lines, newline, trailing_newline)
    return LedgerPlan(projected, summary, (), (), note)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def format_summary(summary: LedgerSummary) -> str:
    return (
        f"Needs {summary.needs_remaining} left; "
        f"Wants {summary.wants_remaining} left; "
        f"Savings {summary.savings_remaining} left; "
        f"Budget total {summary.remaining_total}; "
        f"Confirmed cash {summary.confirmed_cash}"
    )


def format_plan(
    plan: LedgerPlan,
    *,
    dry_run: bool = True,
    collapsed: int = 0,
    target: str | None = None,
) -> str:
    """Render the receipt: status line, summary line, then the COMPLETE sheet."""
    chunks: list[str] = []
    mode = "DRY RUN (nothing written)" if dry_run else "APPLIED"
    if target:
        mode = f"{mode} -> {target}"
    chunks.append(mode)
    if collapsed:
        chunks.append(f"Collapsed duplicate input lines: {collapsed}")
    chunks.append(f"Summary: {format_summary(plan.summary)}")
    if plan.note:
        chunks.append(f"Note: {plan.note}")
    chunks.append("--- FULL SHEET ---")
    body = plan.text if plan.text.endswith("\n") else plan.text + "\n"
    chunks.append(body)
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Local-file storage
# --------------------------------------------------------------------------- #
def local_timezone():
    """The timezone used to date undated entries: this machine's local zone."""
    return datetime.now().astimezone().tzinfo


def local_today(timezone_name: str | None = None) -> date:
    if timezone_name:
        return datetime.now(ZoneInfo(timezone_name)).date()
    return datetime.now(local_timezone()).date()


def default_ledger_path() -> Path:
    """Where the ledger lives unless --ledger or $FINANCE_LEDGER_PATH says otherwise."""
    configured = os.environ.get("FINANCE_LEDGER_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path("finance-ledger.md")


def lock_path_for(ledger_path: Path) -> Path:
    return ledger_path.with_name(ledger_path.name + ".lock")


class LedgerLock:
    """A small cross-platform advisory lock for one ledger file.

    The lock is an exclusively-created sibling file.  It is removed on exit, and
    a lock older than ``stale_after`` seconds is treated as abandoned by a dead
    process and reclaimed.
    """

    def __init__(self, path: Path, timeout: float = 30.0, stale_after: float = 300.0):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after

    def __enter__(self) -> "LedgerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if age > self.stale_after:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LedgerError(
                        f"could not acquire ledger lock {self.path} "
                        "(another update may still be running)"
                    )
                time.sleep(0.05)
            else:
                os.write(handle, f"{os.getpid()}\n".encode())
                os.close(handle)
                return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class LocalFileLedger:
    """Local-file storage: read, then write back with concurrency and readback."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def read_bytes(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError as exc:
            raise LedgerError(f"ledger file not found: {self.path}") from exc

    def read_text(self) -> str:
        try:
            return self.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError(f"ledger file is not UTF-8 Markdown: {self.path}") from exc

    def apply(self, baseline: bytes, staged: bytes) -> Path:
        """Re-read, require the baseline to be unchanged, then write atomically.

        The immediate re-read is the optimistic-concurrency guard; the readback
        after the write proves that what is on disk is exactly what was staged.
        """
        live = self.read_bytes()
        if live != baseline:
            raise ConcurrentModificationError(
                f"ledger changed on disk while the update was staged: {self.path}"
            )
        self._atomic_write(staged)
        if self.read_bytes() != staged:
            raise LedgerError(
                f"post-write readback differs from staged content: {self.path}"
            )
        return self.path

    def create(self, staged: bytes, *, force: bool = False) -> Path:
        """Create a new ledger file; refuse to clobber an existing one."""
        if self.path.exists() and not force:
            raise LedgerError(f"refusing to overwrite existing file: {self.path}")
        self._atomic_write(staged)
        if self.read_bytes() != staged:
            raise LedgerError(
                f"post-write readback differs from staged content: {self.path}"
            )
        return self.path

    def _atomic_write(self, content: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_STATE_SUFFIX = ".events.json"


def _load_event_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read event state: {path}") from exc
    if not isinstance(data, dict):
        raise LedgerError("event state must be a JSON object")
    return data


def _save_event_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="events-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_cli_date(value: str, timezone_name: str | None) -> date:
    if value == "today":
        return local_today(timezone_name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--date must be 'today' or YYYY-MM-DD") from exc


def _whole_percent(flag: str) -> Callable[[str], int]:
    """Build an argparse type for one split percentage.

    A non-integer must fail with a message that says what to fix, rather than
    argparse's bare "invalid int value".
    """

    def parse(value: str) -> int:
        try:
            return int(value.strip())
        except (AttributeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"{flag} must be a whole number between 0 and 100 (got {value!r})"
            ) from None

    return parse


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ledger",
        default=None,
        help="ledger Markdown file (default: $FINANCE_LEDGER_PATH or ./finance-ledger.md)",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone for undated entries (default: this machine's local zone)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the change; without it the command only prints the projection",
    )


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance_logger",
        description=(
            "Deterministic local finance-ledger updater.  Every command prints the "
            "COMPLETE resulting sheet; --apply is the only switch that writes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser(
        "config",
        help="set the Needs/Wants/Savings split this ledger uses",
        description=(
            "Set the ledger's own Needs/Wants/Savings split, in whole percentages "
            "that add up to exactly 100.  Needs are the essentials the month "
            "cannot avoid (rent, food, transport, bills, medicine); Wants are "
            "discretionary (takeout, subscriptions, treats, games); Savings is "
            "what is set aside.  The split is stored in the sheet's budget heading "
            "(for example '60/25/15'), and every budget maximum and remaining "
            "figure is recomputed from it.  Entries already on the sheet keep the "
            "Need/Want label they were logged with.  Without --apply nothing is "
            "written."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    config.add_argument(
        "--ledger",
        default=None,
        help="ledger Markdown file (default: $FINANCE_LEDGER_PATH or ./finance-ledger.md)",
    )
    config.add_argument(
        "--needs",
        required=True,
        type=_whole_percent("--needs"),
        help="percentage of income for Needs: rent, food, transport, bills, medicine",
    )
    config.add_argument(
        "--wants",
        required=True,
        type=_whole_percent("--wants"),
        help="percentage of income for Wants: takeout, subscriptions, treats, games",
    )
    config.add_argument(
        "--savings",
        required=True,
        type=_whole_percent("--savings"),
        help="percentage of income set aside as Savings",
    )
    config.add_argument(
        "--apply",
        action="store_true",
        help="write the change; without it the command only prints the projection",
    )

    add = subparsers.add_parser("add", help="log expenses and/or income")
    _common_arguments(add)
    add.add_argument(
        "--entry", action="append", default=[], metavar="'AMOUNT DESCRIPTION'"
    )
    add.add_argument("--income", action="append", default=[], metavar="AMOUNT")
    add.add_argument("--kind", choices=["need", "want"], default=None)
    add.add_argument("--date", default="today", help="YYYY-MM-DD or 'today' (default)")
    add.add_argument("--month", default=None)
    add.add_argument(
        "--event-id", default=None, help="idempotency key for the source event"
    )
    add.add_argument("--state-path", default=None)

    move = subparsers.add_parser("move", help="relocate existing entries between dates")
    _common_arguments(move)
    move.add_argument(
        "--entry", action="append", default=[], metavar="'AMOUNT DESCRIPTION'"
    )
    move.add_argument("--kind", choices=["need", "want"], default=None)
    move.add_argument("--from-date", required=True)
    move.add_argument("--to-date", required=True)
    move.add_argument("--month", default=None)

    pay = subparsers.add_parser("payloan", help="reduce a loan balance")
    _common_arguments(pay)
    pay.add_argument("--name", required=True, help="the loan's name in the Loans block")
    pay.add_argument("--amount", required=True, help="whole-unit payment")
    pay.add_argument("--funded-by", choices=["savings", "cash"], default=None)
    pay.add_argument("--month", default=None)

    setloan = subparsers.add_parser(
        "setloan", help="set a loan balance to current account state"
    )
    _common_arguments(setloan)
    setloan.add_argument("--name", required=True)
    setloan.add_argument("--balance", required=True)
    setloan.add_argument("--month", default=None)

    rollover = subparsers.add_parser(
        "rollover", help="open the next month, carrying loan balances forward"
    )
    _common_arguments(rollover)
    rollover.add_argument(
        "--to-month", required=True, help="full month name, for example October"
    )
    rollover.add_argument("--year", type=int, default=None)
    rollover.add_argument("--opening-income", required=True, type=int)
    rollover.add_argument("--out", default=None, help="target file for the new month")
    rollover.add_argument(
        "--force", action="store_true", help="overwrite an existing target file"
    )
    return parser


def _resolve_ledger_argument(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_ledger_path()


def _default_rollover_target(ledger_path: Path, to_month: str) -> Path:
    slug = canonical_month(to_month).casefold()
    return ledger_path.with_name(f"{slug}-expenses-log.md")


def _print(text: str) -> None:
    print(text, end="" if text.endswith("\n") else "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)

    # Validate a requested split before touching any file: a bad split is a usage
    # error, not a ledger problem.
    split = None
    if args.command == "config":
        split = BudgetSplit(needs=args.needs, wants=args.wants, savings=args.savings)

    if args.command == "rollover":
        ledger_path = _resolve_ledger_argument(args.ledger)
        target = (
            Path(args.out).expanduser()
            if args.out
            else _default_rollover_target(ledger_path, args.to_month)
        )
        with LedgerLock(lock_path_for(target)):
            source = LocalFileLedger(ledger_path).read_text()
            plan = plan_rollover(
                source, args.to_month, args.opening_income, year=args.year
            )
            if not args.apply:
                _print(format_plan(plan, dry_run=True, target=str(target)))
                return 0
            written = LocalFileLedger(target).create(
                plan.text.encode("utf-8"), force=args.force
            )
        _print(format_plan(plan, dry_run=False, target=str(written)))
        return 0

    ledger_path = _resolve_ledger_argument(args.ledger)
    ledger = LocalFileLedger(ledger_path)
    state_path = (
        Path(args.state_path).expanduser()
        if getattr(args, "state_path", None)
        else ledger_path.with_name(ledger_path.name + _STATE_SUFFIX)
    )
    state = _load_event_state(state_path) if args.command == "add" else {}

    if args.command == "add" and args.event_id and args.event_id in state:
        # A replayed source event: show the current sheet and write nothing.
        current = ledger.read_text()
        _print("REPLAYED EVENT (nothing written)")
        _print("--- FULL SHEET ---")
        _print(current)
        return 0

    with LedgerLock(lock_path_for(ledger_path)):
        baseline = ledger.read_bytes()
        try:
            baseline_text = baseline.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError(
                f"ledger file is not UTF-8 Markdown: {ledger_path}"
            ) from exc

        if args.command == "add":
            if not args.entry and not args.income:
                parser.error("at least one --entry or --income is required")
            entry_date = _parse_cli_date(args.date, args.timezone)
            entries = [parse_entry(raw, entry_date, args.kind) for raw in args.entry]
            incomes = [parse_income(raw, entry_date) for raw in args.income]
            entries, collapsed = collapse_batch_duplicates(entries)
            plan = plan_update(
                baseline_text, entries, month=args.month, incomes=incomes
            )
        elif args.command == "move":
            if not args.entry:
                parser.error("at least one --entry is required")
            source_date = _parse_cli_date(args.from_date, args.timezone)
            target_date = _parse_cli_date(args.to_date, args.timezone)
            entries = [parse_entry(raw, source_date, args.kind) for raw in args.entry]
            collapsed = 0
            plan = move_entries(
                baseline_text, entries, to_date=target_date, month=args.month
            )
        elif args.command == "payloan":
            collapsed = 0
            plan = pay_loan(
                baseline_text,
                args.name,
                int(args.amount),
                funded_by=args.funded_by,
                month=args.month,
            )
        elif args.command == "setloan":
            collapsed = 0
            plan = set_loan_balance(
                baseline_text, args.name, int(args.balance), month=args.month
            )
        elif args.command == "config":
            collapsed = 0
            assert split is not None  # built above, before the ledger was read
            plan = configure_split(baseline_text, split)
        else:  # pragma: no cover - argparse rejects unknown commands
            parser.error("unknown command")

        if not args.apply:
            _print(format_plan(plan, dry_run=True, collapsed=collapsed))
            return 0

        written = ledger.apply(baseline, plan.text.encode("utf-8"))
        if args.command == "add" and args.event_id:
            state[args.event_id] = {
                "applied_at": datetime.now(local_timezone()).isoformat(),
                "ledger": str(written),
            }
            _save_event_state(state_path, state)
        _print(
            format_plan(plan, dry_run=False, collapsed=collapsed, target=str(written))
        )
    return 0


__all__ = [
    "BUDGET_PLACEHOLDER",
    "BudgetNotConfiguredError",
    "BudgetSplit",
    "ConcurrentModificationError",
    "DuplicateEntryError",
    "Entry",
    "IncomeEntry",
    "LedgerError",
    "LedgerFormatError",
    "LedgerLock",
    "LedgerPlan",
    "LedgerSummary",
    "LocalFileLedger",
    "build_blank_sheet",
    "canonical_month",
    "classify_description",
    "collapse_batch_duplicates",
    "configure_split",
    "default_ledger_path",
    "format_plan",
    "format_summary",
    "lock_path_for",
    "local_today",
    "local_timezone",
    "main",
    "month_name",
    "month_number",
    "move_entries",
    "normalize_description",
    "parse_entry",
    "parse_income",
    "parse_split_label",
    "pay_loan",
    "plan_rollover",
    "plan_update",
    "read_budget_split",
    "recompute",
    "require_budget_split",
    "set_loan_balance",
    "sheet_month",
    "sheet_year",
]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LedgerError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
