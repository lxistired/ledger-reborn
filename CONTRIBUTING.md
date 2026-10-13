# Contributing

Thank you for helping maintain Ledger Reborn. Open an issue with the specific workflow and expected accounting result before a large feature change.

For a first contribution, follow the [ten-minute invoice/payment/restart walkthrough](docs/CONTRIBUTOR_WALKTHROUGH.md). It includes exact expected balances and a place to report Windows or Intel Mac build and OS details.

- Keep money in integer cents and use Decimal for quantity/cost calculations.
- Preserve transaction atomicity, period locks, bank deduplication and audit records.
- Use synthetic fixtures; never commit a real customer ledger, credentials or proprietary vendor assets.
- Freeze expected calculations independently of the implementation.
- Run `python -m unittest discover -s tests -v`, `python tests/http_smoke.py` and `node --check web/app.js` for changes that affect those parts.
- For desktop changes, run the frozen executable smoke test and document which platform and UI were actually checked.
- Label a behavior inferred from packaged UI resources differently from one observed in the original application.

Please describe the final change, validation and remaining limitations in the PR. Small, focused fixes and reproducible accounting edge cases are welcome. The project does not accept license-bypass code or redistributed commercial application binaries.
