# From REA investigation to a runnable accounting application

Project #000001: [Ledger Reborn](https://github.com/lxistired/ledger-reborn), maintained by [@lxistired](https://github.com/lxistired).

## Question

Could REA help us understand enough of a paid desktop accounting product to build a usable local application that saves transactions and produces correct reports?

The investigation target was the official QuickBooks Desktop for Mac 2024 Trial, version 23.1.13.962 R14 Trial. We used the vendor's official sample companies. We did not use a real business ledger, register an Intuit account, connect a bank, execute payments or file a return.

## How REA helped

REA 6.4.0, source commit `3bdc8730bd98e073522bcb8ba86527be402c0cbe`, was used with Ghidra for focused native functions, with its interface-resource decoder for NIB layouts, and with native runtime capture for selected original windows and actions.

Focused investigation covered inventory arithmetic, account-type sign treatment in budgets, reconciliation balance paths and cash-basis branches. Resource decoding provided frames, fonts, control properties and connections. Runtime observations established selected Home/Trial Balance layouts and the Hide Header action. A later Chart of Accounts observation supplied another live layout reference.

REA supplied evidence and imperfect pseudocode. We independently wrote the Python/SQLite engine and frontend. These were not original source code recovered wholesale or a one-click generated product.

## What the application does

The public application saves balanced journals, invoices, bills, contact snapshots, inventory movements, partial payments, CSV bank matches, reconciliations, budgets and period locks. UI controls connect to those operations; reports are calculated from saved data.

An isolated synthetic UI flow recorded two invoices, partial receipts, three bills across two vendors, inventory cost, bank import/matching, fees, reconciliation and a locked-period rejection. Independent final checks gave assets $3,600.00, liabilities $20.00, equity $3,580.00 and net profit $580.00. This was a synthetic workflow, not an original-company migration.

The public fixtures and tests are available to reproduce accounting expectations. Current platform build/installation results are recorded in the release assets and workflow runs rather than inferred from source passing.

## Separate original-software comparison

Original reports were exported locally from the official sample company and compared with an independent GL reconstruction. The 2023 P&L, 2023-12-31 balance sheet and fiscal trial balance matched account-by-account to the cent. The P&L net profit was $113,560.69; assets were $630,033.93; trial-balance debits and credits were each $1,081,718.48.

Those original reports and sample database are not redistributed. This result concerns report reconstruction. It does not establish restored invoice lines, SKU quantities, payment links, full company-file migration or replacement of QuickBooks Online services. It is separate from the public synthetic tests.

## Limitations encountered

The original trial crashed while opening some historical-transaction windows on the local machine. Runtime messages reported `TargetMO transDisplayName` and `TargetMO transAbsAmount` unrecognized selectors. REA pseudocode identified the name/amount calls in the invoice-list path. The underlying cause of the missing runtime methods remains unknown; an account requirement was not established as the cause.

The application was usable without Intuit login for official sample loading and Chart of Accounts. That does not prove account-free access to online payment/bank services. No vendor binary was patched and no licensing or operating-system security controls were bypassed.

Selected layout work has been measured, but full pixel and original interaction equivalence has not passed. Accounting correctness and appearance are different checks. Vendor bitmaps used during private experiments have been removed from this public tree and replaced with original artwork.

## Continuing the work

The intended programme is public maintenance of this accounting application, then Project #000002 for personal tax. Personal tax is currently a roadmap item, not working software in this release.

We welcome REA community feedback on reproducible comparisons, evidence quality and workflows worth investigating. [Source and downloads](https://github.com/lxistired/ledger-reborn) · [Follow the maintainer](https://github.com/lxistired) · [Roadmap](../ROADMAP.md).
