"""Independent synthetic business acceptance; not QuickBooks-original goldens.

Expected values were frozen before importing or executing the product engine.
Tests call the public Ledger API; they do not repeat its journal/report algorithm.
"""
from __future__ import annotations

import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
FIXTURES = BASE / "fixtures"
sys.path.insert(0, str(BASE))


def read_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FrozenFixtureTests(unittest.TestCase):
    def test_frozen_pre_product_fixtures(self):
        for line in (FIXTURES / "FROZEN.sha256").read_text().splitlines():
            digest, name = line.split("  ", 1)
            with self.subTest(fixture=name):
                self.assertEqual(hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest(), digest)

    def test_independent_golden_is_not_claimed_as_original(self):
        expected = read_json("expected.json")
        self.assertFalse(expected["provenance"]["quickbooks_original_output"])
        self.assertEqual(expected["provenance"]["amount_unit"], "integer cents")

    def test_golden_balance_equation_and_period_additivity(self):
        expected = read_json("expected.json")
        for date, bs in expected["as_of"].items():
            with self.subTest(as_of=date):
                self.assertEqual(bs["assets_cents"], bs["liabilities_cents"] + bs["owner_equity_cents"] + bs["retained_profit_cents"])
        for field in ["revenue_cents", "expenses_cents", "net_income_cents"]:
            self.assertEqual(expected["periods"]["2026-YTD"][field], expected["periods"]["2026-01"][field] + expected["periods"]["2026-02"][field])

    def test_synthetic_statement_matches_hand_calculated_bank(self):
        expected = read_json("expected.json")
        with (FIXTURES / "bank.csv").open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), expected["bank_rows"])
        self.assertEqual(len({r["external_id"] for r in rows}), len(rows))
        self.assertEqual(sum(int(r["amount_cents"]) for r in rows), expected["bank_final_cents"])
        for month in ["2026-01", "2026-02"]:
            self.assertEqual(sum(int(r["amount_cents"]) for r in rows if r["date"].startswith(month)), expected["periods"][month]["bank_change_cents"])


