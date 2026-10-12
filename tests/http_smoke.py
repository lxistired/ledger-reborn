import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
spec=importlib.util.spec_from_file_location('local_server',BASE/'server.py')
server_module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)
checks=[]
with tempfile.TemporaryDirectory() as temporary:
    server_module.BOOKS={k:Path(temporary)/(k+'.sqlite') for k in ('company','demo')}
    server=server_module.ThreadingHTTPServer(('127.0.0.1',0),server_module.Handler)
    port=server.server_port
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def req(method,path,payload=None,origin=True,host=None,content_type='application/json'):
        client=http.client.HTTPConnection('127.0.0.1',port)
        headers={'Host':host or f'127.0.0.1:{port}'}
        if payload is not None:
            headers['Content-Type']=content_type
            if origin:headers['Origin']=f'http://127.0.0.1:{port}'
        client.request(method,path,json.dumps(payload,ensure_ascii=False).encode('utf-8') if payload is not None else None,headers)
        response=client.getresponse();body=response.read();code=response.status;ctype=response.getheader('Content-Type');client.close()
        return code,json.loads(body) if 'json' in ctype else body
    def post(action,payload,book='company'):
        code,data=req('POST','/api/action?book='+book,{'action':action,'payload':payload})
        assert code==200,(action,code,data)
        return data['result']
    code,blank=req('GET','/api/state')
    assert code==200 and len(blank['data']['journals'])==0
    customer=post('create_contact',{'name':'本地测试客户 <script>alert(1)</script>','kind':'customer'})
    project=post('create_project',{'name':'本地API核验项目'})
    post('post_journal',{'date':'2026-10-01','memo':'期初投入','lines':[{'account':'1000','debit_cents':100000,'credit_cents':0},{'account':'3000','debit_cents':0,'credit_cents':100000}]})
    inv=post('create_invoice',{'date':'2026-10-02','due_date':'2026-10-10','contact_id':customer['id'],'project_id':project['id'],'lines':[{'description':'真实记账功能验收','amount_cents':12345,'account':'4000'}]})
    post('receive_payment',{'date':'2026-10-03','invoice_id':inv['id'],'amount_cents':2345,'bank_account':'1000'})
    code,data=req('GET','/api/state')
    assert data['data']['invoices'][0]['outstanding_cents']==10000
    # Every HTTP request opened a new Ledger and closed it; a new state read is persistence proof.
    checks.append('contact/journal/invoice/partial payment persisted across fresh request connections')
    code,pnl=req('GET','/api/report?name=profit_loss&start_date=2026-10-01&end_date=2026-10-31&basis=accrual')
    assert code==200 and pnl['result']['income_cents']==12345
    checks.append('actual accrual report reads posted invoice amount')
    for name,query in [('balance_sheet','as_of=2026-10-31'),('trial_balance','as_of=2026-10-31'),('ar_aging','as_of=2026-10-31'),('ap_aging','as_of=2026-10-31'),('project_profitability','start=2026-10-01&end=2026-10-31'),('budget_vs_actual','period=2026-10'),('inventory_report','as_of=2026-10-31'),('audit_log',''),('bank_register','bank_account=1000')]:
        code,result=req('GET','/api/report?name='+name+'&'+query)
        assert code==200,(name,result)
    checks.append('all nine report routes return actual core output')
    code,backup=req('GET','/api/backup')
    assert code==200 and backup.startswith(b'SQLite format 3')
    code,export=req('GET','/api/export')
    assert code==200 and len(export['data']['invoices'])==1
    checks.append('SQLite backup and JSON export include persistent ledger')
    bads=[('arbitrary method',{'action':'_initialize','payload':{}},True,None,'application/json'),('missing Origin',{'action':'create_project','payload':{'name':'unauthorized'}},False,None,'application/json'),('DNS rebinding Host',{'action':'create_project','payload':{'name':'unauthorized'}},True,'attacker.example','application/json'),('non JSON',{'action':'create_project','payload':{'name':'unauthorized'}},True,None,'text/plain'),('file path bank CSV',{'action':'import_bank_csv','payload':{'contents':'/etc/passwd'}},True,None,'application/json'),('unbalanced journal',{'action':'post_journal','payload':{'date':'2026-10-05','memo':'bad','lines':[{'account':'1000','debit_cents':100,'credit_cents':0},{'account':'3000','debit_cents':0,'credit_cents':99}]}},True,None,'application/json'),('internal invoice line fields',{'action':'create_invoice','payload':{'date':'2026-10-05','contact_id':customer['id'],'lines':[{'description':'bad','amount_cents':100,'item_id':999}]}},True,None,'application/json')]
    for label,payload,origin,host,ctype in bads:
        code,data=req('POST','/api/action',payload,origin,host,ctype)
        assert code==400,(label,code,data)
        assert any('\u4e00'<=c<='\u9fff' for c in data['error'])
        checks.append('rejected: '+label)
    post('load_demo',{},'demo')
    code,demo=req('GET','/api/state?book=demo')
    assert code==200 and len(demo['data']['journals'])==len(json.loads((BASE/'fixtures/scenario.json').read_text())['events']) and len(demo['data']['bank_transactions'])==19
    code,company=req('GET','/api/state')
    assert len(company['data']['journals'])==3
    checks.append('synthetic fixture/19-bank-row demo is isolated from company data')
    server.shutdown();server.server_close()
(BASE / 'tests/http_smoke_result.json').write_text(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False,indent=2))
print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False,indent=2))
