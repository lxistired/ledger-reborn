# Public roadmap

Maintainer: [@lxistired](https://github.com/lxistired). This is the intended work programme, not a claim that the items below already ship.

## Project #000001 — Ledger Reborn

The current preview supplies a real local accounting engine and an evolving desktop interface. Maintenance priorities are:

1. Reliable macOS and Windows packaging, installation, backups and restore verification.
2. More original-workflow comparisons, with verified and inferred behavior kept separate.
3. Credit memos, refunds and inventory returns, each with independent accounting expectations.
4. Tax and currency extensions with explicit policies and test fixtures.
5. Import tools and reconciliations before any claim of full company-file migration.
6. Accessibility, localisation and contributor-maintained platform coverage.

Release notes will distinguish implementation, validation and remaining gaps. A version number will not stand in for compatibility or production readiness.

## Project #000002 — Personal Tax (planned)

The initial target is US personal federal tax calculation, followed by carefully scoped extensions. This is the direction of the series; there is no personal tax engine in the accounting preview.

The sequence is:

1. Version each rule set by tax year and link it to official sources.
2. Build an explainable calculation engine using synthetic household scenarios.
3. Verify forms and edge cases independently, including held-out scenarios.
4. Add a local UI and clear import/export formats.
5. Evaluate state coverage and filing integrations after calculation validation.

Tax calculation, form preparation and electronic filing are separate capabilities. The project will not announce filing support because a calculation or a UI works. TurboTax is a comparison target for research, not a product included in this repository.

## How to help

Please open an issue with a concrete workflow, expected behavior and a reproducible synthetic example. Follow [@lxistired](https://github.com/lxistired) for the series and the repository releases for working builds.
