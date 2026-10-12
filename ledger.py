"""A local, independent, double-entry accounting engine.

Money is integer cents. Inventory quantities are Decimal strings. The engine is
not Intuit code, a QuickBooks file reader, or a tax/payroll filing system.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date as Date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path


DEFAULT_ACCOUNTS = [
    ('1000', 'Bank', 'asset'), ('1100', 'Accounts Receivable', 'asset'),
    ('1200', 'Inventory', 'asset'), ('1500', 'Equipment', 'asset'),
    ('1590', 'Accumulated Depreciation', 'asset'),
    ('2000', 'Accounts Payable', 'liability'), ('2100', 'Tax Payable', 'liability'),
    ('3000', 'Owner Equity', 'equity'), ('3100', 'Retained Earnings', 'equity'),
    ('4000', 'Sales', 'income'), ('5000', 'Cost of Goods Sold', 'expense'),
    ('6100', 'Rent', 'expense'), ('6200', 'Utilities', 'expense'),
    ('6300', 'Office', 'expense'), ('6400', 'Depreciation', 'expense'),
    ('6500', 'Bank Fees', 'expense'),
]


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('date must be YYYY-MM-DD')
    try:
        Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('invalid calendar date') from exc
    return value


def _cents(value, positive=False):
    if type(value) is not int:
        raise ValueError('money must be integer cents; floats are not accepted')
    if abs(value) > 10**15:
        raise ValueError('amount exceeds supported range')
    if positive and value <= 0:
        raise ValueError('amount must be positive')
    return value


def _quantity(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('quantity must be a Decimal, integer, or decimal string')
    try:
        qty = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('invalid quantity') from exc
    if not qty.is_finite() or qty <= 0 or qty > Decimal('1000000000'):
        raise ValueError('quantity must be finite, positive, and at most 1e9')
    if qty.as_tuple().exponent < -6:
        raise ValueError('quantity supports at most six decimal places')
    return qty


def _decimal_text(value):
    return format(value, 'f')


def _round_cents(value):
    return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _text(value, name='text', max_length=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f'{name} must be a nonempty string of at most {max_length} characters')
    return value.strip()


CONTACT_DETAIL_LIMITS = {'company_name':200,'first_name':200,'last_name':200,'phone':100,'email':320,'address':2000}


def _optional_text(value, name, max_length):
    if value is None:
        return ''
    if not isinstance(value,str) or len(value)>max_length:
        raise ValueError(f'{name} must be a string of at most {max_length} characters')
    return value.strip()


def _id(value, name='id'):
    if type(value) is not int or value <= 0 or value >= 2**63:
        raise ValueError(f'{name} must be a positive integer id')
    return value


def _consistent_read(fn):
    """All queries of a report see a single SQLite snapshot."""
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            nested = self.conn.in_transaction
            if not nested:
                self._price_metadata = None
                self._contact_detail_metadata = None
                self._document_detail_metadata = None
                self.conn.execute('BEGIN')
            try:
                result = fn(self, *args, **kwargs)
                if not nested:
                    self.conn.execute('COMMIT')
                return result
            except Exception:
                if not nested:
                    self.conn.execute('ROLLBACK')
                raise
    return wrapped


def _allocate(amount, weights):
    """Allocate integer cents proportionally, preserving the exact total."""
    if not weights:
        return []
    total = sum(weights)
    if total <= 0:
        raise ValueError('allocation weights must be positive')
    base = [amount * weight // total for weight in weights]
    remainder = amount - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % total), i))
    for i in order[:remainder]:
        base[i] += 1
    return base


class Ledger:
    """SQLite-backed accounting API. Mutations are atomic and serialized."""

    def __init__(self, path='ledger.sqlite3', readonly=False):
        if type(readonly) is not bool:
            raise ValueError('readonly must be a boolean')
        self.path = str(path)
        self.read_only = readonly
        self._price_metadata = None
        self._contact_detail_metadata = None
        self._document_detail_metadata = None
        if readonly and (self.path == ':memory:' or not Path(self.path).is_file()):
            raise ValueError('read-only ledger must be an existing database file')
        wal = Path(self.path+'-wal')
        if readonly and wal.exists() and wal.stat().st_size:
            raise ValueError('read-only immutable ledger requires a checkpointed static database, not an active WAL file')
        if not readonly and self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        connection_path = Path(self.path).resolve().as_uri()+'?mode=ro&immutable=1' if readonly else self.path
        self.conn = sqlite3.connect(connection_path, uri=readonly, isolation_level=None, timeout=15, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA busy_timeout=15000')
        if readonly:
            self.conn.execute('PRAGMA query_only=ON')
            self.conn.execute('SELECT 1 FROM accounts LIMIT 1')
        else:
            self.conn.execute('PRAGMA journal_mode=WAL')
            self._initialize()

    def _initialize(self):
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('asset','liability','equity','income','expense')));
        CREATE TABLE IF NOT EXISTS contacts (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL
          CHECK(kind IN ('customer','vendor','both')));
        CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS entries (
          id INTEGER PRIMARY KEY, date TEXT NOT NULL, memo TEXT NOT NULL,
          source TEXT NOT NULL, external_id TEXT,
          reversal_of INTEGER UNIQUE REFERENCES entries(id));
        CREATE TABLE IF NOT EXISTS journal_lines (
          id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES entries(id),
          account_id INTEGER NOT NULL REFERENCES accounts(id),
          debit_cents INTEGER NOT NULL CHECK(debit_cents>=0),
          credit_cents INTEGER NOT NULL CHECK(credit_cents>=0),
          project_id INTEGER REFERENCES projects(id),
          CHECK((debit_cents>0 AND credit_cents=0) OR (credit_cents>0 AND debit_cents=0)));
        CREATE INDEX IF NOT EXISTS journal_account ON journal_lines(account_id,entry_id);
        CREATE INDEX IF NOT EXISTS entry_date ON entries(date);
        CREATE TABLE IF NOT EXISTS documents (
          id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('invoice','bill')),
          date TEXT NOT NULL, due_date TEXT NOT NULL, contact_id INTEGER NOT NULL REFERENCES contacts(id),
          project_id INTEGER REFERENCES projects(id), total_cents INTEGER NOT NULL CHECK(total_cents>0),
          entry_id INTEGER NOT NULL UNIQUE REFERENCES entries(id), external_id TEXT);
        CREATE TABLE IF NOT EXISTS document_lines (
          id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
          description TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
          account_id INTEGER NOT NULL REFERENCES accounts(id), project_id INTEGER REFERENCES projects(id),
          item_id INTEGER, qty TEXT, cogs_cents INTEGER NOT NULL DEFAULT 0,
          cogs_account_id INTEGER REFERENCES accounts(id), inventory_account_id INTEGER REFERENCES accounts(id));
        CREATE TABLE IF NOT EXISTS payments (
          id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
          date TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
          bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
          entry_id INTEGER NOT NULL UNIQUE REFERENCES entries(id), external_id TEXT);
        CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          inventory_account_id INTEGER NOT NULL REFERENCES accounts(id),
          cogs_account_id INTEGER NOT NULL REFERENCES accounts(id),
          income_account_id INTEGER NOT NULL REFERENCES accounts(id));
        CREATE TABLE IF NOT EXISTS inventory_movements (
          id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), date TEXT NOT NULL,
          qty TEXT NOT NULL, value_cents INTEGER NOT NULL, type TEXT NOT NULL,
          entry_id INTEGER NOT NULL REFERENCES entries(id), document_id INTEGER REFERENCES documents(id));
        CREATE TABLE IF NOT EXISTS bank_transactions (
          id INTEGER PRIMARY KEY, bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
          date TEXT NOT NULL, description TEXT NOT NULL, amount_cents INTEGER NOT NULL,
          external_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          entry_line_id INTEGER UNIQUE REFERENCES journal_lines(id),
          UNIQUE(bank_account_id,external_id));
        CREATE TABLE IF NOT EXISTS reconciliations (
          id INTEGER PRIMARY KEY, bank_account_id INTEGER NOT NULL REFERENCES accounts(id),
          statement_date TEXT NOT NULL, ending_balance_cents INTEGER NOT NULL,
          UNIQUE(bank_account_id,statement_date));
        CREATE TABLE IF NOT EXISTS reconciliation_lines (
          reconciliation_id INTEGER NOT NULL REFERENCES reconciliations(id),
          entry_line_id INTEGER NOT NULL UNIQUE REFERENCES journal_lines(id),
          PRIMARY KEY(reconciliation_id,entry_line_id));
        CREATE TABLE IF NOT EXISTS budgets (
          id INTEGER PRIMARY KEY, period TEXT NOT NULL, account_id INTEGER NOT NULL REFERENCES accounts(id),
          amount_cents INTEGER NOT NULL, project_id INTEGER REFERENCES projects(id));
        CREATE UNIQUE INDEX IF NOT EXISTS budget_unique ON budgets(period,account_id,COALESCE(project_id,0));
        CREATE TABLE IF NOT EXISTS closed_periods (end_date TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          action TEXT NOT NULL, data_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests (
          operation TEXT NOT NULL, external_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
          result_json TEXT NOT NULL, PRIMARY KEY(operation,external_id));
        CREATE TRIGGER IF NOT EXISTS immutable_entries_update BEFORE UPDATE ON entries
          BEGIN SELECT RAISE(ABORT,'journal entries are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_entries_delete BEFORE DELETE ON entries
          BEGIN SELECT RAISE(ABORT,'journal entries are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_lines_update BEFORE UPDATE ON journal_lines
          BEGIN SELECT RAISE(ABORT,'journal lines are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_lines_delete BEFORE DELETE ON journal_lines
          BEGIN SELECT RAISE(ABORT,'journal lines are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit
          BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit
          BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_document_update BEFORE UPDATE ON documents
          BEGIN SELECT RAISE(ABORT,'documents are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_document_delete BEFORE DELETE ON documents
          BEGIN SELECT RAISE(ABORT,'documents are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_docline_update BEFORE UPDATE ON document_lines
          BEGIN SELECT RAISE(ABORT,'document lines are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_docline_delete BEFORE DELETE ON document_lines
          BEGIN SELECT RAISE(ABORT,'document lines are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_payment_update BEFORE UPDATE ON payments
          BEGIN SELECT RAISE(ABORT,'payments are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_payment_delete BEFORE DELETE ON payments
          BEGIN SELECT RAISE(ABORT,'payments are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_inventory_update BEFORE UPDATE ON inventory_movements
          BEGIN SELECT RAISE(ABORT,'inventory movements are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_inventory_delete BEFORE DELETE ON inventory_movements
          BEGIN SELECT RAISE(ABORT,'inventory movements are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS closed_entry_insert BEFORE INSERT ON entries
          WHEN NEW.date <= COALESCE((SELECT MAX(end_date) FROM closed_periods),'')
          BEGIN SELECT RAISE(ABORT,'accounting period is closed'); END;
        ''')
        with self._transaction():
            self.conn.executemany('INSERT OR IGNORE INTO accounts(code,name,kind) VALUES(?,?,?)', DEFAULT_ACCOUNTS)

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise ValueError('ledger is read-only')
        with self._lock:
            self._price_metadata = None
            self._contact_detail_metadata = None
            self._document_detail_metadata = None
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.conn.execute('COMMIT')
            except Exception:
                self.conn.execute('ROLLBACK')
                raise

    def _audit(self, action, data):
        self.conn.execute('INSERT INTO audit(action,data_json) VALUES(?,?)',
                          (action, json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)))

    def _run(self, operation, payload, external_id, fn):
        if external_id is not None:
            external_id = _text(external_id, 'external_id', 200)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        with self._transaction():
            if external_id is not None:
                row = self.conn.execute('SELECT * FROM requests WHERE operation=? AND external_id=?',
                                        (operation, external_id)).fetchone()
                if row:
                    if row['payload_hash'] != digest:
                        raise ValueError('external_id already used with a different request')
                    return json.loads(row['result_json'])
            result = fn()
            self._audit(operation, {'request': payload, 'result': result})
            if external_id is not None:
                self.conn.execute('INSERT INTO requests VALUES(?,?,?,?)',
                                  (operation, external_id, digest, json.dumps(result, default=str)))
            return result

    def _account(self, code, kinds=None):
        row = self.conn.execute('SELECT * FROM accounts WHERE code=?', (str(code),)).fetchone()
        if row is None:
            raise ValueError(f'unknown account: {code}')
        if kinds and row['kind'] not in kinds:
            raise ValueError(f'account {code} must have kind in {sorted(kinds)}')
        return row

    def _project(self, project_id):
        if project_id is not None:
            _id(project_id, 'project_id')
            if not self.conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
                raise ValueError('unknown project')
        return project_id

    def _contact(self, contact_id, kind):
        _id(contact_id, 'contact_id')
        row = self.conn.execute('SELECT * FROM contacts WHERE id=?', (contact_id,)).fetchone()
        if row is None or row['kind'] not in (kind, 'both'):
            raise ValueError(f'contact must be a {kind}')
        return row

    def _open_date(self, date):
        _date(date)
        closed = self.conn.execute('SELECT MAX(end_date) FROM closed_periods').fetchone()[0]
        if closed and date <= closed:
            raise ValueError(f'accounting period is closed through {closed}')

    def _is_inventory_account(self, account):
        return account['code'] == '1200' or bool(self.conn.execute(
            'SELECT 1 FROM items WHERE inventory_account_id=? LIMIT 1', (account['id'],)).fetchone())

    def create_account(self, code, name, kind):
        code, name = _text(str(code), 'account code', 40), _text(name, 'account name', 200)
        if kind not in ('asset', 'liability', 'equity', 'income', 'expense'):
            raise ValueError('unsupported account kind')
        def create():
            if self.conn.execute('SELECT 1 FROM accounts WHERE code=?', (code,)).fetchone():
                raise ValueError('account code already exists')
            cursor = self.conn.execute('INSERT INTO accounts(code,name,kind) VALUES(?,?,?)', (code, name, kind))
            return {'id': cursor.lastrowid, 'code': code, 'name': name, 'kind': kind}
        return self._run('create_account', {'code': code, 'name': name, 'kind': kind}, None, create)

    def create_contact(self, name, kind, company_name=None, first_name=None, last_name=None, phone=None, email=None, address=None):
        name = _text(name, 'contact name', 200)
        if kind not in ('customer', 'vendor', 'both'):
            raise ValueError('contact kind must be customer, vendor, or both')
        raw = dict(company_name=company_name,first_name=first_name,last_name=last_name,phone=phone,email=email,address=address)
        details = {key:_optional_text(value,key,CONTACT_DETAIL_LIMITS[key]) for key,value in raw.items() if value is not None}
        def create():
            cursor = self.conn.execute('INSERT INTO contacts(name,kind) VALUES(?,?)', (name, kind))
            if details:
                self._audit('contact_details',dict(contact_id=cursor.lastrowid,details=details))
                self._contact_detail_metadata = None
            return self._contact_record(cursor.lastrowid)
        return self._run('create_contact',dict(name=name,kind=kind,**details),None,create)

    def _contact_record(self, contact_id):
        _id(contact_id,'contact_id')
        row = self.conn.execute('SELECT * FROM contacts WHERE id=?',(contact_id,)).fetchone()
        if row is None:
            raise ValueError('unknown contact')
        if self._contact_detail_metadata is None:
            metadata = {}
            for record in self.conn.execute("SELECT data_json FROM audit WHERE action='contact_details' ORDER BY id"):
                detail = json.loads(record['data_json'])
                metadata.setdefault(detail['contact_id'],{}).update(detail['details'])
            self._contact_detail_metadata = metadata
        return dict(row,**{key:self._contact_detail_metadata.get(contact_id,{}).get(key,'') for key in CONTACT_DETAIL_LIMITS})

    @_consistent_read
    def get_contact(self, contact_id):
        return self._contact_record(contact_id)

    def update_contact_details(self, contact_id, name=None, company_name=None, first_name=None, last_name=None,
                               phone=None, email=None, address=None, external_id=None):
        _id(contact_id,'contact_id')
        if name is not None:
            name = _text(name,'contact name',200)
        raw = dict(company_name=company_name,first_name=first_name,last_name=last_name,phone=phone,email=email,address=address)
        details = {key:_optional_text(value,key,CONTACT_DETAIL_LIMITS[key]) for key,value in raw.items() if value is not None}
        if name is None and not details:
            raise ValueError('contact update requires name or detail fields')
        def save():
            self._contact_record(contact_id)
            if name is not None:
                self.conn.execute('UPDATE contacts SET name=? WHERE id=?',(name,contact_id))
            if details:
                self._audit('contact_details',dict(contact_id=contact_id,details=details))
                self._contact_detail_metadata = None
            return self._contact_record(contact_id)
        return self._run('update_contact_details',dict(contact_id=contact_id,name=name,**details),external_id,save)

    def create_project(self, name):
        name = _text(name, 'project name', 200)
        def create():
            if self.conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
                raise ValueError('project name already exists')
            cursor = self.conn.execute('INSERT INTO projects(name) VALUES(?)', (name,))
            return {'id': cursor.lastrowid, 'name': name}
        return self._run('create_project', {'name': name}, None, create)

    def create_item(self, sku, name, inventory_account='1200', cogs_account='5000', income_account='4000'):
        sku, name = _text(sku, 'sku', 80), _text(name, 'item name', 200)
        def create():
            ia = self._account(inventory_account, {'asset'})
            ca = self._account(cogs_account, {'expense'})
            sa = self._account(income_account, {'income'})
            if self.conn.execute('SELECT 1 FROM items WHERE sku=?', (sku,)).fetchone():
                raise ValueError('sku already exists')
            cur = self.conn.execute('INSERT INTO items(sku,name,inventory_account_id,cogs_account_id,income_account_id) VALUES(?,?,?,?,?)',
                                    (sku, name, ia['id'], ca['id'], sa['id']))
            return self._item(cur.lastrowid)
        return self._run('create_item', dict(sku=sku, name=name, inventory_account=inventory_account,
                         cogs_account=cogs_account, income_account=income_account), None, create)

    def _item(self, item_id):
        _id(item_id, 'item_id')
        row = self.conn.execute('''SELECT i.*,ia.code inventory_account,ca.code cogs_account,sa.code income_account
          FROM items i JOIN accounts ia ON ia.id=i.inventory_account_id
          JOIN accounts ca ON ca.id=i.cogs_account_id JOIN accounts sa ON sa.id=i.income_account_id WHERE i.id=?''',
                                (item_id,)).fetchone()
        if row is None:
            raise ValueError('unknown inventory item')
        result = dict(row)
        qty, value = self._inventory_balance(item_id)
        result.update(qty=_decimal_text(qty), value_cents=value)
        return result

    def _entry(self, entry_id):
        _id(entry_id, 'entry_id')
        row = self.conn.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
        if row is None:
            raise ValueError('unknown journal entry')
        result = dict(row)
        result['lines'] = [dict(r) for r in self.conn.execute('''SELECT l.id,l.entry_id,a.code account,a.name account_name,
          l.debit_cents,l.credit_cents,l.project_id FROM journal_lines l JOIN accounts a ON a.id=l.account_id
          WHERE l.entry_id=? ORDER BY l.id''', (entry_id,))]
        return result

    def _post(self, date, memo, lines, source, external_id=None, reversal_of=None):
        self._open_date(date)
        if not isinstance(lines, (list, tuple)) or len(lines) < 2:
            raise ValueError('journal requires at least two lines')
        normalized = []
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError('journal line must be an object')
            account = self._account(line.get('account'))
            if source in ('journal', 'reversal', 'bank_import') and self._is_inventory_account(account):
                raise ValueError('use inventory purchase/sale APIs; direct inventory account entries would bypass item quantities')
            debit = _cents(line.get('debit_cents', 0))
            credit = _cents(line.get('credit_cents', 0))
            if debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
                raise ValueError('each journal line must have one positive debit or credit')
            project = self._project(line.get('project_id'))
            normalized.append((account['id'], debit, credit, project))
        debit_total = sum(l[1] for l in normalized)
        credit_total = sum(l[2] for l in normalized)
        if debit_total != credit_total:
            raise ValueError('journal debits and credits do not balance')
        _cents(debit_total, positive=True)
        cur = self.conn.execute('INSERT INTO entries(date,memo,source,external_id,reversal_of) VALUES(?,?,?,?,?)',
                                (date, str(memo or '').strip(), source, external_id, reversal_of))
        self.conn.executemany('INSERT INTO journal_lines(entry_id,account_id,debit_cents,credit_cents,project_id) VALUES(?,?,?,?,?)',
                              [(cur.lastrowid,) + l for l in normalized])
        return self._entry(cur.lastrowid)

    def post_journal(self, date, memo, lines, external_id=None):
        _date(date)
        payload = dict(date=date, memo=memo, lines=lines)
        return self._run('post_journal', payload, external_id,
                         lambda: self._post(date, memo, lines, 'journal', external_id))

    def void_journal(self, entry_id, date, memo='Reversal', external_id=None):
        _date(date)
        def reverse():
            entry = self._entry(entry_id)
            if entry['source'] not in ('journal', 'reversal'):
                raise ValueError('document, payment, bank-import, and inventory entries cannot be voided independently')
            if date < entry['date']:
                raise ValueError('reversal cannot precede original entry')
            if self.conn.execute('SELECT 1 FROM entries WHERE reversal_of=?', (entry_id,)).fetchone():
                raise ValueError('journal has already been reversed')
            lines = [dict(account=l['account'], debit_cents=l['credit_cents'], credit_cents=l['debit_cents'],
                          project_id=l['project_id']) for l in entry['lines']]
            return self._post(date, f'{memo}: entry {entry_id}', lines, 'reversal', external_id, entry_id)
        return self._run('void_journal', dict(entry_id=entry_id, date=date, memo=memo), external_id, reverse)

    def _make_document(self, type, date, contact_id, lines, due_date, project_id, external_id=None, inventory=False, memo=None):
        self._open_date(date)
        due_date = _date(due_date or date)
        if due_date < date:
            raise ValueError('due date cannot precede document date')
        self._contact(contact_id, 'customer' if type == 'invoice' else 'vendor')
        self._project(project_id)
        if not isinstance(lines, (list, tuple)) or not lines:
            raise ValueError('document requires at least one line')
        normalized, journal = [], []
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError('document line must be an object')
            if not inventory and any(k in line for k in ('item_id','qty','cogs_cents','cogs_account_id','inventory_account_id')):
                raise ValueError('use buy_inventory/sell_inventory for inventory document lines')
            account = self._account(line.get('account', '4000' if type == 'invoice' else '6300'),
                                    {'income'} if type == 'invoice' else {'expense', 'asset'})
            if not inventory and self._is_inventory_account(account):
                raise ValueError('use buy_inventory for inventory bills')
            amount = _cents(line.get('amount_cents'), positive=True)
            description = _text(line.get('description', 'Sale' if type == 'invoice' else 'Expense'), 'description', 1000)
            project = self._project(line.get('project_id', project_id))
            normalized.append(dict(description=description, amount_cents=amount, account_id=account['id'],
                                   project_id=project, item_id=line.get('item_id'), qty=line.get('qty'),
                                   cogs_cents=_cents(line.get('cogs_cents', 0)),
                                   cogs_account_id=line.get('cogs_account_id'), inventory_account_id=line.get('inventory_account_id')))
            journal.append(dict(account=account['code'], debit_cents=amount if type == 'bill' else 0,
                                credit_cents=amount if type == 'invoice' else 0, project_id=project))
        total = _cents(sum(l['amount_cents'] for l in normalized), positive=True)
        journal.insert(0, dict(account='1100' if type == 'invoice' else '2000',
                               debit_cents=total if type == 'invoice' else 0,
                               credit_cents=total if type == 'bill' else 0, project_id=project_id))
        for line in normalized:
            if line['cogs_cents']:
                if type != 'invoice' or not line['cogs_account_id'] or not line['inventory_account_id']:
                    raise ValueError('invalid inventory cost line')
                ca = self.conn.execute('SELECT code FROM accounts WHERE id=?', (line['cogs_account_id'],)).fetchone()[0]
                ia = self.conn.execute('SELECT code FROM accounts WHERE id=?', (line['inventory_account_id'],)).fetchone()[0]
                journal.extend([dict(account=ca, debit_cents=line['cogs_cents'], project_id=line['project_id']),
                                dict(account=ia, credit_cents=line['cogs_cents'], project_id=line['project_id'])])
        memo = _optional_text(memo,'memo',1000)
        journal_memo = f'{type.title()} - {self._contact(contact_id, "customer" if type == "invoice" else "vendor")["name"]}'
        if memo:
            journal_memo += ' | '+memo
        entry = self._post(date, journal_memo,
                           journal, type, external_id)
        cur = self.conn.execute('INSERT INTO documents(type,date,due_date,contact_id,project_id,total_cents,entry_id,external_id) VALUES(?,?,?,?,?,?,?,?)',
                                (type, date, due_date, contact_id, project_id, total, entry['id'], external_id))
        for line in normalized:
            self.conn.execute('''INSERT INTO document_lines(document_id,description,amount_cents,account_id,project_id,
              item_id,qty,cogs_cents,cogs_account_id,inventory_account_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                              (cur.lastrowid, line['description'], line['amount_cents'], line['account_id'], line['project_id'],
                               line['item_id'], line['qty'], line['cogs_cents'], line['cogs_account_id'], line['inventory_account_id']))
        self._audit('document_details',dict(document_id=cur.lastrowid,memo=memo,contact_snapshot=self._contact_record(contact_id)))
        self._document_detail_metadata = None
        return self._document(cur.lastrowid)

    def create_invoice(self, date, contact_id, lines, due_date=None, project_id=None, external_id=None, memo=None):
        _date(date)
        memo = _optional_text(memo,'memo',1000)
        payload = dict(date=date,contact_id=contact_id,lines=lines,due_date=due_date,project_id=project_id)
        if memo:
            payload['memo'] = memo
        return self._run('create_invoice',payload,external_id,
                         lambda: self._create_document_with_items('invoice',date,contact_id,lines,due_date,project_id,external_id,memo))

    def create_bill(self, date, contact_id, lines, due_date=None, project_id=None, external_id=None, memo=None):
        _date(date)
        memo = _optional_text(memo,'memo',1000)
        payload = dict(date=date,contact_id=contact_id,lines=lines,due_date=due_date,project_id=project_id)
        if memo:
            payload['memo'] = memo
        return self._run('create_bill',payload,external_id,
                         lambda: self._create_document_with_items('bill',date,contact_id,lines,due_date,project_id,external_id,memo))

    def _create_document_with_items(self, type, date, contact_id, lines, due_date, project_id, external_id, memo=None):
        """Prepare all item rows before one atomic document/journal posting."""
        if not isinstance(lines, (list, tuple)) or not lines:
            raise ValueError('document requires at least one line')
        if not any(isinstance(line, dict) and any(key in line for key in ('item_id','qty','unit_price_cents','unit_cost_cents')) for line in lines):
            return self._make_document(type,date,contact_id,lines,due_date,project_id,external_id,memo=memo)
        normalized, movements, prices, planned = [], [], [], {}
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError('document line must be an object')
            price_key = 'unit_price_cents' if type == 'invoice' else 'unit_cost_cents'
            if 'item_id' not in line:
                if set(line) - {'description','amount_cents','account','project_id','qty',price_key}:
                    raise ValueError('unsupported plain document line fields')
                if 'qty' in line or price_key in line:
                    qty = _quantity(line.get('qty'))
                    price = _cents(line.get(price_key),positive=True)
                    amount = _cents(_round_cents(qty*Decimal(price)),positive=True)
                    if 'amount_cents' in line and _cents(line['amount_cents'])!=amount:
                        raise ValueError('item line amount does not equal quantity times unit price')
                    acct = self._account(line.get('account','4000' if type=='invoice' else '6300'))
                    if self._is_inventory_account(acct):
                        raise ValueError('use buy_inventory/sell_inventory for inventory document lines')
                    normalized.append(dict(description=line.get('description','Service' if type=='invoice' else 'Expense'),
                                           amount_cents=amount,account=acct['code'],qty=_decimal_text(qty),
                                           project_id=line.get('project_id',project_id)))
                    prices.append({price_key:price})
                else:
                    acct = self._account(line.get('account','4000' if type=='invoice' else '6300'))
                    if self._is_inventory_account(acct):
                        raise ValueError('use buy_inventory/sell_inventory for inventory document lines')
                    normalized.append(dict(line))
                    prices.append(None)
                continue
            if set(line) - {'item_id','qty',price_key,'description','project_id','amount_cents','account'}:
                raise ValueError('unsupported inventory document line fields')
            item = self._item(line['item_id'])
            self._inventory_date(item['id'], date)
            qty = _quantity(line.get('qty'))
            price = _cents(line.get(price_key), positive=True)
            amount = _cents(_round_cents(qty*Decimal(price)), positive=True)
            if 'amount_cents' in line and _cents(line['amount_cents']) != amount:
                raise ValueError('item line amount does not equal quantity times unit price')
            account = item['income_account'] if type == 'invoice' else item['inventory_account']
            if 'account' in line and str(line['account']) != account:
                raise ValueError('item line account must match the inventory item mapping')
            prepared = dict(description=line.get('description',item['name']), amount_cents=amount, account=account,
                            item_id=item['id'], qty=_decimal_text(qty), project_id=line.get('project_id',project_id))
            if item['id'] not in planned:
                planned[item['id']] = self._inventory_balance(item['id'])
            on_hand, value = planned[item['id']]
            if type == 'invoice':
                if qty > on_hand:
                    raise ValueError('negative inventory is not supported')
                cost = value if qty == on_hand else _round_cents(Decimal(value)*qty/on_hand)
                prepared.update(cogs_cents=cost, cogs_account_id=item['cogs_account_id'], inventory_account_id=item['inventory_account_id'])
                planned[item['id']] = (on_hand-qty,value-cost)
                movements.append(dict(item_id=item['id'],qty=_decimal_text(-qty),value_cents=-cost,type='sale'))
            else:
                planned[item['id']] = (on_hand+qty,value+amount)
                movements.append(dict(item_id=item['id'],qty=_decimal_text(qty),value_cents=amount,type='purchase'))
            normalized.append(prepared)
            prices.append({price_key:price})
        document = self._make_document(type,date,contact_id,normalized,due_date,project_id,external_id,inventory=True,memo=memo)
        for movement in movements:
            self.conn.execute('INSERT INTO inventory_movements(item_id,date,qty,value_cents,type,entry_id,document_id) VALUES(?,?,?,?,?,?,?)',
                              (movement['item_id'],date,movement['qty'],movement['value_cents'],movement['type'],document['entry_id'],document['id']))
        metadata = [dict(line_id=line['id'],**price) for line,price in zip(document['lines'],prices) if price is not None]
        self._audit('document_item_prices',dict(document_id=document['id'],lines=metadata))
        self._price_metadata = None
        result = self._document(document['id'])
        result['inventory_cost_cents'] = -sum(m['value_cents'] for m in movements if m['type']=='sale')
        return result

    def _item_price_metadata(self):
        if self._price_metadata is None:
            metadata = {}
            for row in self.conn.execute("SELECT data_json FROM audit WHERE action='document_item_prices' ORDER BY id"):
                data = json.loads(row['data_json'])
                for line in data['lines']:
                    metadata[line['line_id']] = {key:value for key,value in line.items() if key!='line_id'}
            self._price_metadata = metadata
        return self._price_metadata

    def _document(self, document_id, as_of=None):
        _id(document_id, 'document_id')
        row = self.conn.execute('SELECT d.*,c.name contact_name FROM documents d JOIN contacts c ON c.id=d.contact_id WHERE d.id=?',
                                (document_id,)).fetchone()
        if row is None:
            raise ValueError('unknown document')
        if as_of and row['date'] > as_of:
            raise ValueError('document does not exist as of requested date')
        result = dict(row)
        payment_sql = 'SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE document_id=?'
        args = [document_id]
        if as_of:
            payment_sql += ' AND date<=?'
            args.append(as_of)
        result['paid_cents'] = self.conn.execute(payment_sql, args).fetchone()[0]
        result['outstanding_cents'] = result['total_cents'] - result['paid_cents']
        result['status'] = 'paid' if result['outstanding_cents'] == 0 else ('partial' if result['paid_cents'] else 'unpaid')
        result['lines'] = [dict(r) for r in self.conn.execute('''SELECT l.*,a.code account FROM document_lines l
          JOIN accounts a ON a.id=l.account_id WHERE l.document_id=? ORDER BY l.id''', (document_id,))]
        metadata = self._item_price_metadata()
        for line in result['lines']:
            if line['id'] in metadata:
                line.update(metadata[line['id']])
        if self._document_detail_metadata is None:
            details = {}
            for record in self.conn.execute("SELECT data_json FROM audit WHERE action='document_details' ORDER BY id"):
                data = json.loads(record['data_json'])
                details[data['document_id']] = data
            self._document_detail_metadata = details
        detail = self._document_detail_metadata.get(document_id,{})
        result['memo'] = detail.get('memo','')
        result['contact_snapshot'] = detail.get('contact_snapshot')
        return result

    @_consistent_read
    def get_invoice(self, invoice_id, as_of=None):
        if as_of:
            _date(as_of)
        result = self._document(invoice_id, as_of)
        if result['type'] != 'invoice':
            raise ValueError('document is not an invoice')
        return result

    @_consistent_read
    def get_bill(self, bill_id, as_of=None):
        if as_of:
            _date(as_of)
        result = self._document(bill_id, as_of)
        if result['type'] != 'bill':
            raise ValueError('document is not a bill')
        return result

    def _payment(self, date, document_id, amount_cents, bank_account, type, external_id):
        self._open_date(date)
        document = self._document(document_id)
        if document['type'] != type:
            raise ValueError(f'document is not a {type}')
        if date < document['date']:
            raise ValueError('payment cannot precede document date')
        amount_cents = _cents(amount_cents, positive=True)
        if amount_cents > document['outstanding_cents']:
            raise ValueError('payment exceeds outstanding amount')
        bank = self._account(bank_account, {'asset'})
        if bank['code'] in ('1100', '1200'):
            raise ValueError('bank account cannot be AR or Inventory')
        project = document['project_id']
        lines = ([dict(account=bank_account, debit_cents=amount_cents, project_id=project),
                  dict(account='1100', credit_cents=amount_cents, project_id=project)] if type == 'invoice' else
                 [dict(account='2000', debit_cents=amount_cents, project_id=project),
                  dict(account=bank_account, credit_cents=amount_cents, project_id=project)])
        entry = self._post(date, f'Payment for {type} {document_id}', lines, 'payment', external_id)
        cur = self.conn.execute('INSERT INTO payments(document_id,date,amount_cents,bank_account_id,entry_id,external_id) VALUES(?,?,?,?,?,?)',
                                (document_id, date, amount_cents, bank['id'], entry['id'], external_id))
        return dict(id=cur.lastrowid, document_id=document_id, date=date, amount_cents=amount_cents,
                    bank_account=bank_account, entry_id=entry['id'], outstanding_cents=document['outstanding_cents']-amount_cents)

    def receive_payment(self, date, invoice_id, amount_cents, bank_account='1000', external_id=None):
        _date(date)
        _cents(amount_cents, positive=True)
        return self._run('receive_payment', dict(date=date, invoice_id=invoice_id, amount_cents=amount_cents, bank_account=bank_account),
                         external_id, lambda: self._payment(date, invoice_id, amount_cents, bank_account, 'invoice', external_id))

    def pay_bill(self, date, bill_id, amount_cents, bank_account='1000', external_id=None):
        _date(date)
        _cents(amount_cents, positive=True)
        return self._run('pay_bill', dict(date=date, bill_id=bill_id, amount_cents=amount_cents, bank_account=bank_account),
                         external_id, lambda: self._payment(date, bill_id, amount_cents, bank_account, 'bill', external_id))

    def allocate_payments(self, date, contact_id, allocations, bank_account='1000', external_id=None):
        """Pay selected bills or receive selected invoices atomically."""
        _date(date)
        _id(contact_id,'contact_id')
        if not isinstance(allocations,list) or not allocations or len(allocations)>500:
            raise ValueError('allocations must contain 1 to 500 document payments')
        ids = []
        for allocation in allocations:
            if not isinstance(allocation,dict) or set(allocation)!={'document_id','amount_cents'}:
                raise ValueError('allocation requires document_id and amount_cents only')
            ids.append(_id(allocation['document_id'],'document_id'))
            _cents(allocation['amount_cents'],positive=True)
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate document in payment allocations')
        def pay_selected():
            documents = [self._document(id) for id in ids]
            if any(doc['contact_id']!=contact_id for doc in documents):
                raise ValueError('payment allocations must belong to the selected contact')
            types = {doc['type'] for doc in documents}
            if len(types)!=1:
                raise ValueError('payment allocations cannot mix invoices and bills')
            type = next(iter(types))
            total = _cents(sum(row['amount_cents'] for row in allocations),positive=True)
            payments = [self._payment(date,row['document_id'],row['amount_cents'],bank_account,type,
                        f'{external_id}:{row["document_id"]}' if external_id else None) for row in allocations]
            return dict(date=date,contact_id=contact_id,type=type,bank_account=bank_account,total_cents=total,payments=payments)
        return self._run('allocate_payments',dict(date=date,contact_id=contact_id,allocations=allocations,bank_account=bank_account),
                         external_id,pay_selected)

    def pay_bills(self, date, allocations, bank_account='1000', external_id=None):
        """Pay selected supplier bills, including different vendors, atomically."""
        _date(date)
        if not isinstance(allocations,list) or not allocations or len(allocations)>500:
            raise ValueError('allocations must contain 1 to 500 document payments')
        ids = []
        for allocation in allocations:
            if not isinstance(allocation,dict) or set(allocation)!={'document_id','amount_cents'}:
                raise ValueError('allocation requires document_id and amount_cents only')
            ids.append(_id(allocation['document_id'],'document_id'))
            _cents(allocation['amount_cents'],positive=True)
        if len(set(ids))!=len(ids):
            raise ValueError('duplicate document in payment allocations')
        def pay_selected():
            documents = [self._document(id) for id in ids]
            if any(document['type']!='bill' for document in documents):
                raise ValueError('pay_bills accepts supplier bills only')
            total = _cents(sum(row['amount_cents'] for row in allocations),positive=True)
            payments = [self._payment(date,row['document_id'],row['amount_cents'],bank_account,'bill',
                         f'{external_id}:{row["document_id"]}' if external_id else None) for row in allocations]
            return dict(date=date,type='bill',bank_account=bank_account,total_cents=total,payments=payments,
                        vendor_ids=sorted({document['contact_id'] for document in documents}))
        return self._run('pay_bills',dict(date=date,allocations=allocations,bank_account=bank_account),external_id,pay_selected)

    def _inventory_balance(self, item_id, as_of=None):
        sql = 'SELECT qty,value_cents FROM inventory_movements WHERE item_id=?'
        args = [item_id]
        if as_of:
            sql += ' AND date<=?'
            args.append(as_of)
        qty, value = Decimal(0), 0
        for row in self.conn.execute(sql, args):
            qty += Decimal(row['qty'])
            value += row['value_cents']
        return qty, value

    def _inventory_date(self, item_id, date):
        last = self.conn.execute('SELECT MAX(date) FROM inventory_movements WHERE item_id=?', (item_id,)).fetchone()[0]
        if last and date < last:
            raise ValueError(f'inventory backdating is unsupported; item latest movement is {last}')

    def buy_inventory(self, date, item_id, qty, unit_cost_cents, offset_account='2000', contact_id=None, external_id=None):
        _date(date)
        qty = _quantity(qty)
        _cents(unit_cost_cents, positive=True)
        amount = _cents(_round_cents(qty * Decimal(unit_cost_cents)), positive=True)
        def purchase():
            self._open_date(date)
            item = self._item(item_id)
            self._inventory_date(item_id, date)
            offset = self._account(offset_account, {'asset', 'liability', 'equity'})
            if self._is_inventory_account(offset) or offset['code'] == '1100':
                raise ValueError('invalid inventory purchase offset account')
            bill = None
            if offset['code'] == '2000':
                if contact_id is None:
                    row = self.conn.execute("SELECT id FROM contacts WHERE name='Inventory Supplier' AND kind='vendor' ORDER BY id LIMIT 1").fetchone()
                    if row:
                        vendor_id = row[0]
                    else:
                        vendor_id = self.conn.execute("INSERT INTO contacts(name,kind) VALUES('Inventory Supplier','vendor')").lastrowid
                else:
                    vendor_id = contact_id
                bill = self._make_document('bill', date, vendor_id,
                      [dict(description=f'Inventory {item["sku"]}: {_decimal_text(qty)}', amount_cents=amount,
                            account=item['inventory_account'], item_id=item_id, qty=_decimal_text(qty))], date, None, external_id, True)
                entry_id = bill['entry_id']
            else:
                entry = self._post(date, f'Buy inventory {item["sku"]}',
                          [dict(account=item['inventory_account'], debit_cents=amount),
                           dict(account=offset_account, credit_cents=amount)], 'inventory_purchase', external_id)
                entry_id = entry['id']
            cur = self.conn.execute('INSERT INTO inventory_movements(item_id,date,qty,value_cents,type,entry_id,document_id) VALUES(?,?,?,?,?,?,?)',
                                    (item_id, date, _decimal_text(qty), amount, 'purchase', entry_id, bill['id'] if bill else None))
            return dict(id=cur.lastrowid, item_id=item_id, date=date, qty=_decimal_text(qty), unit_cost_cents=unit_cost_cents,
                        amount_cents=amount, entry_id=entry_id, bill_id=bill['id'] if bill else None,
                        item=self._item(item_id))
        return self._run('buy_inventory', dict(date=date, item_id=item_id, qty=_decimal_text(qty), unit_cost_cents=unit_cost_cents,
                         offset_account=offset_account, contact_id=contact_id), external_id, purchase)

    def sell_inventory(self, date, item_id, qty, unit_price_cents, contact_id, project_id=None, external_id=None):
        _date(date)
        qty = _quantity(qty)
        _cents(unit_price_cents, positive=True)
        amount = _cents(_round_cents(qty * Decimal(unit_price_cents)), positive=True)
        def sale():
            self._open_date(date)
            item = self._item(item_id)
            self._inventory_date(item_id, date)
            on_hand, value = self._inventory_balance(item_id)
            if qty > on_hand:
                raise ValueError('negative inventory is not supported')
            cost = value if qty == on_hand else _round_cents(Decimal(value) * qty / on_hand)
            invoice = self._make_document('invoice', date, contact_id,
                       [dict(description=f'{item["name"]}: {_decimal_text(qty)}', amount_cents=amount,
                             account=item['income_account'], item_id=item_id, qty=_decimal_text(qty), cogs_cents=cost,
                             cogs_account_id=item['cogs_account_id'], inventory_account_id=item['inventory_account_id'])],
                       date, project_id, external_id, True)
            self.conn.execute('INSERT INTO inventory_movements(item_id,date,qty,value_cents,type,entry_id,document_id) VALUES(?,?,?,?,?,?,?)',
                              (item_id, date, _decimal_text(-qty), -cost, 'sale', invoice['entry_id'], invoice['id']))
            invoice.update(inventory_cost_cents=cost, item_id=item_id, qty=_decimal_text(qty), item=self._item(item_id))
            return invoice
        return self._run('sell_inventory', dict(date=date, item_id=item_id, qty=_decimal_text(qty), unit_price_cents=unit_price_cents,
                         contact_id=contact_id, project_id=project_id), external_id, sale)

    @_consistent_read
    def inventory_report(self, as_of=None):
        if as_of:
            _date(as_of)
        rows = []
        for item in self.conn.execute('SELECT id FROM items ORDER BY id'):
            detail = self._item(item['id'])
            qty, value = self._inventory_balance(item['id'], as_of)
            detail.update(qty=_decimal_text(qty), value_cents=value,
                          average_unit_cost_cents=_decimal_text(Decimal(value)/qty) if qty else '0')
            rows.append(detail)
        total = sum(r['value_cents'] for r in rows)
        account_ids = {self._account('1200')['id']}
        account_ids.update(r['inventory_account_id'] for r in rows)
        ledger_value = 0
        for aid in account_ids:
            ledger_value += self.conn.execute('''SELECT COALESCE(SUM(l.debit_cents-l.credit_cents),0)
              FROM journal_lines l JOIN entries e ON e.id=l.entry_id WHERE l.account_id=? AND e.date<=?''',
              (aid, as_of or '9999-12-31')).fetchone()[0]
        return dict(as_of=as_of, rows=rows, total_value_cents=total, ledger_value_cents=ledger_value,
                    subledger_difference_cents=ledger_value-total)

    def import_bank_csv(self, contents, bank_account='1000'):
        if not isinstance(contents, str):
            raise ValueError('CSV must be a string or filesystem path')
        if '\n' not in contents and len(contents) < 4096 and os.path.isfile(contents):
            contents = Path(contents).read_text(encoding='utf-8-sig')
        reader = csv.DictReader(io.StringIO(contents.lstrip('\ufeff')))
        if reader.fieldnames is None:
            raise ValueError('CSV is empty')
        parsed, occurrences = [], {}
        for number, original in enumerate(reader, 2):
            row = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k,v in original.items() if k is not None}
            try:
                date = _date(row.get('date'))
                description = _text(row.get('description', row.get('memo', 'Bank transaction')), 'description', 1000)
                if 'amount_cents' in row:
                    if not re.fullmatch(r'-?\d+', row['amount_cents'] or ''):
                        raise ValueError('amount_cents must be an integer string')
                    amount = _cents(int(row['amount_cents']))
                elif 'amount' in row:
                    amount_text = row['amount'].replace(',', '')
                    if not re.fullmatch(r'-?\d+(\.\d{1,2})?', amount_text):
                        raise ValueError('amount must have at most two decimal places')
                    amount = _cents(int(Decimal(amount_text) * 100))
                else:
                    raise ValueError('CSV requires amount_cents or amount')
                if amount == 0:
                    raise ValueError('bank amount must be nonzero')
                signature = f'{date}\x00{description}\x00{amount}'
                occurrences[signature] = occurrences.get(signature, 0) + 1
                external_id = row.get('external_id') or hashlib.sha256(f'{signature}\x00{occurrences[signature]}'.encode()).hexdigest()
                external_id = _text(external_id, 'bank external_id', 200)
                parsed.append(dict(date=date, description=description, amount_cents=amount, external_id=external_id))
            except (ValueError, TypeError) as exc:
                raise ValueError(f'CSV line {number}: {exc}') from exc
        def import_rows():
            bank = self._account(bank_account, {'asset'})
            if bank['code'] in ('1100', '1200'):
                raise ValueError('bank account cannot be AR or Inventory')
            imported, duplicates, result_rows = 0, 0, []
            for row in parsed:
                previous = self.conn.execute('SELECT * FROM bank_transactions WHERE bank_account_id=? AND external_id=?',
                                             (bank['id'], row['external_id'])).fetchone()
                if previous:
                    if any(previous[k] != row[k] for k in ('date','description','amount_cents')):
                        raise ValueError('bank external_id already used with different transaction data')
                    duplicates += 1
                    result_rows.append(dict(previous))
                else:
                    cur = self.conn.execute('INSERT INTO bank_transactions(bank_account_id,date,description,amount_cents,external_id) VALUES(?,?,?,?,?)',
                                            (bank['id'], row['date'], row['description'], row['amount_cents'], row['external_id']))
                    imported += 1
                    result_rows.append(dict(self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (cur.lastrowid,)).fetchone()))
            return dict(imported_count=imported, duplicate_count=duplicates, rows=result_rows)
        return self._run('import_bank_csv', dict(bank_account=bank_account, row_count=len(parsed)), None, import_rows)

    def classify_bank_transaction(self, transaction_id, account, project_id=None, memo=None):
        _id(transaction_id, 'transaction_id')
        def classify():
            row = self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (transaction_id,)).fetchone()
            if row is None:
                raise ValueError('unknown bank transaction')
            if row['status'] != 'pending':
                raise ValueError('bank transaction already classified or matched')
            bank = self.conn.execute('SELECT code FROM accounts WHERE id=?', (row['bank_account_id'],)).fetchone()[0]
            counter = self._account(account)
            if counter['id'] == row['bank_account_id']:
                raise ValueError('counter account must differ from bank')
            amount = row['amount_cents']
            lines = ([dict(account=bank, debit_cents=amount, project_id=project_id),
                      dict(account=account, credit_cents=amount, project_id=project_id)] if amount > 0 else
                     [dict(account=account, debit_cents=-amount, project_id=project_id),
                      dict(account=bank, credit_cents=-amount, project_id=project_id)])
            entry = self._post(row['date'], memo or row['description'], lines, 'bank_import')
            bank_line = next(l for l in entry['lines'] if l['account'] == bank)
            self.conn.execute("UPDATE bank_transactions SET status='classified',entry_line_id=? WHERE id=?", (bank_line['id'], transaction_id))
            return dict(id=transaction_id, entry_id=entry['id'], entry_line_id=bank_line['id'], status='classified')
        return self._run('classify_bank_transaction', dict(transaction_id=transaction_id, account=account, project_id=project_id, memo=memo), None, classify)

    def match_bank_transaction(self, transaction_id, entry_line_id):
        _id(transaction_id, 'transaction_id')
        _id(entry_line_id, 'entry_line_id')
        def match():
            row = self.conn.execute('SELECT * FROM bank_transactions WHERE id=?', (transaction_id,)).fetchone()
            line = self.conn.execute('SELECT l.*,e.date FROM journal_lines l JOIN entries e ON e.id=l.entry_id WHERE l.id=?', (entry_line_id,)).fetchone()
            if row is None or line is None:
                raise ValueError('unknown transaction or journal line')
            if row['status'] != 'pending':
                if row['entry_line_id'] == entry_line_id:
                    return dict(id=transaction_id, entry_line_id=entry_line_id, status=row['status'])
                raise ValueError('bank transaction already matched')
            if row['bank_account_id'] != line['account_id'] or row['amount_cents'] != line['debit_cents']-line['credit_cents']:
                raise ValueError('bank transaction account or amount does not match journal line')
            if line['date'] > row['date']:
                raise ValueError('journal date cannot be later than bank date')
            if self.conn.execute('SELECT 1 FROM bank_transactions WHERE entry_line_id=?', (entry_line_id,)).fetchone():
                raise ValueError('journal line already matched to another bank transaction')
            self.conn.execute("UPDATE bank_transactions SET status='matched',entry_line_id=? WHERE id=?", (entry_line_id, transaction_id))
            return dict(id=transaction_id, entry_line_id=entry_line_id, status='matched')
        return self._run('match_bank_transaction', dict(transaction_id=transaction_id, entry_line_id=entry_line_id), None, match)

    @_consistent_read
    def bank_register(self, bank_account='1000', as_of=None, uncleared_only=False):
        if as_of:
            _date(as_of)
        bank = self._account(bank_account, {'asset'})
        sql = '''SELECT l.id,l.entry_id,e.date,e.memo,l.debit_cents,l.credit_cents,
          l.debit_cents-l.credit_cents amount_cents,r.statement_date reconciled_statement_date
          FROM journal_lines l JOIN entries e ON e.id=l.entry_id
          LEFT JOIN reconciliation_lines rl ON rl.entry_line_id=l.id
          LEFT JOIN reconciliations r ON r.id=rl.reconciliation_id WHERE l.account_id=?'''
        args = [bank['id']]
        if as_of:
            sql += ' AND e.date<=?'
            args.append(as_of)
        sql += ' ORDER BY e.date,e.id,l.id'
        rows, running = [], 0
        for r in self.conn.execute(sql, args):
            row = dict(r)
            running += row['amount_cents']
            row['running_balance_cents'] = running
            row['cleared'] = bool(row['reconciled_statement_date'] and (not as_of or row['reconciled_statement_date'] <= as_of))
            if not uncleared_only or not row['cleared']:
                rows.append(row)
        return dict(bank_account=bank_account, as_of=as_of, balance_cents=running, rows=rows)

    def reconcile(self, bank_account, statement_date, ending_balance_cents, entry_line_ids):
        _date(statement_date)
        _cents(ending_balance_cents)
        if not isinstance(entry_line_ids, (list, tuple)) or len(set(entry_line_ids)) != len(entry_line_ids):
            raise ValueError('entry_line_ids must be a list without duplicates')
        for line_id in entry_line_ids:
            _id(line_id, 'entry_line_id')
        def perform():
            bank = self._account(bank_account, {'asset'})
            previous = self.conn.execute('SELECT * FROM reconciliations WHERE bank_account_id=? ORDER BY statement_date DESC LIMIT 1',
                                         (bank['id'],)).fetchone()
            if previous and statement_date <= previous['statement_date']:
                raise ValueError('statement dates must increase; previous reconciliations cannot be replaced')
            previous_balance = previous['ending_balance_cents'] if previous else 0
            new_total = 0
            for line_id in entry_line_ids:
                line = self.conn.execute('SELECT l.*,e.date FROM journal_lines l JOIN entries e ON e.id=l.entry_id WHERE l.id=?', (line_id,)).fetchone()
                if line is None or line['account_id'] != bank['id'] or line['date'] > statement_date:
                    raise ValueError('selected line is not an eligible bank entry for this statement')
                if self.conn.execute('SELECT 1 FROM reconciliation_lines WHERE entry_line_id=?', (line_id,)).fetchone():
                    raise ValueError('selected bank entry was already reconciled')
                new_total += line['debit_cents']-line['credit_cents']
            if previous_balance + new_total != ending_balance_cents:
                raise ValueError(f'reconciliation difference is {ending_balance_cents-previous_balance-new_total} cents')
            cur = self.conn.execute('INSERT INTO reconciliations(bank_account_id,statement_date,ending_balance_cents) VALUES(?,?,?)',
                                    (bank['id'], statement_date, ending_balance_cents))
            self.conn.executemany('INSERT INTO reconciliation_lines(reconciliation_id,entry_line_id) VALUES(?,?)',
                                  [(cur.lastrowid, line_id) for line_id in entry_line_ids])
            register = self.bank_register(bank_account, statement_date, True)
            return dict(id=cur.lastrowid, bank_account=bank_account, statement_date=statement_date,
                        ending_balance_cents=ending_balance_cents, book_balance_cents=register['balance_cents'],
                        outstanding_cents=register['balance_cents']-ending_balance_cents,
                        difference_cents=0, uncleared_rows=register['rows'])
        return self._run('reconcile', dict(bank_account=bank_account, statement_date=statement_date,
                         ending_balance_cents=ending_balance_cents, entry_line_ids=list(entry_line_ids)), None, perform)

    def set_budget(self, period, account, amount_cents, project_id=None):
        if not isinstance(period, str) or not re.fullmatch(r'\d{4}-\d{2}', period):
            raise ValueError('budget period must be YYYY-MM')
        _date(period+'-01')
        _cents(amount_cents)
        if amount_cents < 0:
            raise ValueError('budget amount must be nonnegative in the normal account direction')
        def save():
            acct = self._account(account, {'income', 'expense'})
            self._project(project_id)
            old = self.conn.execute('SELECT id FROM budgets WHERE period=? AND account_id=? AND COALESCE(project_id,0)=?',
                                    (period, acct['id'], project_id or 0)).fetchone()
            if old:
                self.conn.execute('UPDATE budgets SET amount_cents=? WHERE id=?', (amount_cents, old['id']))
                id = old['id']
            else:
                id = self.conn.execute('INSERT INTO budgets(period,account_id,amount_cents,project_id) VALUES(?,?,?,?)',
                                       (period, acct['id'], amount_cents, project_id)).lastrowid
            return dict(id=id, period=period, account=account, amount_cents=amount_cents, project_id=project_id)
        return self._run('set_budget', dict(period=period, account=account, amount_cents=amount_cents, project_id=project_id), None, save)

    def close_period(self, end_date):
        _date(end_date)
        def close():
            previous = self.conn.execute('SELECT MAX(end_date) FROM closed_periods').fetchone()[0]
            if previous and end_date < previous:
                raise ValueError('close date cannot move backwards')
            self.conn.execute('INSERT OR IGNORE INTO closed_periods(end_date) VALUES(?)', (end_date,))
            return dict(closed_through=end_date)
        return self._run('close_period', dict(end_date=end_date), None, close)

    def _balances(self, as_of, basis='accrual', project_id=None):
        _date(as_of)
        if basis not in ('accrual', 'cash'):
            raise ValueError('basis must be accrual or cash')
        self._project(project_id)
        balances = {r['id']: 0 for r in self.conn.execute('SELECT id FROM accounts')}
        sql = '''SELECT l.account_id,SUM(l.debit_cents-l.credit_cents) balance
          FROM journal_lines l JOIN entries e ON e.id=l.entry_id WHERE e.date<=?'''
        args = [as_of]
        if project_id is not None:
            sql += ' AND l.project_id=?'
            args.append(project_id)
        sql += ' GROUP BY l.account_id'
        for row in self.conn.execute(sql, args):
            balances[row['account_id']] = row['balance']
        if basis == 'cash':
            ar_id, ap_id = self._account('1100')['id'], self._account('2000')['id']
            for docrow in self.conn.execute('SELECT id FROM documents WHERE date<=? ORDER BY date,id', (as_of,)):
                doc = self._document(docrow['id'], as_of)
                recognized = _allocate(doc['paid_cents'], [l['amount_cents'] for l in doc['lines']])
                for line, paid in zip(doc['lines'], recognized):
                    if project_id is not None and line['project_id'] != project_id:
                        continue
                    unpaid = line['amount_cents'] - paid
                    aid = line['account_id']
                    if doc['type'] == 'invoice':
                        balances[aid] += unpaid
                        balances[ar_id] -= unpaid
                        if line['cogs_cents']:
                            recognized_cost = _round_cents(Decimal(line['cogs_cents'])*Decimal(doc['paid_cents'])/Decimal(doc['total_cents']))
                            unpaid_cost = line['cogs_cents'] - recognized_cost
                            balances[line['cogs_account_id']] -= unpaid_cost
                            balances[line['inventory_account_id']] += unpaid_cost
                    else:
                        balances[aid] -= unpaid
                        balances[ap_id] += unpaid
        return balances

    @_consistent_read
    def trial_balance(self, as_of, fiscal_year_start=None, retained_account=None):
        """Cumulative verification TB, or explicit calendar-year presentation.

        Default rows are the actual cumulative posted balances used for journal
        verification. Supplying this calendar year's January 1 changes income
        and expense rows to that year's net movements and displays prior-year
        unclosed P/L in an explicitly selected equity account, or in a clearly
        calculated separate row. This is report presentation, not a posting.
        Actual prior-year closing journals have already zeroed prior P/L, so
        their transferred profit is not added a second time.
        """
        balances = self._balances(as_of)
        if fiscal_year_start is None and retained_account is not None:
            raise ValueError('retained_account requires an explicit fiscal_year_start for the annual display')
        previous = None
        prior_year_profit = 0
        retained = None
        if fiscal_year_start is not None:
            _date(fiscal_year_start)
            if fiscal_year_start != as_of[:4]+'-01-01':
                raise ValueError('annual trial balance currently supports only January 1 of the report calendar year')
            ordinal = Date.fromisoformat(fiscal_year_start).toordinal()
            previous = (self._balances(Date.fromordinal(ordinal-1).isoformat())
                        if ordinal > 1 else {key: 0 for key in balances})
            for account in self.conn.execute("SELECT id FROM accounts WHERE kind IN ('income','expense')"):
                prior_year_profit -= previous[account['id']]
            if retained_account is not None:
                retained = self._account(retained_account, {'equity'})
        rows, cumulative_rows = [], []
        for account in self.conn.execute('SELECT * FROM accounts ORDER BY code'):
            signed = balances[account['id']]
            cumulative_rows.append(dict(code=account['code'], account=account['code'], name=account['name'], kind=account['kind'],
                             debit_cents=max(signed, 0), credit_cents=max(-signed, 0), balance_cents=signed))
            if previous is not None:
                if account['kind'] in ('income', 'expense'):
                    signed -= previous[account['id']]
                elif retained is not None and account['id'] == retained['id']:
                    signed -= prior_year_profit
            rows.append(dict(code=account['code'], account=account['code'], name=account['name'], kind=account['kind'],
                             debit_cents=max(signed, 0), credit_cents=max(-signed, 0), balance_cents=signed))
        if previous is not None and retained is None and prior_year_profit:
            signed = -prior_year_profit
            rows.append(dict(code='__prior_year_profit__', account=None, name='历年损益结转', kind='equity',
                             debit_cents=max(signed, 0), credit_cents=max(-signed, 0), balance_cents=signed))
        debit, credit = sum(r['debit_cents'] for r in rows), sum(r['credit_cents'] for r in rows)
        return dict(as_of=as_of, rows=rows, total_debit_cents=debit, total_credit_cents=credit,
                    difference_cents=debit-credit, balanced=debit == credit,
                    report_basis='calendar_year' if previous is not None else 'cumulative',
                    accounting_basis='accrual', fiscal_year_start=fiscal_year_start,
                    fiscal_year_policy='calendar_year' if previous is not None else None,
                    retained_account=retained['code'] if retained is not None else None,
                    prior_year_profit_cents=prior_year_profit if previous is not None else None,
                    cumulative_rows=cumulative_rows)

    @_consistent_read
    def profit_loss(self, start_date, end_date, basis='accrual', project_id=None):
        _date(start_date)
        _date(end_date)
        if start_date > end_date:
            raise ValueError('report start must not exceed end')
        end = self._balances(end_date, basis, project_id)
        previous = Date.fromisoformat(start_date).toordinal()-1
        start = self._balances(Date.fromordinal(previous).isoformat(), basis, project_id) if previous > 0 else {a:0 for a in end}
        rows, income, expenses = [], 0, 0
        for account in self.conn.execute("SELECT * FROM accounts WHERE kind IN ('income','expense') ORDER BY code"):
            debit_balance = end[account['id']]-start[account['id']]
            amount = -debit_balance if account['kind'] == 'income' else debit_balance
            rows.append(dict(code=account['code'], account=account['code'], name=account['name'], kind=account['kind'], amount_cents=amount))
            if account['kind'] == 'income':
                income += amount
            else:
                expenses += amount
        return dict(start_date=start_date, end_date=end_date, basis=basis, project_id=project_id, rows=rows,
                    income_cents=income, expense_cents=expenses, net_profit_cents=income-expenses)

    @_consistent_read
    def balance_sheet(self, as_of, basis='accrual', retained_account=None):
        """Balance sheet with separate calendar-year and accumulated earnings.

        ``rows`` always contain actual posted account balances. ``display_rows``
        additionally present unclosed profit as prior years plus current year.
        An explicitly supplied equity account may absorb prior-year profit in
        display only; no closing journal or underlying balance is changed.
        ``current_earnings_cents`` is the legacy cumulative field, retained for
        compatibility. Use ``current_year_profit_cents`` for this year's P/L.
        Fiscal-year configuration currently supports calendar years only.
        """
        balances = self._balances(as_of, basis)
        rows, assets, liabilities, equity, cumulative_profit = [], 0, 0, 0, 0
        fiscal_year_start = as_of[:4] + '-01-01'
        start_ordinal = Date.fromisoformat(fiscal_year_start).toordinal()
        previous = (self._balances(Date.fromordinal(start_ordinal-1).isoformat(), basis)
                    if start_ordinal > 1 else {key: 0 for key in balances})
        prior_year_profit = 0
        for account in self.conn.execute('SELECT * FROM accounts ORDER BY code'):
            signed = balances[account['id']]
            kind = account['kind']
            if kind in ('income', 'expense'):
                cumulative_profit -= signed
                prior_year_profit -= previous[account['id']]
                continue
            normal = signed if kind == 'asset' else -signed
            rows.append(dict(code=account['code'], account=account['code'], name=account['name'], kind=kind, amount_cents=normal))
            if kind == 'asset':
                assets += normal
            elif kind == 'liability':
                liabilities += normal
            else:
                equity += normal
        current_year_profit = cumulative_profit - prior_year_profit
        display_rows = [dict(row, display_role='posted_account') for row in rows]
        prior_row = dict(code='__prior_year_profit__', account=None, name='历年损益结转',
                         kind='equity', amount_cents=prior_year_profit, display_role='prior_year_profit')
        current_row = dict(code='__current_year_profit__', account=None, name='本年利润',
                           kind='equity', amount_cents=current_year_profit, display_role='current_year_profit')
        if retained_account is not None:
            retained = self._account(retained_account, {'equity'})
            row = next(row for row in display_rows if row['code'] == retained['code'])
            row.update(posted_amount_cents=row['amount_cents'], prior_year_profit_cents=prior_year_profit,
                       amount_cents=row['amount_cents']+prior_year_profit,
                       display_role='explicit_retained_earnings')
        else:
            display_rows.append(prior_row)
        display_rows.append(current_row)
        return dict(as_of=as_of, basis=basis, rows=rows, assets_cents=assets, liabilities_cents=liabilities,
                    equity_cents=equity, current_earnings_cents=cumulative_profit,
                    cumulative_profit_cents=cumulative_profit, current_year_profit_cents=current_year_profit,
                    prior_year_profit_cents=prior_year_profit, fiscal_year_start=fiscal_year_start,
                    fiscal_year_policy='calendar_year', display_rows=display_rows,
                    retained_account=retained['code'] if retained_account is not None else None,
                    earnings_breakdown=[prior_row, current_row],
                    total_equity_cents=equity+cumulative_profit,
                    difference_cents=assets-liabilities-equity-cumulative_profit,
                    balanced=assets == liabilities+equity+cumulative_profit)

    def _aging(self, type, as_of):
        _date(as_of)
        rows = []
        buckets = {'current': 0, '1_30': 0, '31_60': 0, '61_90': 0, 'over_90': 0}
        for row in self.conn.execute('SELECT id FROM documents WHERE type=? AND date<=? ORDER BY due_date,id', (type, as_of)):
            doc = self._document(row['id'], as_of)
            if doc['outstanding_cents'] <= 0:
                continue
            overdue = max(0, (Date.fromisoformat(as_of)-Date.fromisoformat(doc['due_date'])).days)
            bucket = 'current' if overdue == 0 else '1_30' if overdue <= 30 else '31_60' if overdue <= 60 else '61_90' if overdue <= 90 else 'over_90'
            buckets[bucket] += doc['outstanding_cents']
            rows.append(dict(id=doc['id'], document_id=doc['id'], date=doc['date'], due_date=doc['due_date'],
                             contact_id=doc['contact_id'], contact_name=doc['contact_name'], total_cents=doc['total_cents'],
                             paid_cents=doc['paid_cents'], outstanding_cents=doc['outstanding_cents'],
                             days_overdue=overdue, bucket=bucket, project_id=doc['project_id']))
        account = self._account('1100' if type == 'invoice' else '2000')
        gl_balance = self.conn.execute('''SELECT COALESCE(SUM(l.debit_cents-l.credit_cents),0)
          FROM journal_lines l JOIN entries e ON e.id=l.entry_id WHERE l.account_id=? AND e.date<=?''',
          (account['id'], as_of)).fetchone()[0]
        if type == 'bill':
            gl_balance = -gl_balance
        total = sum(buckets.values())
        return dict(as_of=as_of, type=type, rows=rows, buckets=buckets, total_outstanding_cents=total,
                    ledger_balance_cents=gl_balance, subledger_difference_cents=gl_balance-total)

    @_consistent_read
    def ar_aging(self, as_of):
        return self._aging('invoice', as_of)

    @_consistent_read
    def ap_aging(self, as_of):
        return self._aging('bill', as_of)

    @_consistent_read
    def budget_actual(self, start, end, project_id=None):
        _date(start)
        _date(end)
        if start > end:
            raise ValueError('report start must not exceed end')
        if start[-2:] != '01' or end != self._month_end(end[:7]):
            raise ValueError('monthly budgets require whole calendar months')
        actual_report = self.profit_loss(start, end, project_id=project_id)
        actual = {r['code']: r for r in actual_report['rows']}
        sql = '''SELECT a.code,a.name,a.kind,SUM(b.amount_cents) budget_cents FROM budgets b JOIN accounts a ON a.id=b.account_id
          WHERE b.period>=? AND b.period<=? AND COALESCE(b.project_id,0)=? GROUP BY a.id ORDER BY a.code'''
        budgets = {r['code']: dict(r) for r in self.conn.execute(sql, (start[:7], end[:7], project_id or 0))}
        rows = []
        for code in sorted(set(actual)|set(budgets)):
            account = actual.get(code) or budgets[code]
            budget = budgets.get(code, {}).get('budget_cents', 0)
            realized = actual.get(code, {}).get('amount_cents', 0)
            rows.append(dict(code=code, account=code, name=account['name'], kind=account['kind'],
                             budget_cents=budget, actual_cents=realized, variance_cents=realized-budget,
                             favorable_variance_cents=(realized-budget) if account['kind']=='income' else (budget-realized)))
        b_income = sum(r['budget_cents'] for r in rows if r['kind']=='income')
        b_expense = sum(r['budget_cents'] for r in rows if r['kind']=='expense')
        return dict(start_date=start, end_date=end, project_id=project_id, rows=rows, budget_income_cents=b_income,
                    budget_expense_cents=b_expense, budget_profit_cents=b_income-b_expense,
                    actual_income_cents=actual_report['income_cents'], actual_expense_cents=actual_report['expense_cents'],
                    actual_profit_cents=actual_report['net_profit_cents'],
                    profit_variance_cents=actual_report['net_profit_cents']-(b_income-b_expense))

    @staticmethod
    def _month_end(period):
        year, month = map(int, period.split('-'))
        if year == 9999 and month == 12:
            return '9999-12-31'
        following = Date(year+1, 1, 1) if month == 12 else Date(year, month+1, 1)
        return Date.fromordinal(following.toordinal()-1).isoformat()

    def budget_vs_actual(self, period, project_id=None):
        _date(period+'-01')
        return self.budget_actual(period+'-01', self._month_end(period), project_id)

    @_consistent_read
    def project_profitability(self, start, end):
        rows = []
        for project in self.conn.execute('SELECT * FROM projects ORDER BY id'):
            pnl = self.profit_loss(start, end, project_id=project['id'])
            rows.append(dict(id=project['id'], project_id=project['id'], name=project['name'],
                             income_cents=pnl['income_cents'], expense_cents=pnl['expense_cents'],
                             net_profit_cents=pnl['net_profit_cents']))
        total = self.profit_loss(start, end)
        return dict(start_date=start, end_date=end, rows=rows,
                    unassigned_income_cents=total['income_cents']-sum(r['income_cents'] for r in rows),
                    unassigned_expense_cents=total['expense_cents']-sum(r['expense_cents'] for r in rows),
                    total_income_cents=total['income_cents'], total_expense_cents=total['expense_cents'],
                    total_net_profit_cents=total['net_profit_cents'])

    @_consistent_read
    def account_listing(self, as_of=None):
        """Chart of accounts with signed and normal-direction balances."""
        as_of = as_of or self.conn.execute('SELECT MAX(date) FROM entries').fetchone()[0] or Date.today().isoformat()
        balances = self._balances(as_of)
        rows = []
        for account in self.conn.execute('SELECT * FROM accounts ORDER BY code'):
            signed = balances[account['id']]
            rows.append(dict(account,account=account['code'],balance_cents=signed,
                             normal_balance_cents=signed if account['kind'] in ('asset','expense') else -signed))
        return dict(as_of=as_of,rows=rows,accounting_basis='accrual',read_only=self.read_only)

    @_consistent_read
    def general_ledger(self, start_date, end_date, account=None):
        """Actual posted journal lines, not invented invoice-level transactions."""
        _date(start_date)
        _date(end_date)
        if start_date>end_date:
            raise ValueError('report start must not exceed end')
        accounts = list(self.conn.execute('SELECT * FROM accounts ORDER BY code'))
        if account is not None:
            selected = self._account(account)
            accounts = [selected]
        ordinal = Date.fromisoformat(start_date).toordinal()
        opening = self._balances(Date.fromordinal(ordinal-1).isoformat()) if ordinal>1 else {a['id']:0 for a in accounts}
        closing = self._balances(end_date)
        groups, flat_rows = [], []
        for acct in accounts:
            running = opening[acct['id']]
            rows = []
            query = '''SELECT l.id line_id,l.entry_id,l.project_id,l.debit_cents,l.credit_cents,
              e.date,e.memo,e.source,e.external_id,d.id document_id,d.type document_type,
              COALESCE(c.name,pc.name,'') name,p.id payment_id,p.document_id paid_document_id,pd.type paid_document_type
              FROM journal_lines l JOIN entries e ON e.id=l.entry_id
              LEFT JOIN documents d ON d.entry_id=e.id LEFT JOIN contacts c ON c.id=d.contact_id
              LEFT JOIN payments p ON p.entry_id=e.id LEFT JOIN documents pd ON pd.id=p.document_id
              LEFT JOIN contacts pc ON pc.id=pd.contact_id
              WHERE l.account_id=? AND e.date>=? AND e.date<=? ORDER BY e.date,e.id,l.id'''
            for item in self.conn.execute(query,(acct['id'],start_date,end_date)):
                row = dict(item)
                amount = row['debit_cents']-row['credit_cents']
                running += amount
                splits = self.conn.execute('''SELECT a.code,a.name FROM journal_lines l JOIN accounts a ON a.id=l.account_id
                  WHERE l.entry_id=? AND l.id<>? ORDER BY l.id''',(row['entry_id'],row['line_id'])).fetchall()
                if row['source']=='payment':
                    type = 'Payment' if row['paid_document_type']=='invoice' else 'Bill Pmt'
                else:
                    type = {'journal':'General Journal','invoice':'Invoice','bill':'Bill','reversal':'Reversal',
                            'bank_import':'Bank Transaction','inventory_purchase':'Inventory Purchase'}.get(row['source'],row['source'])
                document_id = row['document_id'] or row['paid_document_id']
                row.update(account=acct['code'],account_code=acct['code'],account_name=acct['name'],kind=acct['kind'],
                           type=type,num=str(document_id or row['entry_id']),amount_cents=amount,
                           balance_cents=running,running_balance_cents=running,
                           split=splits[0]['name'] if len(splits)==1 else '-SPLIT-',
                           split_account=splits[0]['code'] if len(splits)==1 else None)
                rows.append(row)
            group = dict(code=acct['code'],account=acct['code'],name=acct['name'],kind=acct['kind'],
                         opening_balance_cents=opening[acct['id']],closing_balance_cents=closing[acct['id']],
                         total_debit_cents=sum(r['debit_cents'] for r in rows),
                         total_credit_cents=sum(r['credit_cents'] for r in rows),
                         difference_cents=running-closing[acct['id']],rows=rows)
            groups.append(group)
            flat_rows.extend(rows)
        return dict(start_date=start_date,end_date=end_date,account=account,rows=flat_rows,accounts=groups,
                    total_debit_cents=sum(r['debit_cents'] for r in flat_rows),
                    total_credit_cents=sum(r['credit_cents'] for r in flat_rows),
                    source_kind='posted_journal_lines',accounting_basis='accrual',read_only=self.read_only)

    @_consistent_read
    def contact_center(self, kind, as_of=None, contact_id=None):
        """Real customer/vendor documents and open balances, with no invented master fields."""
        if kind not in ('customer','vendor'):
            raise ValueError('contact center kind must be customer or vendor')
        as_of = as_of or self.conn.execute('SELECT MAX(date) FROM entries').fetchone()[0] or Date.today().isoformat()
        _date(as_of)
        contacts = list(self.conn.execute('SELECT * FROM contacts WHERE kind IN (?,\'both\') ORDER BY name,id',(kind,)))
        if contact_id is not None:
            self._contact(contact_id,kind)
            contacts = [row for row in contacts if row['id']==contact_id]
        type = 'invoice' if kind=='customer' else 'bill'
        rows, documents, payments = [], [], []
        for contact in contacts:
            docs = [self._document(row['id'],as_of) for row in self.conn.execute(
                'SELECT id FROM documents WHERE contact_id=? AND type=? AND date<=? ORDER BY date,id',(contact['id'],type,as_of))]
            total = sum(doc['outstanding_cents'] for doc in docs)
            rows.append(dict(self._contact_record(contact['id']),contact_id=contact['id'],open_balance_cents=total,
                             documents_count=len(docs),unpaid_count=sum(doc['outstanding_cents']>0 for doc in docs)))
            documents.extend(docs)
            payments.extend(dict(row) for row in self.conn.execute('''SELECT p.*,d.contact_id,d.type document_type
              FROM payments p JOIN documents d ON d.id=p.document_id
              WHERE d.contact_id=? AND d.type=? AND p.date<=? ORDER BY p.date,p.id''',(contact['id'],type,as_of)))
        return dict(kind=kind,as_of=as_of,contact_id=contact_id,rows=rows,documents=documents,payments=payments,
                    total_outstanding_cents=sum(r['open_balance_cents'] for r in rows),read_only=self.read_only)

    @_consistent_read
    def audit_log(self):
        result = []
        for row in self.conn.execute('SELECT * FROM audit ORDER BY id'):
            item = dict(row)
            item['data'] = json.loads(item.pop('data_json'))
            result.append(item)
        return result

    def backup(self, destination):
        destination = str(destination)
        if destination == ':memory:' or (self.path != ':memory:' and Path(destination).resolve() == Path(self.path).resolve()):
            raise ValueError('backup must use a different filesystem path')
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError as exc:
            raise ValueError('backup destination already exists; choose a new filename') from exc
        with self._lock:
            try:
                backup = sqlite3.connect(destination)
                try:
                    self.conn.backup(backup)
                finally:
                    backup.close()
            except Exception:
                Path(destination).unlink(missing_ok=True)
                raise
        return dict(path=str(Path(destination).resolve()), bytes=Path(destination).stat().st_size)

    @_consistent_read
    def export_iif(self):
        """General-journal IIF text only. Not a full QuickBooks company export."""
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(['!TRNS','TRNSID','TRNSTYPE','DATE','ACCNT','AMOUNT','MEMO'])
        writer.writerow(['!SPL','SPLID','TRNSTYPE','DATE','ACCNT','AMOUNT','MEMO'])
        writer.writerow(['!ENDTRNS'])
        for entry in self.conn.execute('SELECT * FROM entries ORDER BY date,id'):
            lines = self._entry(entry['id'])['lines']
            for index, line in enumerate(lines):
                amount = line['debit_cents']-line['credit_cents']
                money = f'{"-" if amount<0 else ""}{abs(amount)//100}.{abs(amount)%100:02d}'
                stamp = Date.fromisoformat(entry['date']).strftime('%m/%d/%Y')
                clean_memo = entry['memo'].replace('\t',' ').replace('\n',' ')
                writer.writerow(['TRNS' if index == 0 else 'SPL', line['id'], 'GENERAL JOURNAL', stamp,
                                 line['account_name'], money, clean_memo])
            writer.writerow(['ENDTRNS'])
        return stream.getvalue()

    @_consistent_read
    def list_data(self):
        with self._lock:
            return dict(
                accounts=[dict(r) for r in self.conn.execute('SELECT * FROM accounts ORDER BY code')],
                contacts=[self._contact_record(r['id']) for r in self.conn.execute('SELECT id FROM contacts ORDER BY id')],
                projects=[dict(r) for r in self.conn.execute('SELECT * FROM projects ORDER BY id')],
                items=self.inventory_report()['rows'],
                invoices=[self._document(r['id']) for r in self.conn.execute("SELECT id FROM documents WHERE type='invoice' ORDER BY date,id")],
                bills=[self._document(r['id']) for r in self.conn.execute("SELECT id FROM documents WHERE type='bill' ORDER BY date,id")],
                payments=[dict(r) for r in self.conn.execute('SELECT * FROM payments ORDER BY date,id')],
                journals=[self._entry(r['id']) for r in self.conn.execute('SELECT id FROM entries ORDER BY date,id')],
                bank_transactions=[dict(r) for r in self.conn.execute('SELECT b.*,a.code bank_account FROM bank_transactions b JOIN accounts a ON a.id=b.bank_account_id ORDER BY b.date,b.id')],
                budgets=[dict(r) for r in self.conn.execute('SELECT b.*,a.code account FROM budgets b JOIN accounts a ON a.id=b.account_id ORDER BY b.period,a.code')],
                reconciliations=[dict(r) for r in self.conn.execute('SELECT * FROM reconciliations ORDER BY statement_date,id')],
                closed_through=self.conn.execute('SELECT MAX(end_date) FROM closed_periods').fetchone()[0])

    @_consistent_read
    def status(self):
        latest = self.conn.execute('SELECT MAX(date) FROM entries').fetchone()[0]
        tables = ('accounts','contacts','projects','items','entries','documents','payments','inventory_movements','bank_transactions','audit')
        counts = {table: self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}
        closed = self.conn.execute('SELECT MAX(end_date) FROM closed_periods').fetchone()[0]
        return dict(path=self.path, counts=counts, latest_entry_date=latest, schema_version=1, currency='USD',
                    closed_until=closed,read_only=self.read_only,
                    closed_through=closed,
                    integrity=self.conn.execute('PRAGMA integrity_check').fetchone()[0],
                    basis_policy='cash is a management receipts/payments view, not statutory tax accounting')

    def close(self):
        with self._lock:
            self.conn.close()