class LedgerBusinessAcceptance(unittest.TestCase):
    """Public API assertions against independently frozen financial outcomes."""

    def setUp(self):
        # Fail early if a fixture was edited after its pre-product freeze.
        for line in (FIXTURES / "FROZEN.sha256").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest(), digest)
        from ledger import Ledger
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-accounting-acceptance-")
        self.addCleanup(self.directory.cleanup)
        self.book = Ledger(Path(self.directory.name) / "book.sqlite")
        self.addCleanup(self.book.close)
        self.scenario = read_json("scenario.json")
        self.expected = read_json("expected.json")
        self.contacts = {}
        self.projects = {}
        self.documents = {}
        self.results = {}
        self.sale_costs = []
        self.seed()

    def seed(self):
        for account in self.scenario["accounts_to_create"]:
            self.book.create_account(account["code"], account["name"], account["type"])
        for contact in self.scenario["contacts"]:
            self.contacts[contact["key"]] = self.book.create_contact(contact["name"], kind=contact["kind"])["id"]
        for project in self.scenario["projects"]:
            self.projects[project["key"]] = self.book.create_project(project["name"])["id"]
        item = self.scenario["item"]
        self.item_id = self.book.create_item(item["sku"], item["name"])["id"]
        for event in self.scenario["events"]:
            self.apply(event)
        for period, rows in self.scenario["budgets"].items():
            for account, amount in rows.items():
                self.book.set_budget(period=period, account=account, amount_cents=amount)

    def apply(self, event):
        common = {"date": event["date"], "external_id": event["key"]}
        kind = event["type"]
        project_id = self.projects.get(event.get("project"))
        if kind == "journal":
            lines = []
            for raw in event["lines"]:
                row = {"account": raw["account"], "debit_cents": raw.get("debit_cents", 0), "credit_cents": raw.get("credit_cents", 0)}
                if raw.get("project"):
                    row["project_id"] = self.projects[raw["project"]]
                lines.append(row)
            result = self.book.post_journal(memo=event["memo"], lines=lines, **common)
        elif kind in ["invoice", "bill"]:
            method = self.book.create_invoice if kind == "invoice" else self.book.create_bill
            result = method(contact_id=self.contacts[event["contact"]], lines=event["lines"], project_id=project_id, **common)
            self.documents[event["key"]] = result["id"]
        elif kind == "buy_inventory":
            result = self.book.buy_inventory(item_id=self.item_id, qty=event["qty"], unit_cost_cents=event["unit_cost_cents"], offset_account=event["offset_account"], contact_id=self.contacts[event["contact"]], **common)
            if result.get("bill_id"):
                self.documents[event["key"]] = result["bill_id"]
        elif kind == "sell_inventory":
            result = self.book.sell_inventory(item_id=self.item_id, qty=event["qty"], unit_price_cents=event["unit_price_cents"], contact_id=self.contacts[event["contact"]], project_id=project_id, **common)
            self.documents[event["key"]] = result["id"]
            self.sale_costs.append(result["inventory_cost_cents"])
        elif kind == "receive_payment":
            result = self.book.receive_payment(invoice_id=self.documents[event["document"]], amount_cents=event["amount_cents"], **common)
        elif kind == "pay_bill":
            result = self.book.pay_bill(bill_id=self.documents[event["document"]], amount_cents=event["amount_cents"], **common)
        else:
            raise AssertionError(f"Unknown synthetic event {kind}")
        self.results[event["key"]] = result
        return result

    def snapshot(self):
        # A public, logical snapshot checks documents and movements as well as
        # balances. A balanced trial balance alone cannot detect leaked records.
        return self.book.list_data()

    def assert_pnl(self, report, expected):
        self.assertEqual(report["income_cents"], expected["revenue_cents"])
        self.assertEqual(report["expense_cents"], expected["expenses_cents"])
        self.assertEqual(report["net_profit_cents"], expected["net_income_cents"])

    def test_accrual_income_expense_accounts_months_and_ytd(self):
        for period, expected in self.expected["periods"].items():
            with self.subTest(period=period):
                report = self.book.profit_loss(expected["start"], expected["end"])
                self.assert_pnl(report, expected)
                actual = {r["code"]: r["amount_cents"] for r in report["rows"] if r["amount_cents"]}
                by_account = {**expected["income_by_account_cents"], **expected["expense_by_account_cents"]}
                self.assertEqual(actual, {k: v for k, v in by_account.items() if v})

    def test_project_classification_and_unassigned_costs(self):
        for period, expected in self.expected["periods"].items():
            summary = self.book.project_profitability(expected["start"], expected["end"])
            projects = {r["id"]: r for r in summary["rows"]}
            for key, values in expected["projects"].items():
                with self.subTest(period=period, project=key):
                    self.assert_pnl(projects[self.projects[key]], values)
                    self.assert_pnl(self.book.profit_loss(expected["start"], expected["end"], project_id=self.projects[key]), values)
            self.assertEqual(summary["total_net_profit_cents"], expected["net_income_cents"])
            self.assertEqual(summary["unassigned_income_cents"], 0)
            self.assertEqual(summary["unassigned_expense_cents"], expected["expenses_cents"] - sum(v["expenses_cents"] for v in expected["projects"].values()))

    def test_balance_sheet_historical_cutoffs_and_equation(self):
        for date, expected in self.expected["as_of"].items():
            with self.subTest(as_of=date):
                report = self.book.balance_sheet(date)
                self.assertEqual(report["assets_cents"], expected["assets_cents"])
                self.assertEqual(report["liabilities_cents"], expected["liabilities_cents"])
                self.assertEqual(report["equity_cents"], expected["owner_equity_cents"])
                self.assertEqual(report["current_earnings_cents"], expected["retained_profit_cents"])
                self.assertEqual(report["assets_cents"], report["liabilities_cents"] + report["total_equity_cents"])
                self.assertTrue(report["balanced"])
                self.assertEqual(report["difference_cents"], 0)
                actual = {r["code"]: r["amount_cents"] for r in report["rows"]}
                for code, amount in expected["balances_cents"].items():
                    self.assertEqual(actual.get(code, 0), amount, (date, code))
                self.assert_pnl(self.book.profit_loss("2026-01-01", date), expected)

    def test_trial_balance_every_business_date_and_each_journal_balanced(self):
        dates = sorted({e["date"] for e in self.scenario["events"]})
        for date in dates:
            with self.subTest(date=date):
                report = self.book.trial_balance(date)
                self.assertEqual(report["total_debit_cents"], report["total_credit_cents"])
                self.assertEqual(report["difference_cents"], 0)
                self.assertTrue(report["balanced"])
        for journal in self.book.list_data()["journals"]:
            self.assertEqual(sum(r["debit_cents"] for r in journal["lines"]), sum(r["credit_cents"] for r in journal["lines"]), journal["id"])

    def test_ar_ap_aging_restores_history_after_later_payments(self):
        for date, expected in self.expected["open_documents_cents"].items():
            with self.subTest(date=date):
                for category, getter, aging in [("invoices", self.book.get_invoice, self.book.ar_aging), ("bills", self.book.get_bill, self.book.ap_aging)]:
                    document_values = expected[category]
                    for key, amount in document_values.items():
                        doc = getter(self.documents[key], as_of=date)
                        self.assertEqual(doc["outstanding_cents"], amount, key)
                        self.assertEqual(doc["total_cents"] - doc["paid_cents"], amount)
                    report = aging(date)
                    self.assertEqual(report["total_outstanding_cents"], sum(document_values.values()))
                    self.assertEqual(report["subledger_difference_cents"], 0)
                    self.assertEqual(sum(report["buckets"].values()), sum(document_values.values()))
                    outstanding_rows = {r["document_id"]: r["outstanding_cents"] for r in report["rows"] if r["outstanding_cents"]}
                    self.assertEqual(outstanding_rows, {self.documents[k]: v for k, v in document_values.items() if v})
        january_a = self.book.get_invoice(self.documents["inv-retail-jan-a"], as_of="2026-01-31")
        self.assertEqual(january_a["paid_cents"], 60000)
        self.assertEqual(january_a["status"], "partial")
        self.assertEqual(self.book.get_invoice(self.documents["inv-retail-jan-a"])["status"], "paid")

    def test_inventory_moving_average_rounding_and_all_historical_positions(self):
        self.assertEqual(self.sale_costs, self.expected["inventory_sale_costs_cents"])
        for position in self.expected["inventory_movements"]:
            with self.subTest(date=position["date"]):
                report = self.book.inventory_report(as_of=position["date"])
                row = next(r for r in report["rows"] if r["id"] == self.item_id)
                self.assertEqual(Decimal(row["qty"]), Decimal(position["qty"]))
                self.assertEqual(row["value_cents"], position["carrying_cents"])
                self.assertEqual(report["total_value_cents"], position["carrying_cents"])
                self.assertEqual(report["subledger_difference_cents"], 0)
        self.assertEqual(100000 + 140000 + 80000 - sum(self.sale_costs), 108308)
        self.assertEqual(10 + 10 + 5 - 8 - 4 - 3 - 2, 8)

    def test_budget_actual_each_account_and_profit_variance(self):
        for period in ["2026-01", "2026-02"]:
            expected = self.expected["periods"][period]
            report = self.book.budget_vs_actual(period)
            self.assertEqual(report["budget_income_cents"], expected["budget_revenue_cents"])
            self.assertEqual(report["budget_expense_cents"], expected["budget_expenses_cents"])
            self.assertEqual(report["budget_profit_cents"], expected["budget_profit_cents"])
            self.assertEqual(report["actual_profit_cents"], expected["net_income_cents"])
            self.assertEqual(report["profit_variance_cents"], expected["profit_variance_cents"])
            actual_expected = {**expected["income_by_account_cents"], **expected["expense_by_account_cents"]}
            for row in report["rows"]:
                self.assertEqual(row["budget_cents"], self.scenario["budgets"][period].get(row["code"], 0))
                self.assertEqual(row["actual_cents"], actual_expected.get(row["code"], 0))
                self.assertEqual(row["variance_cents"], row["actual_cents"] - row["budget_cents"])

    def test_bank_csv_import_deduplicates_without_posting_again(self):
        before = self.book.balance_sheet("2026-02-28")
        result = self.book.import_bank_csv(str(FIXTURES / "bank.csv"))
        self.assertEqual(result["imported_count"], 19)
        self.assertEqual(result["duplicate_count"], 0)
        second = self.book.import_bank_csv(str(FIXTURES / "bank.csv"))
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["duplicate_count"], 19)
        self.assertEqual(len(self.book.list_data()["bank_transactions"]), 19)
        self.assertEqual(self.book.balance_sheet("2026-02-28"), before)

    def test_bank_matching_then_two_statement_reconciliations(self):
        imported = self.book.import_bank_csv(str(FIXTURES / "bank.csv"))
        register = self.book.bank_register()["rows"]
        self.assertEqual(len(register), 19)
        for transaction in imported["rows"]:
            candidates = [r for r in register if r["date"] == transaction["date"] and r["amount_cents"] == transaction["amount_cents"]]
            self.assertEqual(len(candidates), 1)
            self.book.match_bank_transaction(transaction["id"], candidates[0]["id"])
        self.assertEqual({r["status"] for r in self.book.list_data()["bank_transactions"]}, {"matched"})
        january_ids = [r["id"] for r in register if r["date"] <= "2026-01-31"]
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.book.reconcile("1000", "2026-01-31", 1458501, january_ids)
        self.assertEqual(self.snapshot(), before)
        january = self.book.reconcile("1000", "2026-01-31", 1458500, january_ids)
        self.assertEqual(january["difference_cents"], 0)
        self.assertEqual(january["outstanding_cents"], 0)
        february_ids = [r["id"] for r in register if r["date"] > "2026-01-31"]
        february = self.book.reconcile("1000", "2026-02-28", 1541700, february_ids)
        self.assertEqual(february["difference_cents"], 0)
        self.assertEqual(self.book.bank_register(uncleared_only=True)["rows"], [])
        self.assertTrue(all(r["cleared"] for r in self.book.bank_register(as_of="2026-01-31")["rows"]))

    def test_classify_new_bank_fee_only_once(self):
        result = self.book.import_bank_csv("date,description,amount_cents,external_id\n2026-03-02,Synthetic unbooked fee,-203,synthetic-new-fee\n")
        transaction = result["rows"][0]
        self.book.classify_bank_transaction(transaction["id"], "6500")
        report = self.book.profit_loss("2026-03-01", "2026-03-31")
        self.assertEqual(report["expense_cents"], 203)
        self.assertEqual(report["net_profit_cents"], -203)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.book.classify_bank_transaction(transaction["id"], "6500")
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_business_ids_do_not_double_post_and_conflicts_fail(self):
        before = self.snapshot()
        for key in ["owner-capital", "bill-stock-jan-a", "inv-retail-jan-a", "receipt-jan-retail-partial", "payment-service-jan-partial"]:
            event = next(e for e in self.scenario["events"] if e["key"] == key)
            duplicate = self.apply(event)
            self.assertEqual(duplicate["id"], self.results[key]["id"])
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ValueError):
            self.book.post_journal("2026-01-02", "conflicting owner capital", [{"account": "1000", "debit_cents": 1}, {"account": "3000", "credit_cents": 1}], external_id="owner-capital")
        self.assertEqual(self.snapshot(), before)

    def test_overselling_inventory_is_atomic_and_failed_id_can_be_reused(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.book.sell_inventory("2026-03-01", self.item_id, "9", 30000, self.contacts["customer"], external_id="retry-sale")
        self.assertEqual(self.snapshot(), before)
        result = self.book.sell_inventory("2026-03-01", self.item_id, "8", 30000, self.contacts["customer"], external_id="retry-sale")
        self.assertEqual(result["inventory_cost_cents"], 108308)
        row = next(r for r in self.book.inventory_report()["rows"] if r["id"] == self.item_id)
        self.assertEqual(Decimal(row["qty"]), Decimal(0))
        self.assertEqual(row["value_cents"], 0)

    def test_bad_document_or_unbalanced_journal_rolls_back_all_records(self):
        before = self.snapshot()
        bad_lines = [{"description": "valid first", "account": "6600", "amount_cents": 100}, {"description": "invalid second", "account": "DOES-NOT-EXIST", "amount_cents": 100}]
        with self.assertRaises(ValueError):
            self.book.create_bill("2026-03-01", self.contacts["vendor"], bad_lines, external_id="retry-bill")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ValueError):
            self.book.post_journal("2026-03-01", "unbalanced", [{"account": "1000", "debit_cents": 100}, {"account": "3000", "credit_cents": 99}], external_id="retry-journal")
        self.assertEqual(self.snapshot(), before)
        valid = self.book.create_bill("2026-03-01", self.contacts["vendor"], bad_lines[:1], external_id="retry-bill")
        self.assertEqual(valid["total_cents"], 100)
        self.assertEqual(valid["outstanding_cents"], 100)

    def test_managed_document_inventory_and_payment_entries_cannot_be_voided_directly(self):
        before = self.snapshot()
        entry_ids = [
            self.results["inv-retail-feb-a"]["entry_id"],
            self.results["bill-service-feb"]["entry_id"],
            self.results["stock-feb-cash"]["entry_id"],
            self.results["receipt-feb-retail-partial"]["entry_id"],
        ]
        for entry_id in entry_ids:
            with self.subTest(entry_id=entry_id):
                with self.assertRaises(ValueError):
                    self.book.void_journal(entry_id, "2026-03-01", memo="must not detach subledger")
                self.assertEqual(self.snapshot(), before)

    def test_managed_inventory_cannot_be_changed_without_quantity_movement(self):
        before = self.snapshot()
        operations = [
            lambda: self.book.post_journal("2026-03-01", "inventory amount without quantity", [{"account": "1200", "debit_cents": 100}, {"account": "1000", "credit_cents": 100}]),
            lambda: self.book.create_bill("2026-03-01", self.contacts["vendor"], [{"account": "1200", "description": "inventory bill without item movement", "amount_cents": 100}]),
        ]
        for operation in operations:
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.snapshot(), before)
        imported = self.book.import_bank_csv("date,description,amount_cents,external_id\n2026-03-01,inventory without quantity,-100,inventory-no-qty\n")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.book.classify_bank_transaction(imported["rows"][0]["id"], "1200")
        self.assertEqual(self.snapshot(), before)

    def test_overpayment_and_payment_before_document_fail_then_exact_balance_closes(self):
        invoice_id = self.documents["inv-retail-feb-a"]
        before = self.snapshot()
        for date, amount in [("2026-03-01", 52001), ("2026-02-04", 100), ("2026-03-01", -1), ("2026-03-01", 0)]:
            with self.subTest(date=date, amount=amount):
                with self.assertRaises(ValueError):
                    self.book.receive_payment(date, invoice_id, amount, external_id="retry-payment")
                self.assertEqual(self.snapshot(), before)
        self.book.receive_payment("2026-03-01", invoice_id, 52000, external_id="retry-payment")
        invoice = self.book.get_invoice(invoice_id)
        self.assertEqual(invoice["paid_cents"], 72000)
        self.assertEqual(invoice["outstanding_cents"], 0)
        self.assertEqual(invoice["status"], "paid")
        self.assertEqual(self.book.get_invoice(invoice_id, "2026-02-28")["outstanding_cents"], 52000)

    def test_closed_period_blocks_all_accounting_mutations(self):
        self.book.close_period("2026-01-31")
        before = self.snapshot()
        operations = [
            lambda: self.book.post_journal("2026-01-31", "late adjustment", [{"account": "6500", "debit_cents": 1}, {"account": "1000", "credit_cents": 1}]),
            lambda: self.book.create_invoice("2026-01-31", self.contacts["customer"], [{"account": "4100", "description": "late", "amount_cents": 100}]),
            lambda: self.book.create_bill("2026-01-31", self.contacts["vendor"], [{"account": "6600", "description": "late", "amount_cents": 100}]),
            lambda: self.book.buy_inventory("2026-01-31", self.item_id, "1", 100, offset_account="1000"),
            lambda: self.book.sell_inventory("2026-01-31", self.item_id, "1", 100, self.contacts["customer"]),
            lambda: self.book.pay_bill("2026-01-31", self.documents["bill-service-feb"], 100),
            lambda: self.book.receive_payment("2026-01-31", self.documents["inv-retail-feb-a"], 100),
            lambda: self.book.void_journal(self.results["owner-capital"]["id"], "2026-01-31"),
        ]
        for number, operation in enumerate(operations):
            with self.subTest(operation=number):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.snapshot(), before)
        self.book.post_journal("2026-03-01", "permitted next period", [{"account": "6500", "debit_cents": 1}, {"account": "1000", "credit_cents": 1}])
        self.assertEqual(self.book.profit_loss("2026-03-01", "2026-03-31")["net_profit_cents"], -1)

    def test_malformed_import_and_conflicting_bank_id_are_atomic(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.book.import_bank_csv("date,description,amount_cents,external_id\n2026-03-01,valid first,101,new-first\n2026-03-02,invalid second,1.2,new-second\n")
        self.assertEqual(self.snapshot(), before)
        self.book.import_bank_csv(str(FIXTURES / "bank.csv"))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.book.import_bank_csv("date,description,amount_cents,external_id\n2026-03-01,valid new row,101,new-first\n2026-01-02,conflict,999,bank-owner-capital\n")
        self.assertEqual(self.snapshot(), before)

    def test_decimal_stock_conserves_cost_to_last_cent(self):
        item = self.book.create_item("SYN-FRACTIONAL", "Synthetic fractional stock")
        purchase = self.book.buy_inventory("2026-03-01", item["id"], "2.5", 103, offset_account="1000")
        self.assertEqual(purchase["amount_cents"], 258)
        first = self.book.sell_inventory("2026-03-02", item["id"], "1.25", 200, self.contacts["customer"])
        last = self.book.sell_inventory("2026-03-03", item["id"], "1.25", 200, self.contacts["customer"])
        self.assertEqual(first["inventory_cost_cents"], 129)
        self.assertEqual(last["inventory_cost_cents"], 129)
        self.assertEqual(first["total_cents"], 250)
        row = next(r for r in self.book.inventory_report()["rows"] if r["id"] == item["id"])
        self.assertEqual(Decimal(row["qty"]), Decimal(0))
        self.assertEqual(row["value_cents"], 0)

    def test_invalid_date_float_money_and_bad_quantity_rejected_without_state_change(self):
        before = self.snapshot()
        operations = [
            lambda: self.book.post_journal("2026-02-30", "bad date", [{"account": "1000", "debit_cents": 1}, {"account": "3000", "credit_cents": 1}]),
            lambda: self.book.post_journal("2026-03-01", "float cents", [{"account": "1000", "debit_cents": 1.0}, {"account": "3000", "credit_cents": 1}]),
            lambda: self.book.buy_inventory("2026-03-01", self.item_id, "-1", 100),
            lambda: self.book.buy_inventory("2026-03-01", self.item_id, "NaN", 100),
            lambda: self.book.buy_inventory("2026-03-01", self.item_id, "0.0000001", 100),
        ]
        for number, operation in enumerate(operations):
            with self.subTest(operation=number):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.snapshot(), before)

    def test_backup_reopen_preserves_full_book_closed_period_and_new_work(self):
        from ledger import Ledger
        self.book.import_bank_csv(str(FIXTURES / "bank.csv"))
        self.book.close_period("2026-01-31")
        expected_snapshot = self.snapshot()
        destination = Path(self.directory.name) / "restored.sqlite"
        self.book.backup(destination)
        restored = Ledger(destination)
        self.addCleanup(restored.close)
        self.assertEqual(restored.list_data(), expected_snapshot)
        self.assertEqual(restored.status()["integrity"], "ok")
        self.assertEqual(restored.balance_sheet("2026-02-28"), self.book.balance_sheet("2026-02-28"))
        with self.assertRaises(ValueError):
            restored.post_journal("2026-01-31", "closed restored book", [{"account": "1000", "debit_cents": 1}, {"account": "3000", "credit_cents": 1}])
        restored.post_journal("2026-03-01", "new work after restoration", [{"account": "6500", "debit_cents": 100}, {"account": "1000", "credit_cents": 100}])
        self.assertEqual(restored.profit_loss("2026-03-01", "2026-03-31")["net_profit_cents"], -100)
        self.assertEqual(self.snapshot(), expected_snapshot)


if __name__ == "__main__":
    unittest.main()
