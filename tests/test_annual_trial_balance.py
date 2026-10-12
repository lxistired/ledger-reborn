"""Explicit calendar-year TB display tests, separate from frozen fixtures.

Expected cents below are independently stated synthetic account balances.
They are not outputs claimed to come from the QuickBooks original.
"""
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from ledger import Ledger


class AnnualTrialBalancePresentation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.book = Ledger(Path(self.directory.name) / 'book.sqlite')
        self.addCleanup(self.book.close)

    def journal(self, date, debit, credit, amount):
        return self.book.post_journal(date, 'Synthetic annual TB scenario', [
            {'account':debit, 'debit_cents':amount},
            {'account':credit, 'credit_cents':amount},
        ])

    def positive_two_years(self):
        self.journal('2024-01-01','1000','3000',50000)
        self.journal('2024-01-01','1000','3100',20000)
        self.journal('2024-02-01','1000','4000',10000)
        self.journal('2024-03-01','6100','1000',2000)
        self.journal('2025-02-01','1000','4000',7000)
        self.journal('2025-03-01','6100','1000',1500)

    def balances(self, result):
        return {r['code']:r['balance_cents'] for r in result['rows'] if r['balance_cents']}

    def test_explicit_year_view_matches_independent_accounts_and_default_remains_cumulative(self):
        self.positive_two_years()
        snapshot = self.book.list_data()
        audit = self.book.audit_log()
        cumulative = self.book.trial_balance('2025-12-31')
        annual = self.book.trial_balance('2025-12-31',fiscal_year_start='2025-01-01',retained_account='3100')
        self.assertEqual(self.balances(cumulative), {
            '1000':83500, '3000':-50000, '3100':-20000,
            '4000':-17000, '6100':3500,
        })
        self.assertEqual(self.balances(annual), {
            '1000':83500, '3000':-50000, '3100':-28000,
            '4000':-7000, '6100':1500,
        })
        self.assertEqual(cumulative['total_debit_cents'],87000)
        self.assertEqual(cumulative['total_credit_cents'],87000)
        self.assertEqual(annual['total_debit_cents'],85000)
        self.assertEqual(annual['total_credit_cents'],85000)
        self.assertEqual(annual['prior_year_profit_cents'],8000)
        self.assertEqual(annual['report_basis'],'calendar_year')
        self.assertEqual(cumulative['report_basis'],'cumulative')
        self.assertEqual(annual['cumulative_rows'],cumulative['rows'])
        self.assertTrue(annual['balanced'])
        self.assertEqual(self.book.list_data(),snapshot)
        self.assertEqual(self.book.audit_log(),audit)

    def test_no_selected_retained_account_uses_explicit_synthetic_row(self):
        self.positive_two_years()
        result = self.book.trial_balance('2025-12-31',fiscal_year_start='2025-01-01')
        balances = self.balances(result)
        self.assertEqual(balances['3100'],-20000)
        self.assertEqual(balances['__prior_year_profit__'],-8000)
        self.assertEqual(result['total_debit_cents'],85000)
        self.assertEqual(result['total_credit_cents'],85000)
        self.assertIsNone(result['retained_account'])
        self.assertTrue(result['balanced'])

    def test_prior_and_current_losses_are_preserved_as_debit_equity_and_annual_expense(self):
        self.journal('2024-01-01','1000','3000',50000)
        self.journal('2024-02-01','1000','4000',2000)
        self.journal('2024-03-01','6100','1000',10000)
        self.journal('2025-02-01','1000','4000',1000)
        self.journal('2025-03-01','6100','1000',3000)
        result = self.book.trial_balance('2025-12-31',fiscal_year_start='2025-01-01',retained_account='3100')
        self.assertEqual(self.balances(result), {
            '1000':40000, '3000':-50000, '3100':8000,
            '4000':-1000, '6100':3000,
        })
        self.assertEqual(result['prior_year_profit_cents'],-8000)
        self.assertEqual(result['total_debit_cents'],51000)
        self.assertEqual(result['total_credit_cents'],51000)
        self.assertTrue(result['balanced'])

    def test_actual_previous_year_closing_journal_is_not_transferred_twice(self):
        self.journal('2024-01-01','1000','3000',50000)
        self.journal('2024-02-01','1000','4000',10000)
        self.journal('2024-03-01','6100','1000',2000)
        self.book.post_journal('2024-12-31','Actual manual prior-year closing journal', [
            {'account':'4000','debit_cents':10000},
            {'account':'6100','credit_cents':2000},
            {'account':'3100','credit_cents':8000},
        ])
        self.journal('2025-02-01','1000','4000',7000)
        self.journal('2025-03-01','6100','1000',1500)
        before = self.book.trial_balance('2025-12-31')
        annual = self.book.trial_balance('2025-12-31',fiscal_year_start='2025-01-01',retained_account='3100')
        self.assertEqual(self.balances(annual), {
            '1000':63500, '3000':-50000, '3100':-8000,
            '4000':-7000, '6100':1500,
        })
        self.assertEqual(annual['prior_year_profit_cents'],0)
        self.assertEqual(annual['rows'],before['rows'])
        self.assertEqual(annual['total_debit_cents'],65000)
        self.assertEqual(annual['total_credit_cents'],65000)
        self.assertTrue(annual['balanced'])

    def test_non_calendar_start_missing_annual_scope_and_nonequity_retained_are_rejected(self):
        self.positive_two_years()
        before = self.book.list_data()
        for kwargs in (
            {'fiscal_year_start':'2025-04-01'},
            {'fiscal_year_start':'2024-01-01'},
            {'retained_account':'3100'},
            {'fiscal_year_start':'2025-01-01','retained_account':'1000'},
            {'fiscal_year_start':'2025-01-01','retained_account':'missing'},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.book.trial_balance('2025-12-31',**kwargs)
        self.assertEqual(self.book.list_data(),before)


if __name__ == '__main__':
    unittest.main()
