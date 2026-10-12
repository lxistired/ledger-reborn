"""Public synthetic HTTP workflows; no vendor database or original report files."""
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from ledger import Ledger


class BackendHTTP(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        spec = importlib.util.spec_from_file_location('backend_http_server',BASE/'server.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.data_dir = Path(self.directory.name)/'user-data'
        self.http = self.module.create_server(port=0,data_dir=self.data_dir)
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self,method,path,payload=None):
        client = http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        headers = {'Host':f'127.0.0.1:{self.port}'}
        if payload is not None:
            headers.update({'Content-Type':'application/json','Origin':f'http://127.0.0.1:{self.port}'})
        client.request(method,path,json.dumps(payload).encode() if payload is not None else None,headers)
        response = client.getresponse()
        status,content_type = response.status,response.getheader('Content-Type','')
        body = response.read()
        client.close()
        return status,json.loads(body) if 'json' in content_type else body

    def action(self,action,payload,book='company'):
        code,result = self.request('POST','/api/action?book='+book,{'action':action,'payload':payload})
        self.assertEqual(code,200,result)
        return result['result']

    def test_missing_reference_is_explicitly_unavailable_and_company_stays_default(self):
        code,state = self.request('GET','/api/state')
        self.assertEqual(code,200,state)
        self.assertEqual(state['book'],'company')
        self.assertEqual(state['available_books'],['company','demo'])
        code,missing = self.request('GET','/api/state?book=reference')
        self.assertEqual(code,404,missing)
        self.assertEqual(missing['code'],'reference_unavailable')
        self.assertFalse(self.http.books['reference'].exists())

    def test_reference_post_is_rejected_and_cannot_write_company_or_user_data(self):
        self.request('GET','/api/state')
        before = (self.data_dir/'company.sqlite').read_bytes()
        for action in ('create_contact','post_journal','allocate_payments','load_demo'):
            code,data = self.request('POST','/api/action?book=reference',{'action':action,'payload':{}})
            self.assertEqual(code,403,data)
        self.assertEqual((self.data_dir/'company.sqlite').read_bytes(),before)
        self.assertFalse((self.data_dir/'reference.sqlite').exists())
        code,state = self.request('GET','/api/state')
        self.assertEqual(state['data']['contacts'],[])
        self.assertEqual(state['data']['journals'],[])

    def test_data_directory_factory_is_isolated_and_explicit_argument_overrides_env(self):
        override = Path(self.directory.name)/'env-data'
        explicit = Path(self.directory.name)/'explicit-data'
        with patch.dict('os.environ',{'LEDGER_REBORN_DATA_DIR':str(override)}):
            self.assertEqual(self.module.default_data_dir(),override.resolve())
            other = self.module.create_server(port=0,data_dir=explicit)
        try:
            self.assertEqual(other.data_dir,explicit.resolve())
            self.assertEqual(other.server_address[0],'127.0.0.1')
            self.assertNotEqual(other.server_port,self.port)
            self.action('create_contact',{'name':'Isolated customer','kind':'customer'})
            independent = Ledger(other.books['company'])
            try:
                self.assertEqual(independent.list_data()['contacts'],[])
            finally:
                independent.close()
            self.assertTrue((self.data_dir/'company.sqlite').exists())
            self.assertTrue((explicit/'company.sqlite').exists())
            self.assertFalse(override.exists())
            self.assertNotEqual(self.data_dir,self.module.BASE)
        finally:
            other.server_close()

    def test_frozen_application_resources_and_executable_directory_cannot_hold_user_data(self):
        executable = Path(self.directory.name)/'installed'/'Ledger Reborn.exe'
        with patch.object(self.module.sys,'frozen',True,create=True), patch.object(self.module.sys,'executable',str(executable)):
            with self.assertRaises(ValueError):
                self.module.validate_data_dir(executable.parent/'data')
            with self.assertRaises(ValueError):
                self.module.validate_data_dir(self.module.BASE/'data')
            self.assertEqual(self.module.validate_data_dir(self.data_dir),self.data_dir.resolve())





    def test_mixed_item_invoice_and_batch_payment_are_available_through_allowlisted_api(self):
        customer = self.action('create_contact',{'name':'Customer','kind':'customer'})['id']
        vendor = self.action('create_contact',{'name':'Vendor','kind':'vendor'})['id']
        item = self.action('create_item',{'sku':'W','name':'Widget'})['id']
        bill = self.action('create_bill',{'date':'2026-01-01','contact_id':vendor,'lines':[
            {'item_id':item,'qty':'5','unit_cost_cents':1000},
            {'description':'Office','account':'6300','amount_cents':200},
        ]})
        invoice = self.action('create_invoice',{'date':'2026-01-02','contact_id':customer,'lines':[
            {'item_id':item,'qty':'2','unit_price_cents':2000},
            {'description':'Service','account':'4000','amount_cents':500},
        ]})
        second = self.action('create_invoice',{'date':'2026-01-02','contact_id':customer,'lines':[{'amount_cents':1000}]})
        receipt = self.action('allocate_payments',{'date':'2026-01-03','contact_id':customer,'allocations':[
            {'document_id':invoice['id'],'amount_cents':2000},
            {'document_id':second['id'],'amount_cents':500},
        ],'external_id':'receipt-001'})
        self.assertEqual(receipt['total_cents'],2500)
        code,result = self.request('GET',f'/api/report?name=get_invoice&invoice_id={invoice["id"]}&as_of=2026-01-31')
        self.assertEqual(code,200,result)
        self.assertEqual(result['result']['outstanding_cents'],2500)
        self.assertEqual(result['result']['lines'][0]['unit_price_cents'],2000)
        code,center = self.request('GET',f'/api/report?name=contact_center&kind=customer&contact_id={customer}&as_of=2026-01-31')
        self.assertEqual(code,200,center)
        self.assertEqual(center['result']['rows'][0]['open_balance_cents'],3000)
        code,gl = self.request('GET','/api/report?name=general_ledger&start_date=2026-01-01&end_date=2026-01-31&account=1100')
        self.assertEqual(code,200,gl)
        self.assertEqual(gl['result']['accounts'][0]['closing_balance_cents'],3000)
        second_vendor = self.action('create_contact',{'name':'Second Vendor','kind':'vendor'})['id']
        second_bill = self.action('create_bill',{'date':'2026-01-02','contact_id':second_vendor,'lines':[{'amount_cents':700}]})
        payment = self.action('pay_bills',{'date':'2026-01-04','allocations':[
            {'document_id':bill['id'],'amount_cents':1000},
            {'document_id':second_bill['id'],'amount_cents':500},
        ]})
        self.assertEqual(payment['total_cents'],1500)
        self.assertEqual(payment['vendor_ids'],[vendor,second_vendor])

    def test_contact_details_edit_and_invoice_bill_memos_save_and_read_back_across_http_connections(self):
        details = {'name':'Example Customer','kind':'customer','company_name':'Example Company',
                   'first_name':'Ada','last_name':'Chen','phone':'+1 555 0100','email':'ada@example.test',
                   'address':'100 Example Street\nExample City'}
        customer = self.action('create_contact',details)
        vendor = self.action('create_contact',{'name':'Example Vendor','kind':'vendor','address':'Warehouse Road'})
        self.assertEqual(customer['address'],details['address'])
        invoice = self.action('create_invoice',{'date':'2026-01-02','contact_id':customer['id'],
            'lines':[{'description':'Service','amount_cents':12345}],'memo':'Invoice header saved note'})
        bill = self.action('create_bill',{'date':'2026-01-02','contact_id':vendor['id'],
            'lines':[{'description':'Supply','amount_cents':2345}],'memo':'Bill header saved note'})
        edit = self.action('update_contact_details',{'contact_id':customer['id'],'name':'Renamed Customer',
             'address':'200 Changed Street','phone':'','external_id':'customer-edit-001'})
        self.assertEqual(edit['name'],'Renamed Customer')
        self.assertEqual(edit['phone'],'')
        code,contact = self.request('GET',f'/api/report?name=get_contact&contact_id={customer["id"]}')
        self.assertEqual(code,200,contact)
        self.assertEqual(contact['result']['address'],'200 Changed Street')
        self.assertEqual(contact['result']['company_name'],'Example Company')
        self.assertEqual(contact['result']['kind'],'customer')
        for report,document,note in (('get_invoice',invoice,'Invoice header saved note'),('get_bill',bill,'Bill header saved note')):
            key = 'invoice_id' if report=='get_invoice' else 'bill_id'
            code,readback = self.request('GET',f'/api/report?name={report}&{key}={document["id"]}')
            self.assertEqual(code,200,readback)
            self.assertEqual(readback['result']['memo'],note)
            self.assertEqual(readback['result']['total_cents'],document['total_cents'])
        code,snapshot = self.request('GET',f'/api/report?name=get_invoice&invoice_id={invoice["id"]}')
        self.assertEqual(snapshot['result']['contact_snapshot']['name'],'Example Customer')
        self.assertEqual(snapshot['result']['contact_snapshot']['address'],details['address'])
        code,state = self.request('GET','/api/state')
        self.assertEqual(code,200,state)
        self.assertEqual(next(row for row in state['data']['contacts'] if row['id']==customer['id'])['email'],'ada@example.test')
        code,bad = self.request('POST','/api/action',{'action':'update_contact_details','payload':{'contact_id':customer['id'],'kind':'vendor'}})
        self.assertEqual(code,400,bad)


if __name__=='__main__':
    unittest.main()
