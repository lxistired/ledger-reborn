# Validation scopes

Evidence is divided by what was actually tested. A source test count is not a UI feature count, and a platform build is not a full original-software comparison.

| Scope | Current evidence |
| --- | --- |
| Public accounting and HTTP source | 52 synthetic tests passed on the local macOS source tree; use `python -m unittest discover` to reproduce |
| HTTP workflow/security smoke | 12 checks passed; source script in `tests/http_smoke.py` |
| Frozen macOS executable | 8 checks passed, including persisted invoice/partial receipt, computed reports, Origin/Host rejection and server shutdown |
| Mac distribution | ARM64 application and DMG actually built; ad hoc signature integrity and DMG verification passed; no Developer ID notarization |
| Desktop UI | Actual independent macOS window opened; My Company was empty; isolated synthetic data loaded and P&L displayed computed values |
| Other platform builds | Consult the actual GitHub Actions runs and release assets; a configured workflow alone is not a verified build |
| Original product comparison | Separate private official-sample report comparison described in `CASE_STUDY.md`; vendor data is not redistributed |

The synthetic `fixtures/expected.json` was frozen independently. Its SHA-256 is:

`f659c025156995f1e7af547a336908429594ec7d58dba441826c43420ec45d25`

The 52 public tests are the public suite after removing private original-data dependencies and adding public distribution checks. They should not be confused with the older private suite's coincidentally identical count.

Native package manifests, executable smoke results and checksums belong to each release. User data is stored separately from application files. Reports are read from the engine rather than displayed as predetermined financial numbers.
