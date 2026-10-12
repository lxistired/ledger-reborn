# Local accounting engine

`ledger.py` is an independently written Python 3 engine using only the standard
library. It records transactions in a persistent SQLite company database. It
does not contain Intuit source code, read a QuickBooks company file, or call an
Intuit service. Original-product reverse-engineering evidence is documented
separately by the main investigation.

## Start with an empty company

```python
from ledger import Ledger

book = Ledger("data/company.sqlite")
customer = book.create_contact("Example customer", "customer")
vendor = book.create_contact("Example supplier", "vendor")
project = book.create_project("Store refit")

book.post_journal("2026-01-01", "Owner investment", [
    {"account": "1000", "debit_cents": 1000000},
    {"account": "3000", "credit_cents": 1000000},
], external_id="opening-001")

invoice = book.create_invoice("2026-01-10", customer["id"], [
    {"description": "Design work", "account": "4000", "amount_cents": 50000},
], due_date="2026-02-09", project_id=project["id"], external_id="invoice-001")
book.receive_payment("2026-01-15", invoice["id"], 20000,
                     bank_account="1000", external_id="payment-001")

print(book.profit_loss("2026-01-01", "2026-01-31"))
print(book.ar_aging("2026-01-31"))
book.close()
```

Amounts are **integer cents**, not dollars. All float monetary inputs are
rejected. Inventory quantities accept `Decimal`, integer, or decimal string
with at most six decimal places. Each company has one reporting currency
(USD in the current application); there is no currency conversion.

## Implemented workflows

- Chart of accounts, customers/vendors, projects, manual balanced journals,
  and append-only journal reversals.
- Sales invoices and supplier bills, partial receipt/payment, outstanding
  balances and aging at a historical cutoff date.
- Mixed ordinary and inventory item lines on one invoice or bill; selected
  invoice receipts or bill payments allocated atomically across multiple
  documents for the same contact.
- Inventory purchases/sales, quantity control and moving weighted average
  cost. Selling records both the invoice and COGS atomically.
- Trial balance, accrual profit/loss, balance sheet, project profitability and
  monthly budget versus actual. Amounts remain integer cents in reports.
- Bank CSV import into a **pending inbox**; matching to existing posted bank
  lines or explicitly classifying new transactions; statement reconciliation
  with outstanding items and a zero-difference check.
- Accounting-period locks, append-only audit trail, repeated-request
  protection, SQLite backup and general-journal IIF text export.

Methods are documented by their signatures in `ledger.py`. `list_data()`
returns accounts, contacts, projects, items, invoices, bills, payments,
journals, pending/matched bank transactions, budgets and reconciliations.
`status()` returns record counts, schema version, integrity and close date.

An inventory invoice row is `{item_id, qty: "2", unit_price_cents: 2500}`;
an inventory bill row uses `unit_cost_cents`. A document can also contain
ordinary `{description, account, amount_cents}` rows. Amounts are calculated
from the exact quantity and integer unit price and rounded once to cents.
Non-stock service rows may use `{description, account, qty,
unit_price_cents}` (or `unit_cost_cents` on a bill) without an `item_id`;
their quantity and rate are retained but they do not change inventory.
Repeated SKU rows consume the remaining total stock cost in row order.
Original unit prices are retained in append-only audit metadata without a
schema migration of the reference database. Missing inventory, a bad later
row or an incompatible account mapping rejects the complete document.

`allocate_payments(date, contact_id, allocations, bank_account="1000",
external_id=None)` takes `[{document_id, amount_cents}, ...]`. It only permits
documents of one type for one contact. It creates real payments and journals,
including partial payments; if a later allocation fails, earlier allocations
roll back. The existing single-document payment methods remain available.
`pay_bills(date, allocations, bank_account="1000", external_id=None)` uses
the same allocation-row fields but accepts selected bills from different
vendors. It rejects invoices, duplicate documents and overpayment, and the
whole batch remains atomic. It supports the Pay Bills all-vendors selection;
customer receipts remain restricted to one selected customer.

`general_ledger(start_date, end_date, account=None)` returns dated posted rows
and account groups with opening balance, running balance, closing balance and
debit/credit totals. `account_listing(as_of=None)` supplies actual chart-of-
account balances. `contact_center(kind, as_of=None, contact_id=None)` supplies
the actual customer or vendor documents, payments and open balances. Contact
master creation accepts `name`, `kind`, `company_name`, `first_name`,
`last_name`, `phone`, `email`, and a multiline string `address`.
`update_contact_details(contact_id, name=None, ...)` edits the name and these
details, with omitted fields unchanged and an explicit empty string clearing a
detail. Existing contacts read missing details as empty strings.
`get_contact(contact_id)`, `list_data()` and the center all return the stored
details. Kind cannot be edited, and payment credentials or payment terms are
not silently accepted. String length limits are enforced before a write.

