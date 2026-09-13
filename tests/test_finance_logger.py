"""Standard-library test suite for the finance ledger.

Everything here runs against fictional data in a temporary directory: no
credentials, no network, no third-party packages.
"""

import ast
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import finance_logger  # noqa: E402
from finance_logger import (  # noqa: E402
    BUDGET_PLACEHOLDER,
    BudgetNotConfiguredError,
    BudgetSplit,
    ConcurrentModificationError,
    DuplicateEntryError,
    Entry,
    LedgerError,
    LedgerFormatError,
    LedgerLock,
    LocalFileLedger,
    build_blank_sheet,
    classify_description,
    collapse_batch_duplicates,
    configure_split,
    format_plan,
    lock_path_for,
    move_entries,
    parse_entry,
    parse_income,
    pay_loan,
    plan_rollover,
    plan_update,
    read_budget_split,
    recompute,
    set_loan_balance,
    sheet_month,
)

BASELINE = """September Expenses Log

Loans:
Loan A - 1200
Loan B - 400
Total: 1600

In:
09/01/26 - 10000
Total: 10000

50/30/20
Needs - 5000 Max
Wants - 3000 Max

Savings:
Total: 0

Cash Reconciliation:
Confirmed income: 10000
Confirmed posted expenses: 571
Cash-funded debt payments: 0
Confirmed cash remaining: 9429

Pending / Not Posted:
Sample Store: 750 (excluded from confirmed cash)

Remaining:
Needs - 4528
Wants - 2901
Savings - 2000
Total: 9429

September 1
[Need] 80 fare
[Need] 42 groceries

September 2
[Need] 350 rent
[Wants] 99 mobile data
"""


class ClassificationTests(unittest.TestCase):
    def test_ordinary_food_and_transport_are_needs(self):
        for description in (
            "198 food",
            "80 fare",
            "42 groceries",
            "350 rent",
            "60 medicine",
            "120 water",
        ):
            with self.subTest(description=description):
                self.assertEqual(classify_description(description), "need")

    def test_discretionary_items_are_wants(self):
        for description in (
            "99 mobile data",
            "85 streaming subscription",
            "75 snacks",
            "50 coffee",
            "120 gym",
            "30 beer",
            "200 clothing",
        ):
            with self.subTest(description=description):
                self.assertEqual(classify_description(description), "want")

    def test_want_keywords_beat_need_keywords_in_the_same_description(self):
        # "food" alone is a Need, but a delivery order is discretionary.
        self.assertEqual(classify_description("400 food"), "need")
        self.assertEqual(classify_description("400 food delivery"), "want")

    def test_explicit_wording_inside_the_description_wins(self):
        self.assertEqual(classify_description("200 food wants"), "want")
        self.assertEqual(classify_description("50 beer needs"), "need")

    def test_explicit_kind_argument_wins_over_the_keyword_policy(self):
        self.assertEqual(classify_description("99 mobile data", "need"), "need")
        self.assertEqual(classify_description("198 food", "want"), "want")

    def test_unknown_description_defaults_to_need(self):
        self.assertEqual(classify_description("500 thingamajig"), "need")

    def test_invalid_explicit_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            classify_description("198 food", "essential")


