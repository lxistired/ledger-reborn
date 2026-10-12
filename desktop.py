#!/usr/bin/env python3
"""Ledger Reborn native desktop window with an isolated local HTTP service."""
from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import platform
import socket
import sys
import tempfile
import threading
import traceback

import server

PRODUCT_NAME = 'Ledger Reborn'
PROJECT_NUMBER = '000001'
VERSION = '2026.10.12-preview.1'
BUNDLE_IDENTIFIER = 'io.github.lxistired.ledgerreborn'


class LocalService:
    def __init__(self,data_dir=None):
        self.http = server.create_server(port=0,data_dir=data_dir)
        self.thread = threading.Thread(target=self.http.serve_forever,kwargs={'poll_interval':0.05},
                                       name='LedgerRebornLocalServer',daemon=True)
        self.lock = threading.Lock()
        self.stopped = False

    @property
    def port(self):
        return self.http.server_port

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/'

    @property
    def data_dir(self):
        return self.http.data_dir

    def start(self):
        self.thread.start()
        return self

    def stop(self,*args):
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if self.thread.is_alive():
                self.http.shutdown()
            self.http.server_close()
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('Local HTTP service did not stop cleanly')


def smoke_test(report_path=None):
    """No GUI and no real company data; run the bundled application's actual API."""
    checks = []
    with tempfile.TemporaryDirectory(prefix='ledger-reborn-frozen-smoke-') as temporary:
        service = LocalService(Path(temporary)/'data').start()
        data_dir = service.data_dir
        port = service.port
        def request(method,path,payload=None,origin=None,host=None):
            connection = http.client.HTTPConnection('127.0.0.1',port,timeout=10)
            headers = {'Host':host or f'127.0.0.1:{port}'}
            body = None
            if payload is not None:
                body = json.dumps(payload).encode()
                headers.update({'Content-Type':'application/json','Origin':origin or f'http://127.0.0.1:{port}'})
            connection.request(method,path,body,headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read()
            content_type = response.getheader('Content-Type','')
            connection.close()
            return status,json.loads(raw) if 'json' in content_type else raw
        def action(name,payload):
            status,result = request('POST','/api/action',{'action':name,'payload':payload})
            if status!=200:
                raise AssertionError((name,status,result))
            return result['result']
        try:
            status,html = request('GET','/')
            assert status==200 and b'<html' in html.lower()
            for asset in ('/app.js','/style.css','/assets/app-icon.svg'):
                status,body = request('GET',asset)
                assert status==200 and body
            checks.append('bundled HTML, JavaScript, CSS and original app icon load')
            status,state = request('GET','/api/state')
            assert status==200 and state['book']=='company' and state['available_books']==['company','demo']
            assert state['data']['journals']==[]
            checks.append('empty company is default; no private reference is bundled')
            status,missing = request('GET','/api/state?book=reference')
            assert status==404 and missing['code']=='reference_unavailable'
            status,_ = request('POST','/api/action?book=reference',{'action':'create_contact','payload':{'name':'Blocked','kind':'customer'}})
            assert status==403
            checks.append('missing reference GET is unavailable and reference POST remains blocked')
            bad = {'action':'create_project','payload':{'name':'Must never be saved'}}
            status,_ = request('POST','/api/action',bad,origin='http://untrusted.example')
            assert status==400
            status,_ = request('GET','/api/state',host='untrusted.example')
            assert status==400
            checks.append('unknown Origin and rebinding Host rejected')
            contact = action('create_contact',{'name':'Frozen smoke customer','kind':'customer',
                             'address':'Synthetic test address','email':'smoke@example.test'})
            action('post_journal',{'date':'2026-01-01','memo':'Synthetic owner investment','lines':[
                {'account':'1000','debit_cents':100000},{'account':'3000','credit_cents':100000}]})
            invoice = action('create_invoice',{'date':'2026-01-02','contact_id':contact['id'],
                              'lines':[{'description':'Synthetic service','amount_cents':12345}],
                              'memo':'Frozen packaged invoice','external_id':'frozen-invoice-001'})
            action('receive_payment',{'date':'2026-01-03','invoice_id':invoice['id'],
                   'amount_cents':2345,'external_id':'frozen-receipt-001'})
            status,state = request('GET','/api/state')
            assert status==200 and state['data']['invoices'][0]['outstanding_cents']==10000
            assert state['data']['invoices'][0]['memo']=='Frozen packaged invoice'
            assert state['data']['invoices'][0]['contact_snapshot']['address']=='Synthetic test address'
            assert state['data']['projects']==[]
            checks.append('contact, memo, invoice and partial payment persist across new connections')
            status,report = request('GET','/api/report?name=trial_balance&as_of=2026-01-31')
            assert status==200 and report['result']['balanced']
            status,report = request('GET','/api/report?name=profit_loss&start_date=2026-01-01&end_date=2026-01-31')
            assert status==200 and report['result']['net_profit_cents']==12345
            status,report = request('GET','/api/report?name=balance_sheet&as_of=2026-01-31')
            assert status==200 and report['result']['balanced'] and report['result']['assets_cents']==112345
            checks.append('packaged accounting reports match independent cent expectations')
            status,backup = request('GET','/api/backup')
            assert status==200 and backup.startswith(b'SQLite format 3')
            assert (data_dir/'company.sqlite').is_file() and data_dir!=server.BASE
            checks.append('SQLite backup and data are outside read-only packaged resources')
        finally:
            service.stop()
        assert not service.thread.is_alive()
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(('127.0.0.1',port))!=0
        checks.append('local server thread and listening socket shut down cleanly')
        result = dict(product=PRODUCT_NAME,project_number=PROJECT_NUMBER,version=VERSION,all_passed=True,
                      frozen=bool(getattr(sys,'frozen',False)),platform=sys.platform,machine=platform.machine(),
                      executable=str(Path(sys.executable).resolve()),resource_root=str(server.BASE),
                      data_dir_policy='isolated TemporaryDirectory; removed after test',passed=len(checks),checks=checks)
    if report_path:
        path = Path(report_path).expanduser().resolve()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=f'{PRODUCT_NAME} — Project #{PROJECT_NUMBER}')
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--smoke-test',action='store_true',help='Run actual packaged API checks with temporary data, without a GUI')
    parser.add_argument('--smoke-report',type=Path)
    args = parser.parse_args()
    if args.smoke_test:
        try:
            smoke_test(args.smoke_report)
            return 0
        except Exception as error:
            failure = dict(product=PRODUCT_NAME,version=VERSION,all_passed=False,
                           frozen=bool(getattr(sys,'frozen',False)),error=repr(error),
                           traceback=traceback.format_exc())
            if args.smoke_report:
                args.smoke_report.parent.mkdir(parents=True,exist_ok=True)
                args.smoke_report.write_text(json.dumps(failure,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(failure,indent=2),flush=True)
            return 1
    service = LocalService(args.data_dir).start()
    try:
        import webview
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
        window = webview.create_window(PRODUCT_NAME,service.url,width=1280,height=820,
                                       min_size=(940,680),text_select=True,background_color='#f3f4f5')
        window.events.closed += service.stop
        backend = 'cocoa' if sys.platform=='darwin' else 'edgechromium' if sys.platform=='win32' else None
        webview.start(gui=backend,private_mode=False,storage_path=str(service.data_dir/'webview'))
    finally:
        service.stop()
    return 0


if __name__=='__main__':
    multiprocessing.freeze_support()
    # Windowed Windows executables have no console streams. Third-party GUI
    # libraries may still write diagnostics; give them valid streams without
    # creating files beside the installed executable.
    if sys.stdout is None:
        sys.stdout = open(os.devnull,'w',encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull,'w',encoding='utf-8')
    raise SystemExit(main())