`create_invoice` and `create_bill` accept an optional `memo` keyword, limited
to 1000 characters. Their result and future reads retain that header memo,
and the posted journal memo includes it. A `contact_snapshot` records the
contact's name and address/details at document creation so editing the master
does not rewrite old document address history. Older documents without a
snapshot return `None`. Details, memos and snapshots are retained in the
append-only audit data, so this does not migrate or alter the reference schema.
Memo-free calls preserve their existing idempotency-request hash format.

The HTTP server exposes these methods through explicit report/action field
allowlists. Annual trial-balance and balance-sheet parameters are available
through HTTP, including an explicitly selected retained-earnings account.

## Read-only original-derived reference

`book=reference` selects the fixed `reference/reconstructed_qb_gl.sqlite3`
file. The server opens it with SQLite read-only immutable mode and `query_only`,
without initialization, migration or write pragmas. Every action POST is
rejected with HTTP 403. Company and synthetic demo databases remain separate.
`Ledger(path, readonly=True)` is for a checkpointed **static** reference or
backup file, and rejects an active nonempty WAL file rather than ignoring
committed WAL data.

The reference HTTP General Ledger uses the saved original GL export's parsed
rows, preserving source row, date, transaction type/number, party name, memo
and split. Carry and closing balances are checked against the independently
reconstructed posted ledger. Ordinary companies use their actual posted
journal lines. The reference is explicitly `reconstructed_gl_only`; the
original company's invoice/payment masters and customer/vendor masters were
not recovered from this export. The UI must not offer writes or invent those
masters. JSON export and a backup download are reads, and do not alter the
reference file. Source-file provenance is documented in `reference/`.

Reference HTTP reports additionally provide `presentation_rows` for the UI.
In the exact original 2023 report scope, account order and colon-separated
path labels come from the saved original report's **label/order metadata**;
all amounts are mapped from the current computed report by account code.
The original TB scope requires December 31, 2023, a January 1, 2023 annual
start, and explicitly selected retained account `32000`. P/L requires the
full 2023 accrual period, and BS the original date/accrual/retained scope.
Other periods or modes use active computed accounts and account-type metadata.
Unmapped extra nonzero computed accounts are preserved, never hidden to match
an original picture. Raw `rows`, balances, totals and ledger data are unchanged.

`presentation_amount_source="computed_live_ledger"` and `source_scope` state
these boundaries. `presentation_matches_report_totals` verifies that the UI
rows retain all report amounts. `display_tree` groups account paths and sums
the computed children. Leaf `source_row` is the original report's row number;
generated parent groups have `source_row=None` and only identify their first
native leaf for ordering. The server does not pretend a derived group is a
captured native heading or read old financial values into the computation.

Original-scope P/L and BS also expose `presentation_layout_rows` containing
every source body label, indent level and row order, including major sections,
subtotals and financial totals. Roles are `header`, `leaf`, `subtotal`, and
`financial_total`. Header amounts are null. Account leaves map live computed
accounts, including a zero-valued parent-Other leaf. Each subtotal sums only
leaf amounts between its matching heading and closing subtotal, so nested
subtotals cannot be counted twice. P/L gross/ordinary/other/net profit follow
live subtotal equations. BS final assets and liabilities/equity use live
report totals. Only A-F source label cells and the G4 text caption are used;
G5 onward financial values and formula caches are ignored.

`presentation_layout_caption` is the original amount-column caption. Levels
are A=0 through F=5. The first four report header rows are separate in
`presentation_layout_header_rows` to avoid duplicating a UI's report title;
body plus headers preserve all 76 P/L or 77 BS source rows. The synthetic BS
Net Income leaf has no drillable account code. `presentation_layout_total_match`
checks leaf completeness and final totals, with unmapped nonzero codes and
calculation errors reported explicitly. Other scopes retain the general
computed account tree.

## Accounting policies and controls

Every posted entry must balance. Write operations use `BEGIN IMMEDIATE`, so a
document, its journal, its inventory movement and its audit record either
commit together or roll back together. Reports use a single SQLite read
snapshot, including when another process posts data concurrently.

`external_id` provides idempotency separately for each supported operation.
Repeating the exact request returns its original result without reposting it.
Reusing the same key for different data fails. Without a supplied key the
caller intentionally requests a new transaction. Invoices, journals,
payments, inventory movements and audit records cannot be updated or deleted
through this engine. Corrections to manual journals use a dated reversal.
Document credit notes/voids and inventory returns are not implemented.

Inventory cost is calculated from **remaining total cents / remaining
quantity**, multiplied by quantity sold, then rounded half-up to cents. It
does not first round the average unit cost. A sale of all remaining stock
consumes all remaining cost cents. Negative stock and a movement earlier than
the last movement for that item are rejected. This chronological-only rule
avoids silently changing previously recorded sale costs. Opening inventory
can be entered with `buy_inventory(..., offset_account="3000")`; direct
journals or plain bills to managed inventory accounts are rejected because
they would bypass quantity accounting.

