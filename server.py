#!/usr/bin/env python3
"""Local, persistent accounting workbench. Standard-library HTTP + Ledger.

All state changes require same-origin JSON POST. No remote network binding,
method dispatch from arbitrary names, arbitrary file access or cloud services.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ledger import Ledger

BASE = Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent)).resolve()
WEB = (BASE / "web").resolve()


def default_data_dir():
    override = os.environ.get('LEDGER_REBORN_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform=='darwin':
        return Path.home()/'Library'/'Application Support'/'Ledger Reborn'
    if sys.platform=='win32':
        return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))/'Ledger Reborn'
    return Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local'/'share')))/'Ledger Reborn'


def validate_data_dir(path):
    target = Path(path).expanduser().resolve()
    protected = []
    if getattr(sys,'frozen',False):
        protected.append(BASE)
        protected.append(Path(sys.executable).resolve().parent)
        protected.extend(parent for parent in Path(sys.executable).resolve().parents if parent.suffix.lower()=='.app')
    if sys.platform=='win32':
        protected.extend(Path(value).resolve() for key in ('PROGRAMFILES','PROGRAMFILES(X86)') if (value:=os.environ.get(key)))
    for root in protected:
        if target==root or root in target.parents:
            raise ValueError('Data directory must be outside the installed application and Program Files.')
    return target


DATA = default_data_dir()
MAX_BODY = 2 * 1024 * 1024
BOOKS = {"company": DATA / "company.sqlite", "demo": DATA / "demo.sqlite"}
BOOKS['reference'] = BASE / 'reference' / 'reconstructed_qb_gl.sqlite3'
READ_ONLY_BOOKS = frozenset({'reference'})
LOCKS = {key: threading.RLock() for key in BOOKS}

# Explicit public API allowlist, including allowed field names.
ACTIONS = {
    "create_account": {"code", "name", "kind"},
    "create_contact": {"name", "kind", "company_name", "first_name", "last_name", "phone", "email", "address"},
    "update_contact_details": {"contact_id", "name", "company_name", "first_name", "last_name", "phone", "email", "address", "external_id"},
    "create_project": {"name"},
    "create_item": {"sku", "name", "inventory_account", "cogs_account", "income_account"},
    "post_journal": {"date", "memo", "lines", "external_id"},
    "void_journal": {"entry_id", "date", "memo", "external_id"},
    "create_invoice": {"date", "contact_id", "lines", "due_date", "project_id", "external_id", "memo"},
    "create_bill": {"date", "contact_id", "lines", "due_date", "project_id", "external_id", "memo"},
    "receive_payment": {"date", "invoice_id", "amount_cents", "bank_account", "external_id"},
    "pay_bill": {"date", "bill_id", "amount_cents", "bank_account", "external_id"},
    "allocate_payments": {"date", "contact_id", "allocations", "bank_account", "external_id"},
    "pay_bills": {"date", "allocations", "bank_account", "external_id"},
    "buy_inventory": {"date", "item_id", "qty", "unit_cost_cents", "offset_account", "contact_id", "external_id"},
    "sell_inventory": {"date", "item_id", "qty", "unit_price_cents", "contact_id", "project_id", "external_id"},
    "import_bank_csv": {"contents", "bank_account"},
    "classify_bank_transaction": {"transaction_id", "account", "project_id", "memo"},
    "match_bank_transaction": {"transaction_id", "entry_line_id"},
    "reconcile": {"bank_account", "statement_date", "ending_balance_cents", "entry_line_ids"},
    "set_budget": {"period", "account", "amount_cents", "project_id"},
    "close_period": {"end_date"},
}
REPORTS = {
    "profit_loss": {"start_date", "end_date", "basis", "project_id"},
    "balance_sheet": {"as_of", "basis", "retained_account"},
    "trial_balance": {"as_of", "fiscal_year_start", "retained_account"},
    "ar_aging": {"as_of"},
    "ap_aging": {"as_of"},
    "inventory_report": {"as_of"},
    "audit_log": set(),
    "project_profitability": {"start", "end"},
    "budget_vs_actual": {"period", "project_id"},
    "bank_register": {"bank_account", "as_of"},
    "general_ledger": {"start_date", "end_date", "account"},
    "account_listing": {"as_of"},
    "contact_center": {"kind", "as_of", "contact_id"},
    "get_invoice": {"invoice_id", "as_of"},
    "get_bill": {"bill_id", "as_of"},
    "get_contact": {"contact_id"},
}


def encode(data):
    return json.dumps(data, ensure_ascii=False, default=lambda x: str(x) if isinstance(x, (Decimal, Path, date)) else repr(x)).encode("utf-8")



ERRORS = {
    "date must be YYYY-MM-DD": "日期格式必须为 YYYY-MM-DD。", "invalid calendar date": "日期不存在。",
    "money must be integer cents; floats are not accepted": "金额必须使用整数分。", "amount exceeds supported range": "金额超出范围。", "amount must be positive": "金额必须大于零。",
    "quantity supports at most six decimal places": "数量最多支持六位小数。", "quantity must be finite, positive, and at most 1e9": "数量必须大于零且不超过十亿。",
    "unsupported account kind": "科目类别无效。", "account code already exists": "科目编号已存在。", "project name already exists": "项目名称已存在。", "sku already exists": "SKU 已存在。",
    "unknown inventory item": "库存商品不存在。", "unknown journal entry": "凭证不存在。", "journal requires at least two lines": "凭证至少需要两行分录。",
    "each journal line must have one positive debit or credit": "每行应仅填写一个大于零的借方或贷方金额。", "journal debits and credits do not balance": "借贷不平衡，凭证没有保存。",
    "use inventory purchase/sale APIs; direct inventory account entries would bypass item quantities": "库存应使用库存进销页面录入，不能用手工分录绕过数量。",
    "document, payment, bank-import, and inventory entries cannot be voided independently": "业务单据、支付、银行分类和库存联动凭证不能单独冲销。",
    "reversal cannot precede original entry": "冲销日期不能早于原凭证日期。", "journal has already been reversed": "该凭证已经冲销。", "due date cannot precede document date": "到期日不能早于单据日期。",
    "document requires at least one line": "单据至少需要一行。", "use buy_inventory/sell_inventory for inventory document lines": "库存商品请使用库存进销页面。", "use buy_inventory for inventory bills": "库存采购请使用库存进销页面。",
    "unknown document": "单据不存在。", "document is not an invoice": "所选单据不是发票。", "document is not a bill": "所选单据不是供应商账单。", "payment cannot precede document date": "支付日期不能早于单据日期。", "payment exceeds outstanding amount": "金额超过单据的未结余额。",
    "bank account cannot be AR or Inventory": "银行科目不能使用应收账款或库存科目。", "invalid inventory purchase offset account": "采购贷记科目应为银行或应付账款。", "negative inventory is not supported": "库存不足，销售未保存。",
    "CSV is empty": "CSV 文件为空。", "amount_cents must be an integer string": "amount_cents 列必须填写整数分。", "amount must have at most two decimal places": "amount 列最多支持两位小数。", "CSV requires amount_cents or amount": "CSV 缺少 amount 或 amount_cents 列。", "bank amount must be nonzero": "银行金额不能为零。",
    "bank external_id already used with different transaction data": "该外部流水编号已用于不同的交易。", "external_id already used with a different request": "该外部编号已用于不同的操作。",
    "unknown bank transaction": "银行流水不存在。", "bank transaction already classified or matched": "该流水已经分类或匹配。", "counter account must differ from bank": "分类科目不能与银行科目相同。", "bank transaction already matched": "该流水已经匹配。",
    "bank transaction account or amount does not match journal line": "流水与原分录的科目或金额不一致。", "journal date cannot be later than bank date": "原分录日期不能晚于银行流水日期。", "journal line already matched to another bank transaction": "该分录已匹配另一条流水。",
    "entry_line_ids must be a list without duplicates": "待对账分录不能重复。", "statement dates must increase; previous reconciliations cannot be replaced": "本次账单日期必须晚于上次，不能覆盖历史对账。", "selected line is not an eligible bank entry for this statement": "勾选的分录不属于该银行或发生在账单日期之后。", "selected bank entry was already reconciled": "该银行分录已经对账。",
    "budget period must be YYYY-MM": "预算月份应为 YYYY-MM。", "budget amount must be nonnegative in the normal account direction": "预算金额不能为负数。", "close date cannot move backwards": "关账日期不能向前回退。", "basis must be accrual or cash": "报表口径无效。", "report start must not exceed end": "开始日期不能晚于结束日期。", "monthly budgets require whole calendar months": "月度预算报告需要完整自然月。",
}

def error_text(error):
    message = str(error)
    if message in ERRORS:
        return ERRORS[message]
    if message.startswith("CSV line "):
        prefix, detail = message.split(": ", 1)
        return "CSV 第 " + prefix[9:] + " 行：" + ERRORS.get(detail, "请检查日期、金额和文本格式。")
    if message.startswith("accounting period is closed through "):
        return "此期间已关账至 " + message.rsplit(" ", 1)[-1] + "，无法录入或冲销。"
    if message.startswith("reconciliation difference is "):
        return "对账差额为 " + message.split(" ")[3] + " 分；请检查账单余额与勾选项目。"
    if message.startswith("inventory backdating is unsupported"):
        return "库存不支持回填更早日期，最新库存移动日期为 " + message.rsplit(" ", 1)[-1] + "。"
    if message.startswith("unknown account: "):
        return "科目不存在：" + message.split(": ", 1)[-1]
    if message.startswith("account ") and "must have kind" in message:
        return "所选科目的类别不符合该业务要求。"
    if message.startswith("contact must be a "):
        return "请建立并选择对应类别的客户或供应商。"
    if "must be a nonempty string" in message:
        return "文本不能为空，或超过允许长度。"
    if "must be a positive integer id" in message:
        return "请选择有效记录。"
    if isinstance(error, TypeError):
        return "操作参数缺失或格式错误。"
    if any("\u4e00" <= character <= "\u9fff" for character in message):
        return message
    return "请检查输入数据、日期、数量和金额。"


def close_book(book):
    if hasattr(book, "close"):
        book.close()
    elif hasattr(book, "conn"):
        book.conn.close()


def open_book(company,books=None):
    return Ledger((books or BOOKS)[company], readonly=company in READ_ONLY_BOOKS)


def reference_available(books=None):
    books = books or BOOKS
    required = ('parsed_gl_transactions.json','classified_gl_accounts.json','original_report_comparison.json',
                'sources/qb_original_pl_2023_rows.json','sources/qb_original_bs_20231231_rows.json')
    return bool('reference' in books and books['reference'].is_file() and all((BASE/'reference'/name).is_file() for name in required))


def book_scope(company):
    return 'reconstructed_gl_only' if company in READ_ONLY_BOOKS else 'local_company'


def reference_general_ledger(book, start_date, end_date, account=None):
    """Original parsed GL rows, with explicit source lineage and carry balance.

    The reference database preserves accounting balances as daily journals;
    this separate fixed source file preserves the original export's row types,
    numbers, names, split fields and dates. No invoice/payment masters are
    synthesized from it.
    """
    posted = book.general_ledger(start_date,end_date,account)
    transactions = json.loads((BASE/'reference'/'parsed_gl_transactions.json').read_text(encoding='utf-8'))
    by_account = {}
    for transaction in transactions:
        code = transaction.get('canonical_account_code',transaction['account_code'])
        if start_date <= transaction['date'] <= end_date and (account is None or str(account)==code):
            by_account.setdefault(code,[]).append(transaction)
    groups, rows = [], []
    for posted_group in posted['accounts']:
        code = posted_group['code']
        source_rows = sorted(by_account.get(code,[]),key=lambda row:(row['date'],row['source_row']))
        running = posted_group['opening_balance_cents']
        detail = []
        for transaction in source_rows:
            amount = transaction['debit_cents']-transaction['credit_cents']
            running += amount
            row = dict(transaction,account=code,account_code=code,
                       line_id=None,entry_id=None,running_balance_cents=running,balance_cents=running,
                       amount_cents=amount,source='quickbooks_original_gl_export')
            detail.append(row)
        group = dict(code=code,account=code,name=posted_group['name'],kind=posted_group['kind'],
                     opening_balance_cents=posted_group['opening_balance_cents'],closing_balance_cents=posted_group['closing_balance_cents'],
                     total_debit_cents=sum(row['debit_cents'] for row in detail),
                     total_credit_cents=sum(row['credit_cents'] for row in detail),
                     difference_cents=running-posted_group['closing_balance_cents'],rows=detail)
        groups.append(group)
        rows.extend(detail)
    return dict(start_date=start_date,end_date=end_date,account=account,rows=rows,accounts=groups,
                total_debit_cents=sum(row['debit_cents'] for row in rows),total_credit_cents=sum(row['credit_cents'] for row in rows),
                source_kind='quickbooks_original_gl_export',accounting_basis='accrual',read_only=True,
                data_scope='reconstructed_gl_only',source_path='reference/parsed_gl_transactions.json',
                documents_reconstructed=False)


TYPE_ORDER = {name:index for index,name in enumerate((
    'Bank','Accounts Receivable','Other Current Asset','Fixed Asset','Other Asset',
    'Accounts Payable','Credit Card','Other Current Liability','Long Term Liability',
    'Equity','Income','Cost of Goods Sold','Expense','Other Income','Other Expense'))}


def row_is_active(row):
    if 'debit_cents' in row:
        return bool(row['debit_cents'] or row['credit_cents'])
    return bool(row.get('amount_cents',0))


def label_metadata(row):
    """Whitelist label/order fields only; source financial values are ignored."""
    return {key:row[key] for key in ('source_row','account_code','account_path','official_type','kind') if key in row}


def display_tree(presentation_rows):
    """Hierarchy from source paths; every numeric aggregate uses computed rows.

    Native source_row is exact for leaves. A generated parent has no invented
    header row: native_first_leaf_row identifies its ordering provenance.
    """
    roots, by_path = [], {}
    for row in presentation_rows:
        path = row.get('account_path') or [row['display_name']]
        parent = None
        for index,label in enumerate(path):
            key = tuple(path[:index+1])
            if key not in by_path:
                node = dict(node_id=':'.join(key),label=label,account_path=list(key),depth=index,children=[])
                by_path[key] = node
                (roots if parent is None else parent['children']).append(node)
            node = by_path[key]
            parent = node
        node['_computed_row'] = row
    def finish(node):
        computed = node.pop('_computed_row',None)
        for child in node['children']:
            finish(child)
        if computed is not None:
            node.update(node_type='account' if not node['children'] else 'group_with_posting',
                        account_code=computed['code'],source_row=computed.get('source_row'),
                        official_type=computed.get('official_type'),kind=computed.get('kind'))
        else:
            node.update(node_type='group',source_row=None)
        leaf_rows = [value for value in [computed.get('source_row') if computed else None]+
                     [child.get('native_first_leaf_row') for child in node['children']] if value is not None]
        node['native_first_leaf_row'] = min(leaf_rows) if leaf_rows else None
        for field in ('amount_cents','debit_cents','credit_cents','balance_cents'):
            if (computed and field in computed) or any(field in child for child in node['children']):
                node[field] = (computed.get(field,0) if computed else 0)+sum(child.get(field,0) for child in node['children'])
    for root in roots:
        finish(root)
    return roots


def add_reference_statement_layout(name, result, comparison):
    """Original statement structure with exclusively live-derived money.

    Only label cells A-F and the text caption G4 are consumed from source JSON.
    Source account amounts/formula caches in G5 onward are never displayed or
    used in a computation. Subtotals sum leaf values inside matched headers.
    """
    filename = 'qb_original_pl_2023_rows.json' if name=='profit_loss' else 'qb_original_bs_20231231_rows.json'
    source = json.loads((BASE/'reference'/'sources'/filename).read_text(encoding='utf-8'))
    labels, header_rows, caption = [], [], ''
    for record in source:
        cells = sorted([(key,value) for key,value in record.items()
                        if re.fullmatch(r'[A-F]\d+',key) and isinstance(value,str) and value.strip()],
                       key=lambda pair:pair[0])
        if 'G4' in record and isinstance(record['G4'],str):
            caption = record['G4']
        if cells:
            key,text = cells[0]
            number = int(key[1:])
            if number<=4:
                header_rows.append(dict(source_row=number,source_column=key[0],label=text,
                    level=ord(key[0])-ord('A'),role='header',kind='header',amount_cents=None))
            else:
                labels.append(dict(source_row=number,source_column=key[0],label=text,
                                   level=ord(key[0])-ord('A')))
        elif 'G4' in record:
            header_rows.append(dict(source_row=4,source_column='G',label=caption,level=0,
                                    role='header',kind='header',amount_cents=None))
    computed = result.get('display_rows',result['rows']) if name=='balance_sheet' else result['rows']
    by_code = {row['code']:row for row in computed}
    code_by_row = {row['source_row']:row['account_code'] for row in comparison['leaf_rows']}
    output, open_headers, totals, errors = [], [], {}, []
    for index, label in enumerate(labels):
        text,level = label['label'],label['level']
        row = dict(label,role='header',kind='header',amount_cents=None,account_code=None,account_kind=None,drilldown=False)
        financial = None
        if name=='profit_loss':
            formulas = {
                'Gross Profit':('Total Income','Total COGS','subtract'),
                'Net Ordinary Income':('Gross Profit','Total Expense','subtract'),
                'Net Other Income':('Total Other Income','Total Other Expense','subtract'),
                'Net Income':('Net Ordinary Income','Net Other Income','add'),
            }
            if text in formulas:
                left,right,operator = formulas[text]
                # An absent Other Expense section means no leaf was presented;
                # completeness checks below prevent hiding a nonzero account.
                right_value = totals.get(right,0) if right=='Total Other Expense' else totals.get(right)
                left_value = totals.get(left)
                if left_value is None or right_value is None:
                    errors.append(dict(source_row=row['source_row'],error='missing live subtotal for financial formula'))
                else:
                    financial = left_value+right_value if operator=='add' else left_value-right_value
                    row['formula'] = f'{left} {"+" if operator=="add" else "-"} {right}'
        else:
            if text=='TOTAL ASSETS':
                financial = result['assets_cents']
            elif text=='TOTAL LIABILITIES & EQUITY':
                financial = result['liabilities_cents']+result['total_equity_cents']
            elif text=='Net Income':
                income = by_code['__current_year_profit__']
                row.update(role='leaf',kind='leaf',amount_cents=income['amount_cents'],account_kind='equity',
                           computed_account_code='__current_year_profit__',amount_source='computed_live_ledger')
        if financial is not None:
            row.update(role='financial_total',kind='financial_total',amount_cents=financial,
                       amount_source='computed_subtotal_formula' if name=='profit_loss' else 'computed_report_total')
        elif row['role']=='leaf':
            pass
        elif text.startswith('Total '):
            target = text[6:]
            if target=='COGS':
                target = 'Cost of Goods Sold'
            header = next((header for header in reversed(open_headers) if header['label']==target),None)
            if header is None:
                errors.append(dict(source_row=row['source_row'],error='unmatched subtotal header',label=target))
                row.update(role='subtotal',kind='subtotal',amount_source='unmapped')
            else:
                leaves = [child for child in output[header['index']+1:] if child['role']=='leaf']
                if any(child['amount_cents'] is None for child in leaves):
                    amount = None
                else:
                    amount = sum(child['amount_cents'] for child in leaves)
                row.update(role='subtotal',kind='subtotal',amount_cents=amount,amount_source='computed_leaf_subtotal',
                           subtotal_leaf_codes=[child.get('account_code') or child.get('computed_account_code') for child in leaves])
        else:
            account_match = re.match(r'^(\d+)\s*[—–-]\s*',text)
            has_children = index+1<len(labels) and labels[index+1]['level']>level
            if account_match and not has_children:
                code = code_by_row.get(row['source_row'],account_match.group(1))
                account = by_code.get(code)
                if account is None:
                    errors.append(dict(source_row=row['source_row'],error='unmapped live account',account_code=code))
                    row.update(role='leaf',kind='leaf',account_code=code,amount_source='unmapped')
                else:
                    row.update(role='leaf',kind='leaf',account_code=code,account_kind=account['kind'],
                               amount_cents=account['amount_cents'],amount_source='computed_live_ledger',drilldown=True)
        if row['role']=='header':
            open_headers = [header for header in open_headers if header['level']<level]
            open_headers.append(dict(label=text,level=level,index=len(output)))
        elif row['role'] in ('subtotal','financial_total'):
            open_headers = [header for header in open_headers if header['level']<level]
        if row['amount_cents'] is not None:
            totals[text] = row['amount_cents']
        output.append(row)
    leaves = [row for row in output if row['role']=='leaf']
    included = {row.get('account_code') or row.get('computed_account_code') for row in leaves}
    unmapped = [row['code'] for row in computed if row['code'] not in included and row_is_active(row)]
    def total(kind):
        return sum(row['amount_cents'] or 0 for row in leaves if row.get('account_kind')==kind)
    if name=='profit_loss':
        matches = (total('income')==result['income_cents'] and total('expense')==result['expense_cents'] and
                   totals.get('Net Income')==result['net_profit_cents'])
    else:
        matches = (total('asset')==result['assets_cents'] and total('liability')==result['liabilities_cents'] and
                   total('equity')==result['total_equity_cents'] and totals.get('TOTAL ASSETS')==result['assets_cents'] and
                   totals.get('TOTAL LIABILITIES & EQUITY')==result['liabilities_cents']+result['total_equity_cents'])
    result.update(presentation_layout_rows=output,presentation_layout_header_rows=header_rows,
                  presentation_layout_caption=caption,presentation_layout_source='reference/sources/'+filename,
                  presentation_layout_source_row_count=len(source),presentation_layout_source_scope='original_2023_statement_labels',
                  presentation_layout_amount_source='computed_live_ledger_and_leaf_subtotals',
                  presentation_layout_total_match=bool(matches and not errors and not unmapped),
                  presentation_layout_unmapped_nonzero_account_codes=unmapped,presentation_layout_errors=errors)


def add_reference_presentation(name, params, result):
    if name not in ('trial_balance','profit_loss','balance_sheet'):
        return
    classified = json.loads((BASE/'reference'/'classified_gl_accounts.json').read_text(encoding='utf-8'))
    metadata = {row['code']:row for row in classified if row.get('posting')}
    computed_rows = result.get('display_rows',result['rows']) if name=='balance_sheet' else result['rows']
    by_code = {row['code']:row for row in computed_rows}
    native_key = None
    if name=='trial_balance' and params.get('as_of')=='2023-12-31' and params.get('fiscal_year_start')=='2023-01-01' and params.get('retained_account')=='32000':
        native_key = 'tb_2023-12-31'
    elif name=='profit_loss' and params.get('start_date')=='2023-01-01' and params.get('end_date')=='2023-12-31' and params.get('basis','accrual')=='accrual' and params.get('project_id') is None:
        native_key = 'pnl_2023'
    elif name=='balance_sheet' and params.get('as_of')=='2023-12-31' and params.get('basis','accrual')=='accrual' and params.get('retained_account')=='32000':
        native_key = 'bs_2023-12-31'
    rows, included = [], set()
    if native_key:
        # Only the source's labels and row positions are read for presentation.
        comparison = json.loads((BASE/'reference'/'original_report_comparison.json').read_text(encoding='utf-8'))['comparisons'][native_key]
        labels = [label_metadata(row) for row in comparison['leaf_rows']]
        if name=='balance_sheet':
            income_row = comparison['automatic_year_end_presentation']['original_bs_net_income']['source_row']
            labels.append(dict(source_row=income_row,account_code='__current_year_profit__',account_path=['Net Income'],official_type='Equity',kind='equity'))
        missing = [label['account_code'] for label in labels if label['account_code'] not in by_code]
        if missing:
            result['presentation_unmapped_account_codes'] = missing
            native_key = None
        else:
            for label in sorted(labels,key=lambda row:row['source_row']):
                code = label['account_code']
                rows.append(dict(by_code[code],**label,display_name=':'.join(label['account_path']),
                                 presentation_role='native_report_account'))
                included.add(code)
    active = []
    for row in computed_rows:
        if row['code'] in included or not row_is_active(row):
            continue
        info = metadata.get(row['code'],{})
        path = info.get('account_path') or [row['name']]
        official_type = info.get('official_type',row.get('kind','').title())
        active.append(dict(row,account_code=row['code'],account_path=path,official_type=official_type,
                           source_row=None,display_name=':'.join(path),presentation_role='computed_active_account'))
    active.sort(key=lambda row:(TYPE_ORDER.get(row['official_type'],99),row.get('kind',''),row['code']))
    rows.extend(active)
    result.update(presentation_rows=rows,presentation_amount_source='computed_live_ledger',
                  source_scope='original_2023_report_label_order' if native_key else 'computed_active_accounts_with_type_metadata',
                  presentation_source='reference/original_report_comparison.json' if native_key else 'reference/classified_gl_accounts.json',
                  display_tree=display_tree(rows))
    if name=='trial_balance':
        result['presentation_matches_report_totals'] = (
            sum(row['debit_cents'] for row in rows)==result['total_debit_cents'] and
            sum(row['credit_cents'] for row in rows)==result['total_credit_cents'])
    elif name=='profit_loss':
        result['presentation_matches_report_totals'] = (
            sum(row['amount_cents'] for row in rows if row['kind']=='income')==result['income_cents'] and
            sum(row['amount_cents'] for row in rows if row['kind']=='expense')==result['expense_cents'])
    else:
        result['presentation_matches_report_totals'] = (
            sum(row['amount_cents'] for row in rows if row['kind']=='asset')==result['assets_cents'] and
            sum(row['amount_cents'] for row in rows if row['kind']=='liability')==result['liabilities_cents'] and
            sum(row['amount_cents'] for row in rows if row['kind']=='equity')==result['total_equity_cents'])
    if native_key and name in ('profit_loss','balance_sheet'):
        add_reference_statement_layout(name,result,comparison)


def get_report(book, company, name, params):
    if company in READ_ONLY_BOOKS and name=='general_ledger':
        return reference_general_ledger(book,**params)
    result = getattr(book,name)(**params)
    if company in READ_ONLY_BOOKS and isinstance(result,dict):
        result.update(read_only=True,data_scope='reconstructed_gl_only')
        add_reference_presentation(name,params,result)
        if name=='account_listing':
            original = json.loads((BASE/'reference'/'classified_gl_accounts.json').read_text(encoding='utf-8'))
            metadata = {row['code']:row for row in original if row.get('posting')}
            result['rows'] = [dict(row,name=metadata[row['code']]['name'],posted_name=row['name'],official_type=metadata[row['code']]['official_type'],
                             account_path=metadata[row['code']]['account_path']) for row in result['rows'] if row['code'] in metadata]
        if name=='contact_center':
            result['master_data_reconstructed'] = False
    return result


def validate_fields(payload):
    for key, value in payload.items():
        if value is None:
            continue
        if key.endswith("_cents"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("金额必须使用整数分。")
        if key.endswith("_id") and key != "external_id":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("请选择有效的记录。")
        if key in ("date", "due_date", "as_of", "start_date", "end_date", "through_date", "statement_date", "fiscal_year_start"):
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("日期格式必须为 YYYY-MM-DD。")
        if key == "qty":
            if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)?", value) or Decimal(value) <= 0:
                raise ValueError("数量必须为正数。")
        if key in ("name", "sku", "code", "memo") and (not isinstance(value, str) or len(value) > 1000):
            raise ValueError("文本过长或格式错误。")
        if key == "lines":
            if not isinstance(value, list) or not 1 <= len(value) <= 500:
                raise ValueError("单据应包含 1 至 500 行。")
            for line in value:
                if not isinstance(line, dict):
                    raise ValueError("单据行格式错误。")
                validate_fields(line)
        if key == 'allocations':
            if not isinstance(value,list) or not 1<=len(value)<=500:
                raise ValueError('付款分配应包含 1 至 500 张单据。')
            for allocation in value:
                if not isinstance(allocation,dict) or set(allocation)!={'document_id','amount_cents'}:
                    raise ValueError('付款分配只允许单据编号和金额。')
                validate_fields(allocation)


def seed_demo(book):
    """Load a separate fixed synthetic ledger, never the user's empty company."""
    if book.list_data().get("journals"):
        raise ValueError("合成测试账本已存在，不重复装载；不会影响您的公司账本。")
    scenario = json.loads((BASE / "fixtures" / "scenario.json").read_text(encoding="utf-8"))
    contacts, projects, documents = {}, {}, {}
    for account in scenario["accounts_to_create"]:
        book.create_account(account["code"], account["name"], account["type"])
    for contact in scenario["contacts"]:
        contacts[contact["key"]] = book.create_contact(contact["name"], kind=contact["kind"])["id"]
    for project in scenario["projects"]:
        projects[project["key"]] = book.create_project(project["name"])["id"]
    item = scenario["item"]
    item_id = book.create_item(item["sku"], item["name"])["id"]
    for event in scenario["events"]:
        common = {"date": event["date"], "external_id": event["key"]}
        project_id = projects.get(event.get("project"))
        kind = event["type"]
        if kind == "journal":
            lines = []
            for line in event["lines"]:
                row = {"account": line["account"], "debit_cents": line.get("debit_cents", 0), "credit_cents": line.get("credit_cents", 0)}
                if line.get("project"):
                    row["project_id"] = projects[line["project"]]
                lines.append(row)
            result = book.post_journal(memo=event["memo"], lines=lines, **common)
        elif kind in ("invoice", "bill"):
            method = book.create_invoice if kind == "invoice" else book.create_bill
            result = method(contact_id=contacts[event["contact"]], lines=event["lines"], project_id=project_id, **common)
            documents[event["key"]] = result["id"]
        elif kind == "buy_inventory":
            result = book.buy_inventory(item_id=item_id, qty=event["qty"], unit_cost_cents=event["unit_cost_cents"], offset_account=event["offset_account"], contact_id=contacts[event["contact"]], **common)
            if result.get("bill_id"):
                documents[event["key"]] = result["bill_id"]
        elif kind == "sell_inventory":
            result = book.sell_inventory(item_id=item_id, qty=event["qty"], unit_price_cents=event["unit_price_cents"], contact_id=contacts[event["contact"]], project_id=project_id, **common)
            documents[event["key"]] = result["id"]
        elif kind == "receive_payment":
            result = book.receive_payment(invoice_id=documents[event["document"]], amount_cents=event["amount_cents"], **common)
        elif kind == "pay_bill":
            result = book.pay_bill(bill_id=documents[event["document"]], amount_cents=event["amount_cents"], **common)
        else:
            raise ValueError("合成资料含有未支持的交易类型。")
    for period, rows in scenario["budgets"].items():
        for account, amount in rows.items():
            book.set_budget(period=period, account=account, amount_cents=amount)
    book.import_bank_csv((BASE / "fixtures" / "bank.csv").read_text(encoding="utf-8"))
    return {"message": "合成验收账本已装载到独立的测试数据库。", "book": "demo"}


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalLedger/1"

    def log_message(self, format, *args):
        # No payloads, personal records or imported bank details in logs.
        print("%s %s" % (self.log_date_time_string(), format % args))

    def send_json(self, code, data):
        body = encode(data)
        self.send_payload(code, body, "application/json; charset=utf-8")

    def send_payload(self, code, body, content_type, filename=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(body)

    def check_host(self):
        allowed = {"127.0.0.1:%s" % self.server.server_port, "localhost:%s" % self.server.server_port}
        if self.headers.get("Host", "") not in allowed:
            raise ValueError("只允许从本机地址访问。")
        return allowed

    def parse_query(self):
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        company = params.pop("book", "company")
        if company not in self.books:
            raise ValueError("未知账本。")
        return parsed.path, params, company

    @property
    def books(self):
        return getattr(self.server,'books',BOOKS)

    @property
    def locks(self):
        return getattr(self.server,'book_locks',LOCKS)

    def do_GET(self):
        try:
            self.check_host()
            path, params, company = self.parse_query()
            if not path.startswith('/api/'):
                target = (WEB/('index.html' if path=='/' else path.lstrip('/'))).resolve()
                allowed_suffixes = {'.html','.js','.css','.png','.svg','.ico','.woff','.woff2'}
                if WEB in target.parents and target.suffix.lower() in allowed_suffixes and target.is_file():
                    content_type = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
                    if target.suffix.lower() in {'.html','.js','.css','.svg'}:
                        content_type += '; charset=utf-8'
                    self.send_payload(200,target.read_bytes(),content_type)
                    return
            if path not in ("/api/state", "/api/report", "/api/export", "/api/backup"):
                self.send_json(404, {"error": "页面不存在。"})
                return
            if company=='reference' and not reference_available(self.books):
                self.send_json(404,{'error':'Reference comparison data is not included in this distribution.',
                                    'code':'reference_unavailable','available_books':['company','demo']})
                return
            with self.locks[company]:
                book = open_book(company,self.books)
                try:
                    if path == "/api/state":
                        available = ['company','demo']+(['reference'] if reference_available(self.books) else [])
                        self.send_json(200, {"book": company, "read_only": company in READ_ONLY_BOOKS,'available_books':available,
                                            "data_scope": book_scope(company), "status": book.status(), "data": book.list_data()})
                    elif path == "/api/report":
                        name = params.pop("name", "")
                        if name not in REPORTS or set(params) - REPORTS[name]:
                            raise ValueError("不支持的报表或参数。")
                        for key in ("project_id","contact_id","invoice_id","bill_id"):
                            if key in params:
                                params[key] = int(params[key])
                        validate_fields(params)
                        result = get_report(book,company,name,params)
                        self.send_json(200, {"name": name, "result": result})
                    elif path == "/api/export":
                        content = encode({"exported_at": date.today().isoformat(), "book": company, "status": book.status(), "data": book.list_data()})
                        self.send_payload(200, content, "application/json; charset=utf-8", "ledger-%s-%s.json" % (company, date.today()))
                    else:
                        destination = sqlite3.connect(":memory:")
                        try:
                            book.conn.backup(destination)
                            body = destination.serialize()
                        finally:
                            destination.close()
                        self.send_payload(200, body, "application/vnd.sqlite3", "ledger-%s-%s.sqlite" % (company, date.today()))
                finally:
                    if book is not None:
                        close_book(book)
        except (ValueError, TypeError, KeyError) as error:
            self.send_json(400, {"error": "操作未完成：" + error_text(error)})
        except Exception as error:
            self.send_json(500, {"error": "操作未完成，请检查数据或服务日志。", "detail": type(error).__name__})
            print("GET error:", repr(error))

    def do_POST(self):
        try:
            allowed = self.check_host()
            origin = self.headers.get("Origin", "")
            if origin not in {"http://" + host for host in allowed}:
                raise ValueError("请求来源无效，请在本机应用页面重新操作。")
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
                raise ValueError("不接受跨站写入。")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("写入请求必须使用 JSON。")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("请求为空或超过 2 MB。")
            path, params, company = self.parse_query()
            if params or path != "/api/action":
                self.send_json(404, {"error": "接口不存在。"})
                return
            if company in READ_ONLY_BOOKS:
                self.send_json(403,{"error":"参考账本只读，不能保存、付款、关账或装载合成数据。"})
                return
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or set(data) != {"action", "payload"}:
                raise ValueError("请求格式错误。")
            action, payload = data["action"], data["payload"]
            if not isinstance(action, str) or not isinstance(payload, dict):
                raise ValueError("请求格式错误。")
            if action == "load_demo":
                if payload or company != "demo":
                    raise ValueError("合成数据只能装载到独立测试账本。")
            elif action not in ACTIONS or set(payload) - ACTIONS[action]:
                raise ValueError("不支持的操作或参数。")
            validate_fields(payload)
            if "lines" in payload:
                allowed_line_fields = {"account", "debit_cents", "credit_cents", "project_id"} if action == "post_journal" else {"description", "amount_cents", "account", "project_id", "item_id", "qty", "unit_price_cents", "unit_cost_cents"}
                if any(set(line) - allowed_line_fields for line in payload["lines"]):
                    raise ValueError("不支持的单据行字段。")
            if action == "import_bank_csv":
                contents = payload.get("contents", "")
                if not isinstance(contents, str) or "\n" not in contents or "," not in contents.splitlines()[0]:
                    raise ValueError("请提供 CSV 文本和表头，不能填写本机文件路径。")
            with self.locks[company]:
                book = open_book(company,self.books)
                try:
                    result = seed_demo(book) if action == "load_demo" else getattr(book, action)(**payload)
                    self.send_json(200, {"ok": True, "result": result})
                finally:
                    close_book(book)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": "操作未完成：" + error_text(error)})
        except Exception as error:
            self.send_json(500, {"error": "操作未完成，请检查数据或服务日志。", "detail": type(error).__name__})
            print("POST error:", repr(error))


def create_server(port=0,data_dir=None):
    if type(port) is not int or not 0<=port<=65535:
        raise ValueError('port must be an integer from 0 through 65535')
    target = validate_data_dir(data_dir if data_dir is not None else default_data_dir())
    target.mkdir(parents=True,exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads = True
    server.books = {'company':target/'company.sqlite','demo':target/'demo.sqlite',
                    'reference':BASE/'reference'/'reconstructed_qb_gl.sqlite3'}
    server.book_locks = {key:threading.RLock() for key in server.books}
    server.data_dir = target
    return server


def main():
    parser = argparse.ArgumentParser(description="Ledger Reborn local server; binds only 127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument('--data-dir',type=Path,help='Local company data directory; overrides LEDGER_REBORN_DATA_DIR')
    args = parser.parse_args()
    try:
        server = create_server(args.port,args.data_dir)
    except (OSError,ValueError) as error:
        parser.error(str(error))
    print("Ledger Reborn: http://127.0.0.1:%s" % server.server_port)
    print("Data directory: %s; quit with Control-C." % server.data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
