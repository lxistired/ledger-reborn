# Ten-minute desktop persistence walkthrough

This checks real invoice/payment posting, restart persistence and reports in the released preview. Windows and Intel Mac installation and full GUI behavior still need community verification. Use synthetic data only.

1. Download your native package from [the preview release](https://github.com/lxistired/ledger-reborn/releases/tag/v2026.10.12-preview.1). Record the asset name, OS version and CPU architecture. Windows needs WebView2. The preview has no publisher certificate or Apple notarization; do not disable operating-system security protections to test it.
2. Open Ledger Reborn and select the empty local company rather than the synthetic demo. If you already have data, use a new isolated data directory (`--data-dir PATH`) for this walkthrough. Never clear a real ledger.
3. Open **Customer Center**, select **New Customer**, and create `Walkthrough Customer`.
4. Open **Create Invoices**, choose that customer and enter one service/account line: description `Walkthrough service`, amount **100.00 USD**. Save the invoice. No inventory item or tax is needed. Record the invoice date and number.
5. Open **Receive Payments**, select the customer and this invoice, then enter a **40.00 USD** partial payment into the local bank account. Save. Expected open invoice balance: **60.00 USD**.
6. Close the application completely, reopen it, and select the same local company. In **Customer Center**, select `Walkthrough Customer`. Expected persisted open balance: **60.00 USD**. Open the invoice to confirm its original total remains **100.00 USD**.
7. For a report period containing the transaction dates, check the accrual Profit & Loss: service income and net profit **100.00 USD**. On the Balance Sheet after the payment, expected bank balance **40.00 USD**, accounts receivable **60.00 USD**, total assets **100.00 USD**, and retained/current earnings **100.00 USD**, assuming this isolated company had no other entries.
8. Open Trial Balance. Expected total debits and credits: **100.00 USD** each. Download a database backup and confirm the file is nonempty. Do not attach databases or personal financial information to a public issue.

Please [open a bug report](https://github.com/lxistired/ledger-reborn/issues/new) with the package name, OS/architecture, step that failed, expected versus actual amount, and a screenshot containing synthetic data only. If everything passes, [report a platform check](https://github.com/lxistired/ledger-reborn/issues/1) with those details. A passing API or calculation test does not establish that the installer and GUI work on your machine.