Historical inventory, receivables and payables are calculated from dated
movements/payments, not current balances. Aging and inventory reports also
expose `subledger_difference_cents` so manual AR/AP entries cannot silently
be presented as document-level balances.

`close_period(end_date)` rejects new accounting entries on or before that
date. It does not create a year-end tax close or transfer profit into retained
earnings. The balance sheet preserves actual posted equity balances in `rows`,
and reports `cumulative_profit_cents`, `prior_year_profit_cents`, and
`current_year_profit_cents`. The latter equals the calendar-year P/L through
the report date. `fiscal_year_start` and `fiscal_year_policy="calendar_year"`
state the supported year boundary; other fiscal-year configurations are not
implemented. The legacy field `current_earnings_cents` remains cumulative for
API compatibility and must not be labeled current-year net income.

`display_rows` appends prior-year profit and current-year profit to posted
balances without changing the ledger. To explicitly combine unclosed
prior-year profit with a particular retained-earnings account in display only,
call `balance_sheet(date, retained_account="3100")` using its code. The engine
does not infer that account from its name. Posted balances, audit records and
total equity remain unchanged under either presentation.

`trial_balance(date)` defaults to **cumulative posted balances**, which is the
right view for verifying all historical journals. For a calendar-year trial
balance like the annual QuickBooks presentation, explicitly call
`trial_balance("2026-12-31", fiscal_year_start="2026-01-01",
retained_account="3100")`. Income/expense rows then contain year-to-date net
movements, asset/liability/equity rows retain their posted closing balances,
and still-unclosed prior-year P/L is combined with the selected equity account
in the returned presentation only. Without a selected account it is shown in
a separate calculated row. `cumulative_rows` always retain the posted view.

If actual journals already closed income/expense balances into equity before
the year starts, the transferred earnings are already in equity; this view
does not add them a second time. Manually entered closing journals within the
report year remain actual journal movements (and affect that year's P/L);
the engine does not infer or hide journal intent from a memo. Non-January
fiscal-year starts are not supported by this annual display.
Budgets can be revised with a new audit entry. A statement reconciliation
does not independently close the accounting period.

## Cash report scope

`basis="cash"` is an explicitly defined **management receipts/payments
view**, not a tax-basis calculation and not a claim of equivalence with all
QuickBooks inventory or cash-report exceptions:

- Invoice income is recognized in proportion to payments received by the
  cutoff date. Cents are allocated across invoice lines with the exact total
  preserved.
- COGS on inventory invoices is recognized by the same paid proportion.
- Supplier-bill expense/asset lines are recognized in proportion to payments
  made. Unpaid receivables/payables are removed through balanced reporting
  adjustments.
- Direct journal entries stay on their posting date. These adjustments do not
  mutate the posted accrual ledger or inventory quantity records.

Use the accrual reports as the independently tested accounting results.
Do not submit these cash reports as statutory tax accounts.

## Bank CSV, reconciliation and backups

```csv
date,description,amount_cents,external_id
2026-01-15,Customer receipt,20000,bank-transaction-001
2026-01-16,Bank fee,-1500,bank-transaction-002
```

`import_bank_csv` accepts CSV contents or a local CSV path. Alternatively an
`amount` column accepts decimal dollars with at most two decimal places.
Positive is money into the bank. A stable bank-provided `external_id` is best.
Without one, a date/description/amount hash plus occurrence count is used;
changed descriptions or differently sliced files may need manual duplicate
review. Imports never post money automatically. An exact repeated import
creates no extra pending records. A conflicting ID rejects the entire import.

Use `match_bank_transaction(id, entry_line_id)` for already posted payments.
Use `classify_bank_transaction(id, account)` only for genuinely unposted rows.
`bank_register` exposes line IDs. A reconciliation accepts newly cleared
bank-line IDs; their net value plus the preceding statement balance must equal
the new statement balance. Already cleared lines cannot be cleared twice.

`backup(destination)` writes a new SQLite file and refuses to overwrite an
existing file. To restore, open that backup using `Ledger(backup_path)` or copy
it to a **new** company path before opening it. IIF export contains general
journals only. It has not been validated by importing into QuickBooks and
does not preserve invoices, inventory entities or payment links there.

## Explicit gaps

No QuickBooks QBW/QBB/QB201x migration parser, bank/OAuth feed, payment gateway,
payroll or government filing, sales-tax engine, automatic tax treatment,
multi-currency, serial/lot stock, inventory assemblies, purchase orders,
discounts/credit notes/returns, user roles, cloud collaboration or statutory
report certification. The local web application needs a trusted local user;
it is not an authenticated multi-user internet service.

The data format remains inspectable and backups are portable. The relevant
test results, original-product evidence and independently frozen expected
results are supplied by the other parts of this investigation, rather than
being inferred from this engine's own output.