class ParsingTests(unittest.TestCase):
    def test_parse_plain_entry(self):
        self.assertEqual(
            parse_entry("198 food", date(2026, 9, 6)),
            Entry(date=date(2026, 9, 6), amount=198, description="food", kind="need"),
        )

    def test_parse_entry_tolerates_currency_symbols_and_commas(self):
        entry = parse_entry("\u20b11,200 rent", date(2026, 9, 6))

        self.assertEqual(entry.amount, 1200)
        self.assertEqual(entry.rendered, "[Need] 1200 rent")

    def test_parse_entry_rejects_fractional_amounts(self):
        with self.assertRaises(ValueError):
            parse_entry("12.5 food", date(2026, 9, 6))

    def test_parse_entry_requires_an_amount_and_a_description(self):
        for bad in ("food", "198", "0 food"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_entry(bad, date(2026, 9, 6))

    def test_income_must_be_a_positive_whole_amount(self):
        self.assertEqual(parse_income("\u20b11,000", date(2026, 9, 6)).amount, 1000)
        for bad in ("0", "-5", "10.5", "abc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_income(bad, date(2026, 9, 6))


class LedgerPlanningTests(unittest.TestCase):
    def test_adds_entry_to_the_requested_date_and_recomputes_every_total(self):
        plan = plan_update(BASELINE, [parse_entry("33 bread", date(2026, 9, 2))])

        self.assertIn(
            "September 2\n[Need] 350 rent\n[Wants] 99 mobile data\n[Need] 33 bread",
            plan.text,
        )
        self.assertEqual(plan.summary.needs_spent, 505)
        self.assertEqual(plan.summary.needs_remaining, 4495)
        self.assertEqual(plan.summary.wants_remaining, 2901)
        self.assertEqual(plan.summary.savings_remaining, 2000)
        self.assertEqual(plan.summary.remaining_total, 9396)
        self.assertEqual(plan.summary.posted_expenses, 604)
        self.assertEqual(plan.summary.confirmed_cash, 9396)

    def test_pending_charges_stay_out_of_every_confirmed_total(self):
        plan = plan_update(BASELINE, [parse_entry("10 bread", date(2026, 9, 2))])

        self.assertIn("Sample Store: 750 (excluded from confirmed cash)", plan.text)
        self.assertEqual(plan.summary.posted_expenses, 581)
        self.assertEqual(plan.summary.confirmed_cash, 9419)

    def test_appends_a_new_date_section_when_the_day_is_absent(self):
        plan = plan_update(BASELINE, [parse_entry("12 snacks", date(2026, 9, 9))])

        self.assertIn("September 9\n[Wants] 12 snacks", plan.text)
        self.assertNotIn("September 10", plan.text)

    def test_rejects_an_exact_same_date_duplicate(self):
        with self.assertRaises(DuplicateEntryError):
            plan_update(BASELINE, [parse_entry("99 mobile data", date(2026, 9, 2))])

    def test_allows_the_same_purchase_on_a_different_date(self):
        plan = plan_update(BASELINE, [parse_entry("99 mobile data", date(2026, 9, 3))])

        self.assertIn("September 3\n[Wants] 99 mobile data", plan.text)
        self.assertEqual(plan.summary.wants_spent, 198)

    def test_collapses_exact_duplicate_inputs_from_one_message(self):
        entry = parse_entry("33 bread", date(2026, 9, 7))

        unique, collapsed = collapse_batch_duplicates([entry, entry])

        self.assertEqual(unique, [entry])
        self.assertEqual(collapsed, 1)

    def test_income_is_inserted_inside_the_income_block_and_grows_the_maxes(self):
        plan = plan_update(BASELINE, [], incomes=[parse_income("2000", date(2026, 9, 7))])

        self.assertIn("In:\n09/01/26 - 10000\n09/07/26 - 2000\nTotal: 12000", plan.text)
        self.assertEqual(plan.summary.income_total, 12000)
        self.assertEqual(plan.summary.needs_max, 6000)
        self.assertEqual(plan.summary.wants_max, 3600)
        self.assertEqual(plan.summary.savings_max, 2400)

    def test_rejects_duplicate_income_in_one_batch(self):
        income = parse_income("2000", date(2026, 9, 7))

        with self.assertRaises(DuplicateEntryError):
            plan_update(BASELINE, [], incomes=[income, income])

    def test_rejects_income_that_already_exists_on_the_sheet(self):
        with self.assertRaises(DuplicateEntryError):
            plan_update(BASELINE, [], incomes=[parse_income("10000", date(2026, 9, 1))])

    def test_a_sheet_with_no_income_yet_still_accepts_entries(self):
        empty = configure_split(
            build_blank_sheet("September", 2026), BudgetSplit(60, 25, 15)
        ).text

        plan = plan_update(empty, [parse_entry("40 fare", date(2026, 9, 4))])

        self.assertEqual(plan.summary.income_total, 0)
        self.assertEqual(plan.summary.needs_spent, 40)
        self.assertEqual(plan.summary.needs_remaining, -40)
        self.assertIn("September 4\n[Need] 40 fare", plan.text)

    def test_rejects_a_transaction_outside_the_declared_month(self):
        with self.assertRaises(ValueError):
            plan_update(
                BASELINE,
                [parse_entry("40 fare", date(2026, 10, 4))],
                month="September",
            )

    def test_rejects_a_sheet_without_the_required_anchors(self):
        with self.assertRaises(LedgerFormatError):
            plan_update("not a ledger", [parse_entry("40 fare", date(2026, 9, 4))])

    def test_plan_requires_at_least_one_change(self):
        with self.assertRaises(ValueError):
            plan_update(BASELINE, [])

    def test_recompute_is_a_no_op_on_a_consistent_sheet(self):
        self.assertEqual(recompute(BASELINE), BASELINE)

    def test_rendered_plan_always_contains_the_whole_sheet(self):
        plan = plan_update(BASELINE, [parse_entry("33 bread", date(2026, 9, 2))])
        receipt = format_plan(plan)

        self.assertIn("--- FULL SHEET ---", receipt)
        self.assertIn("September Expenses Log", receipt)
        self.assertIn("Remaining:", receipt)
        for line in plan.text.splitlines():
            if line.startswith(("[", "Total:", "Needs - ", "Wants - ", "Savings - ")):
                self.assertIn(line, receipt)


class MoveTests(unittest.TestCase):
    def test_relocates_entries_without_changing_spend(self):
        entries = [
            parse_entry("350 rent", date(2026, 9, 2)),
            parse_entry("99 mobile data", date(2026, 9, 2)),
        ]

        plan = move_entries(BASELINE, entries, to_date=date(2026, 9, 8))

        self.assertNotIn("September 2\n[Need] 350 rent", plan.text)
        self.assertIn("September 8\n[Need] 350 rent\n[Wants] 99 mobile data", plan.text)
        self.assertEqual(plan.summary.needs_spent, 472)
        self.assertEqual(plan.summary.wants_spent, 99)
        self.assertEqual(plan.summary.remaining_total, 9429)

    def test_refuses_to_move_onto_a_date_that_already_has_the_line(self):
        twice = BASELINE + "\nSeptember 5\n[Need] 80 fare\n"

        with self.assertRaises(DuplicateEntryError):
            move_entries(
                twice,
                [parse_entry("80 fare", date(2026, 9, 1))],
                to_date=date(2026, 9, 5),
            )

    def test_refuses_a_source_date_whose_line_is_absent(self):
        with self.assertRaises(LedgerFormatError):
            move_entries(
                BASELINE,
                [parse_entry("999 rent", date(2026, 9, 2))],
                to_date=date(2026, 9, 8),
            )

    def test_refuses_a_move_that_keeps_the_same_date(self):
        with self.assertRaises(ValueError):
            move_entries(
                BASELINE,
                [parse_entry("80 fare", date(2026, 9, 1))],
                to_date=date(2026, 9, 1),
            )

    def test_refuses_a_move_across_months(self):
        with self.assertRaises(ValueError):
            move_entries(
                BASELINE,
                [parse_entry("80 fare", date(2026, 9, 1))],
                to_date=date(2026, 10, 1),
            )


class LoanTests(unittest.TestCase):
    def test_setting_a_balance_never_becomes_an_expense(self):
        plan = set_loan_balance(BASELINE, "Loan B", 150)

        self.assertIn("Loan B - 150", plan.text)
        self.assertIn("Total: 1350", plan.text)
        self.assertEqual(plan.summary.posted_expenses, 571)
        self.assertEqual(plan.summary.remaining_total, 9429)

    def test_a_payment_reduces_the_balance_and_the_loans_total(self):
        plan = pay_loan(BASELINE, "Loan A", 200)

        self.assertIn("Loan A - 1000", plan.text)
        self.assertIn("Total: 1400", plan.text)
        self.assertIn("no funding source recorded", plan.note)
        self.assertEqual(plan.summary.posted_expenses, 571)

    def test_a_payment_never_pushes_a_balance_below_zero(self):
        plan = pay_loan(BASELINE, "Loan B", 1000)

        self.assertIn("Loan B - 0", plan.text)

    def test_a_savings_funded_payment_lowers_savings_remaining(self):
        plan = pay_loan(BASELINE, "Loan A", 300, funded_by="savings")

        self.assertIn("Savings:\nUsed for loans: 300\nTotal: 0", plan.text)
        self.assertEqual(plan.summary.savings_remaining, 1700)
        self.assertEqual(plan.summary.remaining_total, 9129)
        self.assertEqual(plan.summary.confirmed_cash, 9429)

    def test_savings_funded_payments_accumulate(self):
        once = pay_loan(BASELINE, "Loan A", 300, funded_by="savings")
        twice = pay_loan(once.text, "Loan A", 200, funded_by="savings")

        self.assertIn("Used for loans: 500", twice.text)
        self.assertEqual(twice.summary.savings_remaining, 1500)

    def test_a_cash_funded_payment_lowers_confirmed_cash_only(self):
        plan = pay_loan(BASELINE, "Loan A", 400, funded_by="cash")

        self.assertIn("Cash-funded debt payments: 400", plan.text)
        self.assertIn("Confirmed cash remaining: 9029", plan.text)
        self.assertEqual(plan.summary.cash_funded_debt, 400)
        self.assertEqual(plan.summary.confirmed_cash, 9029)
        self.assertEqual(plan.summary.remaining_total, 9429)

    def test_a_cash_funded_payment_needs_the_cash_section(self):
        without_cash = BASELINE.replace(
            "Cash Reconciliation:\n"
            "Confirmed income: 10000\n"
            "Confirmed posted expenses: 571\n"
            "Cash-funded debt payments: 0\n"
            "Confirmed cash remaining: 9429\n\n",
            "",
        )

        with self.assertRaises(LedgerFormatError):
            pay_loan(without_cash, "Loan A", 100, funded_by="cash")

    def test_an_unknown_loan_name_is_rejected(self):
        with self.assertRaises(LedgerError):
            pay_loan(BASELINE, "Loan Z", 100)

    def test_a_sheet_without_a_loans_block_is_rejected(self):
        without_loans = BASELINE.replace(
            "Loans:\nLoan A - 1200\nLoan B - 400\nTotal: 1600\n\n", ""
        )

        with self.assertRaises(LedgerFormatError):
            set_loan_balance(without_loans, "Loan A", 100)

    def test_payments_reject_non_positive_amounts(self):
        with self.assertRaises(ValueError):
            pay_loan(BASELINE, "Loan A", 0)


class RolloverTests(unittest.TestCase):
    def test_rollover_carries_loans_forward_and_resets_the_month(self):
        plan = plan_rollover(BASELINE, "October", 12000)

        self.assertIn("October Expenses Log", plan.text)
        self.assertNotIn("September 1", plan.text)
        self.assertIn("Loans:\nLoan A - 1200\nLoan B - 400\nTotal: 1600\n", plan.text)
        self.assertIn("In:\n10/01/26 - 12000\nTotal: 12000", plan.text)
        self.assertIn("Needs - 6000 Max", plan.text)
        self.assertIn("Wants - 3600 Max", plan.text)
        self.assertIn("Savings:\nTotal: 0", plan.text)
        self.assertEqual(plan.summary.needs_remaining, 6000)
        self.assertEqual(plan.summary.wants_remaining, 3600)
        self.assertEqual(plan.summary.savings_remaining, 2400)
        self.assertEqual(plan.summary.remaining_total, 12000)
        self.assertEqual(plan.summary.confirmed_cash, 12000)

    def test_rollover_writes_a_date_heading_for_every_day(self):
        plan = plan_rollover(BASELINE, "October", 12000)

        self.assertIn("\nOctober 31\n", plan.text)
        self.assertNotIn("October 32", plan.text)
        self.assertEqual(plan.text.count("October "), 32)

    def test_rollover_drops_prior_month_expenses(self):
        spent = BASELINE.replace("[Need] 350 rent", "[Need] 350 rent\n[Wants] 500 gym")

        plan = plan_rollover(spent, "October", 12000)

        self.assertNotIn("350 rent", plan.text)
        self.assertNotIn("500 gym", plan.text)
        self.assertEqual(plan.summary.posted_expenses, 0)

    def test_rollover_advances_the_year_when_the_month_wraps(self):
        plan = plan_rollover(BASELINE, "January", 5000)

        self.assertIn("01/01/27 - 5000", plan.text)
        self.assertIn("January Expenses Log", plan.text)

    def test_rollover_requires_positive_opening_income(self):
        with self.assertRaises(ValueError):
            plan_rollover(BASELINE, "October", 0)

    def test_rollover_rejects_a_source_that_is_not_a_ledger(self):
        with self.assertRaises(LedgerFormatError):
            plan_rollover("just some notes", "October", 1000)

    def test_sheet_month_is_detected_from_the_title(self):
        self.assertEqual(sheet_month(BASELINE.splitlines()), "September")


class BudgetSplitTests(unittest.TestCase):
    """The split is the user's own, and it is validated before it is used."""

    def test_a_split_that_does_not_add_up_to_one_hundred_is_rejected(self):
        for values in ((60, 25, 10), (50, 30, 30), (0, 0, 0), (34, 33, 34)):
            with self.subTest(values=values), self.assertRaises(ValueError) as caught:
                BudgetSplit(*values)

            self.assertIn("add up to exactly 100", str(caught.exception))

    def test_a_percentage_outside_zero_to_one_hundred_is_rejected(self):
        for values in ((-1, 50, 51), (101, 0, -1), (0, 120, -20)):
            with self.subTest(values=values), self.assertRaises(ValueError) as caught:
                BudgetSplit(*values)

            self.assertIn("between 0 and 100", str(caught.exception))

    def test_a_percentage_that_is_not_a_whole_number_is_rejected(self):
        for value in ("60", 60.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BudgetSplit(needs=value, wants=25, savings=15)

    def test_the_label_is_the_split_written_onto_the_sheet(self):
        self.assertEqual(BudgetSplit(60, 25, 15).label, "60/25/15")
        self.assertEqual(BudgetSplit(100, 0, 0).label, "100/0/0")
        self.assertEqual(BudgetSplit(5, 5, 90).label, "5/5/90")

    def test_maximums_are_the_splits_share_of_income_rounded_half_to_even(self):
        self.assertEqual(BudgetSplit(60, 25, 15).maxima(12000), (7200, 3000, 1800))
        self.assertEqual(BudgetSplit(50, 30, 20).maxima(10000), (5000, 3000, 2000))
        self.assertEqual(BudgetSplit(33, 33, 34).maxima(1000), (330, 330, 340))


class ConfigureSplitTests(unittest.TestCase):
    """`config` writes the user's split into the sheet and recomputes from it."""

    def test_config_writes_the_users_split_into_the_sheet(self):
        plan = configure_split(BASELINE, BudgetSplit(60, 25, 15))

        self.assertIn("\n60/25/15\nNeeds - 6000 Max\nWants - 2500 Max\n", plan.text)
        self.assertNotIn("50/30/20", plan.text)
        self.assertEqual(plan.summary.needs_max, 6000)
        self.assertEqual(plan.summary.wants_max, 2500)
        self.assertEqual(plan.summary.savings_max, 1500)

    def test_the_maximums_and_remainders_follow_the_configured_split(self):
        plan = configure_split(BASELINE, BudgetSplit(60, 25, 15))

        self.assertEqual(plan.summary.needs_remaining, 5528)
        self.assertEqual(plan.summary.wants_remaining, 2401)
        self.assertEqual(plan.summary.savings_remaining, 1500)
        # The split redistributes the budget, it does not change what was spent.
        self.assertEqual(plan.summary.remaining_total, 9429)
        self.assertEqual(plan.summary.posted_expenses, 571)

    def test_entries_keep_the_label_they_were_logged_with(self):
        plan = configure_split(BASELINE, BudgetSplit(60, 25, 15))

        before = [line for line in BASELINE.splitlines() if line.startswith("[")]
        after = [line for line in plan.text.splitlines() if line.startswith("[")]
        self.assertEqual(after, before)
        self.assertIn("[Need] 350 rent", plan.text)
        self.assertIn("[Wants] 99 mobile data", plan.text)

    def test_any_split_label_is_accepted_on_read(self):
        for label, split in (
            ("70/20/10", BudgetSplit(70, 20, 10)),
            ("5/5/90", BudgetSplit(5, 5, 90)),
            ("100/0/0", BudgetSplit(100, 0, 0)),
        ):
            with self.subTest(label=label):
                sheet = BASELINE.replace("50/30/20", label)
                self.assertEqual(read_budget_split(sheet.splitlines()), split)

    def test_a_hand_written_split_drives_the_maximums(self):
        sheet = BASELINE.replace("50/30/20", "70/20/10")

        plan = plan_update(sheet, [parse_entry("33 bread", date(2026, 9, 2))])

        self.assertIn("\n70/20/10\nNeeds - 7000 Max\nWants - 2000 Max\n", plan.text)
        self.assertEqual(plan.summary.needs_max, 7000)
        self.assertEqual(plan.summary.wants_max, 2000)
        self.assertEqual(plan.summary.savings_max, 1000)

    def test_changing_the_split_again_recomputes_the_maximums(self):
        changed = configure_split(BASELINE, BudgetSplit(60, 25, 15)).text

        plan = configure_split(changed, BudgetSplit(45, 25, 30))

        self.assertIn("\n45/25/30\nNeeds - 4500 Max\nWants - 2500 Max\n", plan.text)
        self.assertEqual(plan.summary.savings_max, 3000)
        self.assertEqual(plan.summary.savings_remaining, 3000)

    def test_repeating_the_same_split_changes_nothing_and_says_so(self):
        changed = configure_split(BASELINE, BudgetSplit(60, 25, 15)).text

        again = configure_split(changed, BudgetSplit(60, 25, 15))

        self.assertEqual(again.text, changed)
        self.assertIn("already", again.note)

    def test_setting_the_original_split_back_restores_the_sheet(self):
        changed = configure_split(BASELINE, BudgetSplit(60, 25, 15)).text

        restored = configure_split(changed, BudgetSplit(50, 30, 20))

        self.assertEqual(restored.text, BASELINE)

    def test_an_unusable_heading_is_reported_rather_than_read_as_a_default(self):
        sheet = BASELINE.replace("50/30/20", "60/25/10")

        with self.assertRaises(LedgerFormatError):
            plan_update(sheet, [parse_entry("33 bread", date(2026, 9, 2))])

    def test_config_can_repair_a_hand_edited_heading(self):
        sheet = BASELINE.replace("50/30/20", "60/25/10")

        plan = configure_split(sheet, BudgetSplit(60, 25, 15))

        self.assertIn("\n60/25/15\nNeeds - 6000 Max\n", plan.text)
        self.assertEqual(plan.summary.needs_max, 6000)

    def test_a_sheet_with_no_budget_heading_at_all_is_rejected(self):
        without = BASELINE.replace("50/30/20\n", "")

        with self.assertRaises(LedgerFormatError):
            configure_split(without, BudgetSplit(60, 25, 15))

    def test_rollover_carries_the_users_split_into_the_new_month(self):
        changed = configure_split(BASELINE, BudgetSplit(60, 25, 15)).text

        plan = plan_rollover(changed, "October", 12000)

        self.assertIn("\n60/25/15\nNeeds - 7200 Max\nWants - 3000 Max\n", plan.text)
        self.assertEqual(plan.summary.needs_remaining, 7200)
        self.assertEqual(plan.summary.wants_remaining, 3000)
        self.assertEqual(plan.summary.savings_remaining, 1800)


class UnconfiguredSplitTests(unittest.TestCase):
    """Without a split, every budget calculation refuses instead of guessing."""

    def unconfigured(self):
        """A complete sheet whose split has never been set."""
        return BASELINE.replace("50/30/20", BUDGET_PLACEHOLDER)

    def test_the_placeholder_means_no_split_has_been_configured(self):
        sheet = self.unconfigured()

        self.assertIsNone(read_budget_split(sheet.splitlines()))
        self.assertIn(BUDGET_PLACEHOLDER, sheet)

    def test_add_refuses_and_names_the_config_command(self):
        with self.assertRaises(BudgetNotConfiguredError) as caught:
            plan_update(self.unconfigured(), [parse_entry("33 bread", date(2026, 9, 2))])

        message = str(caught.exception)
        self.assertIn("no Needs/Wants/Savings split", message)
        self.assertIn("config --needs", message)

    def test_rollover_move_and_the_loan_commands_refuse_too(self):
        sheet = self.unconfigured()

        with self.assertRaises(BudgetNotConfiguredError):
            plan_rollover(sheet, "October", 12000)
        with self.assertRaises(BudgetNotConfiguredError):
            move_entries(
                sheet,
                [parse_entry("350 rent", date(2026, 9, 2))],
                to_date=date(2026, 9, 8),
            )
        with self.assertRaises(BudgetNotConfiguredError):
            set_loan_balance(sheet, "Loan B", 150)
        with self.assertRaises(BudgetNotConfiguredError):
            pay_loan(sheet, "Loan A", 200)

    def test_recompute_refuses_because_there_is_no_maximum_to_check(self):
        with self.assertRaises(BudgetNotConfiguredError):
            recompute(self.unconfigured())

    def test_a_refused_run_leaves_the_sheet_byte_identical(self):
        sheet = self.unconfigured()

        with self.assertRaises(BudgetNotConfiguredError):
            plan_update(sheet, [parse_entry("33 bread", date(2026, 9, 2))])

        self.assertEqual(sheet, self.unconfigured())


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.md"
        self.path.write_text(BASELINE, encoding="utf-8")

    def test_apply_writes_what_was_staged_and_verifies_the_readback(self):
        ledger = LocalFileLedger(self.path)
        baseline = ledger.read_bytes()
        plan = plan_update(
            ledger.read_text(), [parse_entry("33 bread", date(2026, 9, 2))]
        )

        written = ledger.apply(baseline, plan.text.encode("utf-8"))

        self.assertEqual(written, self.path)
        self.assertEqual(ledger.read_bytes(), plan.text.encode("utf-8"))
        leftovers = [item.name for item in self.path.parent.iterdir() if item.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_apply_aborts_when_the_ledger_changed_after_staging(self):
        ledger = LocalFileLedger(self.path)
        baseline = ledger.read_bytes()
        plan = plan_update(
            ledger.read_text(), [parse_entry("33 bread", date(2026, 9, 2))]
        )
        self.path.write_text(
            BASELINE + "\nSeptember 9\n[Need] 5 water\n", encoding="utf-8"
        )
        changed = self.path.read_text(encoding="utf-8")

        with self.assertRaises(ConcurrentModificationError):
            ledger.apply(baseline, plan.text.encode("utf-8"))

        self.assertEqual(self.path.read_text(encoding="utf-8"), changed)

    def test_create_refuses_to_clobber_an_existing_file(self):
        ledger = LocalFileLedger(self.path)

        with self.assertRaises(LedgerError):
            ledger.create(b"replacement", force=False)

        self.assertEqual(ledger.read_bytes(), BASELINE.encode("utf-8"))

    def test_create_writes_a_new_file_and_force_overwrites(self):
        target = self.path.parent / "next-month.md"
        ledger = LocalFileLedger(target)

        ledger.create(b"first\n")

        self.assertEqual(target.read_bytes(), b"first\n")
        ledger.create(b"second\n", force=True)
        self.assertEqual(target.read_bytes(), b"second\n")

    def test_a_missing_ledger_is_a_clear_error(self):
        with self.assertRaises(LedgerError):
            LocalFileLedger(self.path.parent / "absent.md").read_bytes()

    def test_lock_is_exclusive_and_released(self):
        lock_path = lock_path_for(self.path)
        other = LedgerLock(lock_path, timeout=0.2)

        with LedgerLock(lock_path):
            with self.assertRaises(LedgerError):
                other.__enter__()

        with LedgerLock(lock_path):
            pass

    def test_a_stale_lock_is_reclaimed(self):
        lock_path = lock_path_for(self.path)
        lock_path.write_text("999999\n", encoding="utf-8")
        stale = (datetime.now() - timedelta(hours=1)).timestamp()
        os.utime(lock_path, (stale, stale))

        with LedgerLock(lock_path, timeout=1.0):
            pass


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.md"
        self.path.write_text(BASELINE, encoding="utf-8")

    def run_cli(self, *arguments):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = finance_logger.main(list(arguments))
        return code, buffer.getvalue()

    def test_dry_run_prints_the_whole_sheet_and_writes_nothing(self):
        code, output = self.run_cli(
            "add", "--ledger", str(self.path), "--entry", "33 bread",
            "--date", "2026-09-02",
        )

        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", output)
        self.assertIn("[Need] 33 bread", output)
        self.assertIn("Remaining:", output)
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)

    def test_apply_writes_the_sheet_and_returns_the_complete_result(self):
        code, output = self.run_cli(
            "add", "--ledger", str(self.path), "--entry", "33 bread",
            "--date", "2026-09-02", "--apply",
        )

        self.assertEqual(code, 0)
        self.assertIn("APPLIED", output)
        self.assertIn("[Need] 33 bread", self.path.read_text(encoding="utf-8"))
        self.assertFalse(lock_path_for(self.path).exists())

    def test_a_replayed_event_id_writes_nothing(self):
        arguments = (
            "add", "--ledger", str(self.path), "--entry", "33 bread",
            "--date", "2026-09-02", "--event-id", "evt-1", "--apply",
        )
        self.run_cli(*arguments)

        code, output = self.run_cli(*arguments)

        self.assertEqual(code, 0)
        self.assertIn("REPLAYED EVENT", output)
        self.assertEqual(
            self.path.read_text(encoding="utf-8").count("[Need] 33 bread"), 1
        )

    def test_income_only_call_updates_the_budget_maxes(self):
        code, output = self.run_cli(
            "add", "--ledger", str(self.path), "--income", "2000",
            "--date", "2026-09-07", "--apply",
        )

        self.assertEqual(code, 0)
        self.assertIn("Needs - 6000 Max", output)
        self.assertIn("09/07/26 - 2000", self.path.read_text(encoding="utf-8"))

    def test_a_duplicate_entry_is_reported_rather_than_written(self):
        with self.assertRaises(DuplicateEntryError):
            self.run_cli(
                "add", "--ledger", str(self.path), "--entry", "99 mobile data",
                "--date", "2026-09-02", "--apply",
            )

        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)

    def test_rollover_command_creates_the_next_month(self):
        code, output = self.run_cli(
            "rollover", "--ledger", str(self.path), "--to-month", "October",
            "--opening-income", "12000", "--apply",
        )

        self.assertEqual(code, 0)
        self.assertIn("APPLIED", output)
        target = self.path.parent / "october-expenses-log.md"
        self.assertTrue(target.exists())
        text = target.read_text(encoding="utf-8")
        self.assertIn("October Expenses Log", text)
        self.assertIn("Loan A - 1200", text)

    def test_rollover_refuses_to_overwrite_an_existing_month(self):
        arguments = (
            "rollover", "--ledger", str(self.path), "--to-month", "October",
            "--opening-income", "12000", "--apply",
        )
        self.run_cli(*arguments)

        with self.assertRaises(LedgerError):
            self.run_cli(*arguments)

    def test_a_bad_entry_is_reported_rather_than_written(self):
        with self.assertRaises(ValueError):
            self.run_cli(
                "add", "--ledger", str(self.path), "--entry", "food", "--apply"
            )

        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)


    def config_arguments(self, needs="60", wants="25", savings="15"):
        return (
            "config", "--ledger", str(self.path), "--needs", needs,
            "--wants", wants, "--savings", savings,
        )

    def test_config_applies_the_users_split_and_returns_the_whole_sheet(self):
        code, output = self.run_cli(*self.config_arguments(), "--apply")

        self.assertEqual(code, 0)
        self.assertIn("APPLIED", output)
        self.assertIn("--- FULL SHEET ---", output)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("\n60/25/15\nNeeds - 6000 Max\nWants - 2500 Max\n", text)
        self.assertNotIn("50/30/20", text)
        self.assertFalse(lock_path_for(self.path).exists())

    def test_config_without_apply_writes_nothing(self):
        code, output = self.run_cli(*self.config_arguments())

        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", output)
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)

    def test_a_split_that_does_not_add_up_to_one_hundred_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.run_cli(*self.config_arguments(savings="10"), "--apply")

        self.assertIn("add up to exactly 100", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)

    def test_an_out_of_range_percentage_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.run_cli(
                "config", "--ledger", str(self.path), "--needs", "101",
                "--wants", "0", "--savings", "-1", "--apply",
            )

        self.assertIn("between 0 and 100", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)

    def test_a_non_integer_percentage_is_rejected_with_a_readable_message(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit):
            finance_logger.main(
                [
                    "config", "--ledger", str(self.path), "--needs", "sixty",
                    "--wants", "25", "--savings", "15", "--apply",
                ]
            )

        self.assertIn("must be a whole number between 0 and 100", buffer.getvalue())
        self.assertEqual(self.path.read_text(encoding="utf-8"), BASELINE)

    def test_add_refuses_on_an_unconfigured_ledger_and_writes_nothing(self):
        unconfigured = BASELINE.replace("50/30/20", BUDGET_PLACEHOLDER)
        self.path.write_text(unconfigured, encoding="utf-8")

        with self.assertRaises(BudgetNotConfiguredError) as caught:
            self.run_cli(
                "add", "--ledger", str(self.path), "--entry", "33 bread",
                "--date", "2026-09-02", "--apply",
            )

        self.assertIn("config", str(caught.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), unconfigured)

    def test_rollover_refuses_on_an_unconfigured_ledger(self):
        unconfigured = BASELINE.replace("50/30/20", BUDGET_PLACEHOLDER)
        self.path.write_text(unconfigured, encoding="utf-8")

        with self.assertRaises(BudgetNotConfiguredError):
            self.run_cli(
                "rollover", "--ledger", str(self.path), "--to-month", "October",
                "--opening-income", "12000", "--apply",
            )

        self.assertFalse((self.path.parent / "october-expenses-log.md").exists())
        self.assertEqual(self.path.read_text(encoding="utf-8"), unconfigured)

    def test_the_first_run_sequence_config_then_add_then_rollover(self):
        self.path.write_text(
            BASELINE.replace("50/30/20", BUDGET_PLACEHOLDER), encoding="utf-8"
        )

        config_code, _ = self.run_cli(*self.config_arguments(), "--apply")
        add_code, _ = self.run_cli(
            "add", "--ledger", str(self.path), "--entry", "33 bread",
            "--date", "2026-09-02", "--apply",
        )
        rollover_code, output = self.run_cli(
            "rollover", "--ledger", str(self.path), "--to-month", "October",
            "--opening-income", "12000", "--apply",
        )

        self.assertEqual((config_code, add_code, rollover_code), (0, 0, 0))
        october = self.path.parent / "october-expenses-log.md"
        self.assertIn("\n60/25/15\nNeeds - 7200 Max\n", october.read_text("utf-8"))
        self.assertIn("[Need] 33 bread", self.path.read_text(encoding="utf-8"))


