"""Calendar-year equity presentation checks with independently stated amounts.

These small synthetic scenarios do not modify the frozen acceptance fixtures
and do not claim to be QuickBooks-original output.
"""
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from ledger import Ledger


class CalendarYearEarningsPresentation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.book = Ledger(Path(self.directory.name) / 'book.sqlite')
        self.addCleanup(self.book.close)

    def journal(self, date, debit, credit, amount):
        return self.book.post_journal(date, 'Synthetic calendar-year scenario', [
            {'account': debit, 'debit_cents': amount},
            {'account': credit, 'credit_cents': amount},
        ])

    def seed_three_years(self):
        self.journal('2024-01-01', '1000', '3000', 10000000)
        self.journal('2024-01-01', '1000', '3100', 200000)
        self.journal('2024-04-01', '1000', '4000', 125000)
        self.journal('2024-05-01', '6100', '1000', 25000)
        self.journal('2025-03-15', '1000', '4000', 45000)
        self.journal('2025-05-01', '6100', '1000', 15000)
        self.journal('2026-03-15', '1000', '4000', 70000)
        self.journal('2026-05-01', '6100', '1000', 20000)

    def test_current_year_is_not_history_and_legacy_field_remains_compatible(self):
        self.seed_three_years()
        # Independently stated year profits: 2024=100000, 2025=30000,
        # 2026=50000 cents. Posted retained earnings are separate 200000 cents.
        expected = {
            '2024-12-31': (100000, 0, 100000),
            '2025-01-01': (100000, 100000, 0),
            '2025-12-31': (130000, 100000, 30000),
            '2026-06-30': (180000, 130000, 50000),
        }
        for as_of, (cumulative, prior, current) in expected.items():
            with self.subTest(as_of=as_of):
                result = self.book.balance_sheet(as_of)
                self.assertEqual(result['cumulative_profit_cents'], cumulative)
                self.assertEqual(result['current_earnings_cents'], cumulative)
                self.assertEqual(result['prior_year_profit_cents'], prior)
                self.assertEqual(result['current_year_profit_cents'], current)
                self.assertEqual(result['fiscal_year_start'], as_of[:4]+'-01-01')
                self.assertEqual(result['fiscal_year_policy'], 'calendar_year')
                self.assertEqual(result['current_year_profit_cents'],
                    self.book.profit_loss(as_of[:4]+'-01-01', as_of)['net_profit_cents'])
                self.assertEqual(result['total_equity_cents'], 10200000+cumulative)
                self.assertTrue(result['balanced'])
                posted = {r['code']: r for r in result['rows']}
                self.assertEqual(posted['3100']['amount_cents'], 200000)
                displayed = sum(r['amount_cents'] for r in result['display_rows'] if r['kind']=='equity')
                self.assertEqual(displayed, result['total_equity_cents'])

    def test_explicit_retained_merge_changes_only_presentation(self):
        self.seed_three_years()
        snapshot = self.book.list_data()
        audit = self.book.audit_log()
        plain = self.book.balance_sheet('2026-06-30')
        merged = self.book.balance_sheet('2026-06-30', retained_account='3100')
        self.assertEqual(merged['rows'], plain['rows'])
        self.assertEqual(merged['total_equity_cents'], plain['total_equity_cents'])
        self.assertEqual(merged['retained_account'], '3100')
        row = next(r for r in merged['display_rows'] if r['code']=='3100')
        self.assertEqual(row['posted_amount_cents'], 200000)
        self.assertEqual(row['prior_year_profit_cents'], 130000)
        self.assertEqual(row['amount_cents'], 330000)
        self.assertEqual(row['display_role'], 'explicit_retained_earnings')
        self.assertNotIn('prior_year_profit', {r['display_role'] for r in merged['display_rows']})
        self.assertEqual(sum(r['amount_cents'] for r in merged['display_rows'] if r['kind']=='equity'), 10380000)
        self.assertEqual(self.book.list_data(), snapshot)
        self.assertEqual(self.book.audit_log(), audit)

    def test_no_retained_account_is_guessed_by_name_and_invalid_account_rejected(self):
        self.book.create_account('7777', 'Retained Earnings', 'equity')
        self.journal('2024-01-01', '1000', '7777', 15000)
        self.journal('2024-02-01', '1000', '4000', 10000)
        result = self.book.balance_sheet('2025-01-31')
        rows = {r['code']: r for r in result['display_rows']}
        self.assertEqual(rows['7777']['amount_cents'], 15000)
        self.assertEqual(rows['7777']['display_role'], 'posted_account')
        self.assertEqual(rows['__prior_year_profit__']['amount_cents'], 10000)
        with self.assertRaises(ValueError):
            self.book.balance_sheet('2025-01-31', retained_account='1000')
        with self.assertRaises(ValueError):
            self.book.balance_sheet('2025-01-31', retained_account='does-not-exist')

    def test_cash_partial_payment_across_calendar_year_has_correct_current_year(self):
        customer = self.book.create_contact('Synthetic customer', 'customer')
        invoice = self.book.create_invoice('2024-12-10', customer['id'], [
            {'description':'Service', 'amount_cents':10000, 'account':'4000'},
        ])
        self.book.receive_payment('2024-12-20', invoice['id'], 8000)
        self.book.receive_payment('2025-01-20', invoice['id'], 2000)
        cash = self.book.balance_sheet('2025-01-31', basis='cash')
        accrual = self.book.balance_sheet('2025-01-31')
        self.assertEqual((cash['cumulative_profit_cents'], cash['prior_year_profit_cents'], cash['current_year_profit_cents']),
                         (10000, 8000, 2000))
        self.assertEqual((accrual['cumulative_profit_cents'], accrual['prior_year_profit_cents'], accrual['current_year_profit_cents']),
                         (10000, 10000, 0))
        self.assertEqual(self.book.profit_loss('2025-01-01','2025-01-31',basis='cash')['net_profit_cents'], 2000)
        self.assertTrue(cash['balanced'])
        self.assertTrue(accrual['balanced'])

    def test_current_year_loss_and_future_entries_do_not_change_historical_split(self):
        self.journal('2024-05-01', '1000', '4000', 10000)
        self.journal('2025-05-01', '6100', '1000', 12000)
        self.journal('2026-05-01', '1000', '4000', 999999)
        result = self.book.balance_sheet('2025-12-31')
        self.assertEqual(result['cumulative_profit_cents'], -2000)
        self.assertEqual(result['prior_year_profit_cents'], 10000)
        self.assertEqual(result['current_year_profit_cents'], -12000)
        self.assertEqual(result['total_equity_cents'], -2000)
        self.assertEqual(sum(r['amount_cents'] for r in result['display_rows'] if r['kind']=='equity'), -2000)
        self.assertTrue(result['balanced'])


if __name__ == '__main__':
    unittest.main()
