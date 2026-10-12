"""New backend workflows for the QuickBooks-style interface.

Uses independent, small synthetic amounts; does not edit frozen fixtures or
the original-derived reference ledger.
"""
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from ledger import Ledger


class MixedItemsAndPayments(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'book.sqlite'
        self.book = Ledger(self.path)
        self.addCleanup(self.book.close)
        self.customer = self.book.create_contact('Synthetic Customer','customer')['id']
        self.vendor = self.book.create_contact('Synthetic Vendor','vendor')['id']
        self.item = self.book.create_item('W','Widget')['id']
        self.book.post_journal('2026-01-01','Owner investment',[
            {'account':'1000','debit_cents':1000000},
            {'account':'3000','credit_cents':1000000},
        ])

    def purchase_and_invoice(self):
        bill = self.book.create_bill('2026-01-02',self.vendor,[
            {'item_id':self.item,'qty':'10','unit_cost_cents':1000},
            {'item_id':self.item,'qty':'10','unit_cost_cents':1400},
            {'description':'Rent','account':'6100','amount_cents':3000},
        ],external_id='mixed-bill')
        invoice = self.book.create_invoice('2026-01-05',self.customer,[
            {'item_id':self.item,'qty':'4','unit_price_cents':2000},
            {'description':'Service','account':'4000','amount_cents':3000},
            {'item_id':self.item,'qty':'3','unit_price_cents':2500},
        ],external_id='mixed-invoice')
        return bill,invoice

    def test_mixed_inventory_and_service_lines_have_real_cost_and_persist_prices(self):
        bill,invoice = self.purchase_and_invoice()
        self.assertEqual(bill['total_cents'],27000)
        self.assertEqual(invoice['total_cents'],18500)
        self.assertEqual(invoice['inventory_cost_cents'],8400)
        self.assertEqual([line['cogs_cents'] for line in invoice['lines']],[4800,0,3600])
        self.assertEqual([line.get('unit_price_cents') for line in invoice['lines']],[2000,None,2500])
        inventory = self.book.inventory_report()['rows'][0]
        self.assertEqual(inventory['qty'],'13')
        self.assertEqual(inventory['value_cents'],15600)
        self.assertEqual(self.book.inventory_report()['subledger_difference_cents'],0)
        pnl = self.book.profit_loss('2026-01-01','2026-01-31')
        self.assertEqual((pnl['income_cents'],pnl['expense_cents'],pnl['net_profit_cents']),(18500,11400,7100))
        reopened = Ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual([line.get('unit_price_cents') for line in reopened.get_invoice(invoice['id'])['lines']],[2000,None,2500])
        self.assertEqual([line.get('unit_cost_cents') for line in reopened.get_bill(bill['id'])['lines']],[1000,1400,None])

    def test_repeat_mixed_document_and_overselling_last_line_are_atomic(self):
        _,invoice = self.purchase_and_invoice()
        repeated = self.book.create_invoice('2026-01-05',self.customer,[
            {'item_id':self.item,'qty':'4','unit_price_cents':2000},
            {'description':'Service','account':'4000','amount_cents':3000},
            {'item_id':self.item,'qty':'3','unit_price_cents':2500},
        ],external_id='mixed-invoice')
        self.assertEqual(repeated,invoice)
        before = self.book.list_data()
        audit = self.book.audit_log()
        with self.assertRaises(ValueError):
            self.book.create_invoice('2026-01-06',self.customer,[
                {'item_id':self.item,'qty':'5','unit_price_cents':2000},
                {'item_id':self.item,'qty':'999','unit_price_cents':2000},
            ])
        self.assertEqual(self.book.list_data(),before)
        self.assertEqual(self.book.audit_log(),audit)

    def test_batch_receive_and_pay_partial_amounts_and_deduplicate_requests(self):
        bill,invoice = self.purchase_and_invoice()
        second_invoice = self.book.create_invoice('2026-01-06',self.customer,[{'amount_cents':1000}])
        second_bill = self.book.create_bill('2026-01-06',self.vendor,[{'amount_cents':2500}])
        receipt = self.book.allocate_payments('2026-01-10',self.customer,[
            {'document_id':invoice['id'],'amount_cents':10000},
            {'document_id':second_invoice['id'],'amount_cents':500},
        ],external_id='batch-receipt')
        self.assertEqual(receipt['total_cents'],10500)
        self.assertEqual(len(receipt['payments']),2)
        self.assertEqual(self.book.allocate_payments('2026-01-10',self.customer,[
            {'document_id':invoice['id'],'amount_cents':10000},
            {'document_id':second_invoice['id'],'amount_cents':500},
        ],external_id='batch-receipt'),receipt)
        self.book.allocate_payments('2026-01-10',self.vendor,[
            {'document_id':bill['id'],'amount_cents':10000},
            {'document_id':second_bill['id'],'amount_cents':500},
        ],external_id='batch-payment')
        self.assertEqual(self.book.ar_aging('2026-01-31')['total_outstanding_cents'],9000)
        self.assertEqual(self.book.ap_aging('2026-01-31')['total_outstanding_cents'],19000)
        self.assertTrue(self.book.balance_sheet('2026-01-31')['balanced'])

    def test_batch_late_overpayment_and_wrong_contact_roll_back_earlier_payments(self):
        _,invoice = self.purchase_and_invoice()
        second = self.book.create_invoice('2026-01-06',self.customer,[{'amount_cents':1000}])
        before = self.book.list_data()
        audit = self.book.audit_log()
        with self.assertRaises(ValueError):
            self.book.allocate_payments('2026-01-10',self.customer,[
                {'document_id':invoice['id'],'amount_cents':10000},
                {'document_id':second['id'],'amount_cents':1001},
            ])
        self.assertEqual(self.book.list_data(),before)
        self.assertEqual(self.book.audit_log(),audit)
        with self.assertRaises(ValueError):
            self.book.allocate_payments('2026-01-10',self.vendor,[{'document_id':invoice['id'],'amount_cents':1}])
        self.assertEqual(self.book.list_data(),before)

    def test_pay_bills_across_vendors_is_real_and_failed_last_allocation_rolls_back(self):
        second_vendor = self.book.create_contact('Second Vendor','vendor')['id']
        first = self.book.create_bill('2026-01-02',self.vendor,[{'amount_cents':1000}])
        second = self.book.create_bill('2026-01-02',second_vendor,[{'amount_cents':2000}])
        before = self.book.list_data()
        audit = self.book.audit_log()
        with self.assertRaises(ValueError):
            self.book.pay_bills('2026-01-03',[
                {'document_id':first['id'],'amount_cents':500},
                {'document_id':second['id'],'amount_cents':2001},
            ])
        self.assertEqual(self.book.list_data(),before)
        self.assertEqual(self.book.audit_log(),audit)
        result = self.book.pay_bills('2026-01-03',[
            {'document_id':first['id'],'amount_cents':500},
            {'document_id':second['id'],'amount_cents':750},
        ],external_id='vendor-batch')
        self.assertEqual(result['total_cents'],1250)
        self.assertEqual(result['vendor_ids'],[self.vendor,second_vendor])
        self.assertEqual(self.book.get_bill(first['id'])['outstanding_cents'],500)
        self.assertEqual(self.book.get_bill(second['id'])['outstanding_cents'],1250)
        self.assertEqual(self.book.ap_aging('2026-01-31')['total_outstanding_cents'],1750)
        self.assertEqual(self.book.bank_register()['balance_cents'],998750)

    def test_pay_bills_cannot_pay_sales_invoice_or_repeat_duplicate_document(self):
        invoice = self.book.create_invoice('2026-01-02',self.customer,[{'amount_cents':1000}])
        bill = self.book.create_bill('2026-01-02',self.vendor,[{'amount_cents':2000}])
        before = self.book.list_data()
        with self.assertRaises(ValueError):
            self.book.pay_bills('2026-01-03',[{'document_id':invoice['id'],'amount_cents':1}])
        with self.assertRaises(ValueError):
            self.book.pay_bills('2026-01-03',[
                {'document_id':bill['id'],'amount_cents':1},
                {'document_id':bill['id'],'amount_cents':1},
            ])
        self.assertEqual(self.book.list_data(),before)

    def test_cent_and_quantity_mapping_errors_cannot_corrupt_item_document(self):
        self.purchase_and_invoice()
        before = self.book.list_data()
        for line in (
            {'item_id':self.item,'qty':'1','unit_price_cents':2000.0},
            {'item_id':self.item,'qty':'1','unit_price_cents':2000,'amount_cents':1999},
            {'item_id':self.item,'qty':'1','unit_price_cents':2000,'account':'6100'},
            {'item_id':self.item,'qty':1.5,'unit_price_cents':2000},
            {'item_id':self.item,'qty':'1','unit_price_cents':2000,'cogs_cents':1},
        ):
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.book.create_invoice('2026-01-10',self.customer,[line])
        self.assertEqual(self.book.list_data(),before)

    def test_general_ledger_and_centers_use_historical_real_rows(self):
        bill,invoice = self.purchase_and_invoice()
        self.book.receive_payment('2026-01-10',invoice['id'],10000)
        gl = self.book.general_ledger('2026-01-05','2026-01-31',account='1100')
        self.assertEqual(gl['accounts'][0]['opening_balance_cents'],0)
        self.assertEqual([row['type'] for row in gl['rows']],['Invoice','Payment'])
        self.assertEqual([row['running_balance_cents'] for row in gl['rows']],[18500,8500])
        self.assertEqual(gl['accounts'][0]['closing_balance_cents'],8500)
        self.assertEqual(gl['accounts'][0]['difference_cents'],0)
        all_gl = self.book.general_ledger('2026-01-01','2026-01-31')
        self.assertEqual(all_gl['total_debit_cents'],all_gl['total_credit_cents'])
        listing = {row['code']:row for row in self.book.account_listing('2026-01-31')['rows']}
        self.assertEqual(listing['1100']['balance_cents'],8500)
        self.assertEqual(listing['2000']['normal_balance_cents'],27000)
        self.assertEqual(self.book.contact_center('customer','2026-01-06')['rows'][0]['open_balance_cents'],18500)
        center = self.book.contact_center('customer','2026-01-31',self.customer)
        self.assertEqual(center['rows'][0]['open_balance_cents'],8500)
        self.assertEqual(center['documents'][0]['id'],invoice['id'])
        self.assertEqual(center['payments'][0]['amount_cents'],10000)
        self.assertEqual(self.book.contact_center('vendor','2026-01-31')['documents'][0]['id'],bill['id'])

    def test_service_quantity_and_rate_are_calculated_and_retained_without_stock(self):
        invoice = self.book.create_invoice('2026-01-02',self.customer,[
            {'description':'Consulting hours','account':'4000','qty':'1.5','unit_price_cents':1234},
        ])
        self.assertEqual(invoice['total_cents'],1851)
        self.assertEqual(invoice['lines'][0]['qty'],'1.5')
        self.assertEqual(invoice['lines'][0]['unit_price_cents'],1234)
        self.assertIsNone(invoice['lines'][0]['item_id'])
        self.assertEqual(self.book.inventory_report()['total_value_cents'],0)
        before = self.book.list_data()
        with self.assertRaises(ValueError):
            self.book.create_bill('2026-01-03',self.vendor,[
                {'account':'1200','qty':'1','unit_cost_cents':100},
            ])
        with self.assertRaises(ValueError):
            self.book.create_invoice('2026-01-03',self.customer,[
                {'qty':'1','unit_cost_cents':100},
            ])
        with self.assertRaises(ValueError):
            self.book.create_bill('2026-01-03',self.vendor,[
                {'item_id':self.item,'qty':'1','unit_cost_cents':100},
                {'account':'1200','amount_cents':100},
            ])
        self.assertEqual(self.book.list_data(),before)

    def test_contact_details_and_document_memos_persist_and_snapshot_original_contact(self):
        details = dict(company_name='Example Company',first_name='Ada',last_name='Chen',
                       phone='+1 555 0100',email='ada@example.test',address='100 Example Street\nExample City')
        contact = self.book.create_contact('Example Customer','customer',**details)
        invoice = self.book.create_invoice('2026-01-02',contact['id'],[{'amount_cents':12345}],memo='Invoice header note',external_id='memo-invoice')
        bill = self.book.create_bill('2026-01-02',self.vendor,[{'amount_cents':2345}],memo='Bill header note',external_id='memo-bill')
        self.assertEqual(invoice['memo'],'Invoice header note')
        self.assertEqual(bill['memo'],'Bill header note')
        self.assertEqual(invoice['contact_snapshot']['address'],details['address'])
        financial = self.book.trial_balance('2026-01-31')
        changed = self.book.update_contact_details(contact['id'],name='Renamed Customer',phone='',address='200 Changed Street',external_id='contact-edit')
        self.assertEqual(changed['name'],'Renamed Customer')
        self.assertEqual(changed['kind'],'customer')
        self.assertEqual(changed['phone'],'')
        self.assertEqual(changed['company_name'],'Example Company')
        self.assertEqual(self.book.trial_balance('2026-01-31'),financial)
        self.assertEqual(self.book.get_invoice(invoice['id'])['contact_snapshot']['name'],'Example Customer')
        self.assertEqual(self.book.get_invoice(invoice['id'])['contact_snapshot']['address'],details['address'])
        self.assertTrue(any('Invoice header note' in row['memo'] for row in self.book.list_data()['journals']))
        reopened = Ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_contact(contact['id'])['address'],'200 Changed Street')
        self.assertEqual(reopened.get_contact(contact['id'])['email'],'ada@example.test')
        self.assertEqual(reopened.get_invoice(invoice['id'])['memo'],'Invoice header note')
        self.assertEqual(reopened.get_bill(bill['id'])['memo'],'Bill header note')
        center = reopened.contact_center('customer','2026-01-31',contact['id'])
        self.assertEqual(center['rows'][0]['first_name'],'Ada')
        self.assertEqual(center['rows'][0]['open_balance_cents'],12345)
        backup = reopened.backup(Path(self.directory.name)/'details-backup.sqlite')
        restored = Ledger(backup['path'],readonly=True)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get_contact(contact['id']),reopened.get_contact(contact['id']))
        self.assertEqual(restored.get_invoice(invoice['id'])['memo'],'Invoice header note')

    def test_invalid_contact_details_memo_and_failed_invoice_leave_no_partial_metadata(self):
        before = self.book.list_data()
        audit = self.book.audit_log()
        with self.assertRaises(ValueError):
            self.book.update_contact_details(self.customer,phone=123)
        with self.assertRaises(ValueError):
            self.book.create_contact('Invalid','customer',address='x'*2001)
        with self.assertRaises(ValueError):
            self.book.create_invoice('2026-01-02',self.customer,[{'amount_cents':1}],memo='x'*1001)
        with self.assertRaises(ValueError):
            self.book.create_invoice('2026-01-02',self.customer,[{'item_id':self.item,'qty':'99','unit_price_cents':100}],memo='must not leak')
        self.assertEqual(self.book.list_data(),before)
        self.assertEqual(self.book.audit_log(),audit)


if __name__=='__main__':
    unittest.main()