class OfflineTests(unittest.TestCase):
    """The local path must keep working with the network switched off."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.md"
        self.path.write_text(BASELINE, encoding="utf-8")

    def block_the_network(self):
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("the local ledger path must not use the network")

        self.addCleanup(setattr, socket.socket, "connect", socket.socket.connect)
        self.addCleanup(setattr, socket.socket, "connect_ex", socket.socket.connect_ex)
        self.addCleanup(setattr, socket, "create_connection", socket.create_connection)
        self.addCleanup(setattr, socket, "getaddrinfo", socket.getaddrinfo)
        socket.socket.connect = refuse
        socket.socket.connect_ex = refuse
        socket.create_connection = refuse
        socket.getaddrinfo = refuse

    def test_entries_income_and_rollover_all_work_offline(self):
        self.block_the_network()
        buffer = io.StringIO()

        with contextlib.redirect_stdout(buffer):
            code = finance_logger.main(
                [
                    "add", "--ledger", str(self.path), "--month", "September",
                    "--date", "2026-09-12", "--entry", "42 groceries",
                    "--entry", "85 streaming subscription", "--income", "1500",
                    "--apply",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                finance_logger.main(
                    [
                        "rollover", "--ledger", str(self.path), "--to-month",
                        "October", "--opening-income", "12000", "--apply",
                    ]
                ),
                0,
            )

        october = self.path.parent / "october-expenses-log.md"
        self.assertTrue(october.exists())
        self.assertIn("October Expenses Log", buffer.getvalue())


class PurityTests(unittest.TestCase):
    """The default path must not depend on Google, the network, or extras."""

    ALLOWED_IMPORT_ROOTS = {
        "__future__",
        "argparse",
        "calendar",
        "dataclasses",
        "datetime",
        "decimal",
        "json",
        "os",
        "pathlib",
        "re",
        "sys",
        "tempfile",
        "time",
        "typing",
        "zoneinfo",
    }

    NETWORK_OR_GOOGLE_ROOTS = {
        "google",
        "googleapiclient",
        "google_auth_oauthlib",
        "requests",
        "urllib3",
        "urllib.request",
        "http.client",
        "socket",
        "ssl",
        "drive_adapter",
    }

    @staticmethod
    def top_level_imports(source):
        roots = set()
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_the_core_imports_only_the_standard_library(self):
        source = (SCRIPTS / "finance_logger.py").read_text(encoding="utf-8")
        roots = self.top_level_imports(source)

        self.assertEqual(roots - self.ALLOWED_IMPORT_ROOTS, set())
        self.assertEqual(roots & self.NETWORK_OR_GOOGLE_ROOTS, set())

    def test_importing_the_core_pulls_in_no_network_or_google_module(self):
        program = (
            "import sys; sys.path.insert(0, %r); import finance_logger; "
            "bad = sorted(m for m in %r if m in sys.modules); "
            "print(bad)"
        ) % (str(SCRIPTS), sorted(self.NETWORK_OR_GOOGLE_ROOTS))
        result = subprocess.run(
            [sys.executable, "-I", "-c", program],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_the_drive_adapter_is_opt_in_and_imports_without_google_libraries(self):
        source = (SCRIPTS / "drive_adapter.py").read_text(encoding="utf-8")
        roots = self.top_level_imports(source)

        self.assertEqual(roots & {"google", "googleapiclient"}, set())
        import drive_adapter

        self.assertNotIn("googleapiclient", sys.modules)
        self.assertNotIn("google", sys.modules)

    def test_the_drive_adapter_needs_credentials_before_it_can_do_anything(self):
        import drive_adapter

        with self.assertRaises(LedgerError):
            drive_adapter.load_credentials(Path("/nonexistent/credentials.json"))

    def test_the_default_ledger_path_has_no_absolute_or_profile_paths(self):
        self.assertFalse(finance_logger.default_ledger_path().is_absolute())


class ExampleAndTemplateTests(unittest.TestCase):
    def read_template(self):
        return (ROOT / "templates" / "blank-finance-sheet.txt").read_text(
            encoding="utf-8"
        )

    def test_the_blank_template_is_a_usable_january_sheet(self):
        template = self.read_template()

        self.assertEqual(sheet_month(template.splitlines()), "January")
        configured = configure_split(template, BudgetSplit(60, 25, 15)).text
        plan = plan_update(configured, [parse_entry("40 fare", date(2026, 1, 4))])

        self.assertIn("January 4\n[Need] 40 fare", plan.text)

    def test_the_blank_template_is_already_consistent(self):
        template = self.read_template()
        configured = configure_split(template, BudgetSplit(60, 25, 15)).text

        self.assertEqual(recompute(configured), configured)

    def test_the_blank_template_carries_no_split_until_the_user_sets_one(self):
        template = self.read_template()

        self.assertIsNone(read_budget_split(template.splitlines()))
        with self.assertRaises(BudgetNotConfiguredError):
            plan_update(template, [parse_entry("40 fare", date(2026, 1, 4))])

    def test_the_blank_template_is_exactly_the_generated_blank_sheet(self):
        self.assertEqual(build_blank_sheet("January", 2026), self.read_template())

    def test_the_sample_ledger_is_internally_consistent(self):
        sample = (ROOT / "examples" / "sample-ledger.md").read_text(encoding="utf-8")

        self.assertEqual(recompute(sample), sample)

    def test_the_sample_ledger_recomputes_after_a_new_entry(self):
        sample = (ROOT / "examples" / "sample-ledger.md").read_text(encoding="utf-8")

        plan = plan_update(sample, [parse_entry("10 water", date(2026, 9, 12))])

        self.assertEqual(plan.summary.posted_expenses, 774)
        self.assertEqual(plan.summary.needs_remaining, 5400)
        self.assertEqual(plan.summary.wants_remaining, 3426)
        self.assertEqual(plan.summary.savings_remaining, 1600)
        self.assertEqual(plan.summary.remaining_total, 10426)
        self.assertEqual(plan.summary.confirmed_cash, 11226)

    def test_the_sample_ledger_can_be_rolled_over(self):
        sample = (ROOT / "examples" / "sample-ledger.md").read_text(encoding="utf-8")

        plan = plan_rollover(sample, "October", 9000)

        self.assertEqual(plan.summary.remaining_total, 9000)
        self.assertIn("Loan A - 1200", plan.text)


class SkillMetadataTests(unittest.TestCase):
    """The installed skill file must carry the frontmatter an agent looks for."""

    def read_skill(self):
        return (ROOT / "SKILL.md").read_text(encoding="utf-8")

    def test_skill_frontmatter_has_a_name_and_a_description(self):
        text = self.read_skill()

        self.assertTrue(text.startswith("---\n"))
        header = text.split("---\n", 2)[1]
        fields = {}
        for line in header.splitlines():
            if ": " in line and not line.startswith((" ", "\t")):
                key, value = line.split(": ", 1)
                fields[key.strip()] = value.strip().strip('"')

        self.assertEqual(fields.get("name"), "finance-ledger")
        self.assertGreater(len(fields.get("description", "")), 40)

    def test_the_skill_documents_the_mandatory_first_run_split_step(self):
        text = self.read_skill()

        self.assertIn("First run", text)
        self.assertIn("ASK the user", text)
        self.assertIn("--needs", text)
        self.assertIn("Never assume 50/30/20", text)
        self.assertIn("recomputed from the new percentages", text)

    def test_every_reference_file_is_linked_from_the_skill(self):
        text = self.read_skill()
        names = sorted(path.name for path in (ROOT / "references").glob("*.md"))

        self.assertEqual(len(names), 6)
        for name in names:
            self.assertIn(f"references/{name}", text)

    def test_the_skill_documents_the_whole_sheet_contract(self):
        text = self.read_skill()

        self.assertIn("COMPLETE", text)
        self.assertIn("templates/blank-finance-sheet.txt", text)


if __name__ == "__main__":
    unittest.main()
