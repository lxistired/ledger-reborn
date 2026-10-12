# Ledger Reborn · Project #000001

Ledger Reborn is an independent open-source local bookkeeping application. The current public preview is `2026.10.12-preview.1`. The maintainer is [@lxistired](https://github.com/lxistired); the project link is [lxistired/ledger-reborn](https://github.com/lxistired/ledger-reborn). About presents ordinary user-initiated links, with no automatic opening, tracking or follow action. It is not an Intuit product and does not claim official affiliation with QuickBooks.

The public build opens **My Company**, an empty company with a real SQLite ledger. No Intuit account is required for the implemented local workflows. **Synthetic Test Company** is a separate test file loaded explicitly from the included synthetic fixtures. The preview does not connect to banks, execute payments, file taxes, or provide payroll services.

## Interface research and implementation

REA was used to inspect QuickBooks Desktop for Mac 2024 control geometry, interface resources, selected live windows and exported report structures. That research informed layout and behavior. The ledger, local HTTP API and DOM interface are independently implemented. A screenshot, a resource layout or a source export is evidence for the specific observation, not proof of complete original-software parity or a one-command clone.

The source includes independent layout coordinates and documented source references. It does not include raw NIB object archives, vendor application files, vendor icons or logos, screenshots, sample-company databases or vendor report exports. The original home-page bitmap resources were removed and replaced with original inline SVG symbols.

A private reference may appear only when a local installation has the complete reference materials needed by the backend. Public distributions include no private reference. The frontend reads `available_books`; absent references are removed from menus. A stale `book=reference` URL that receives the explicit unavailable response returns to My Company. This is not a general company-file import feature.

## Original project graphics

`assets/app-icon.svg` is original geometric ledger artwork created for this project from rectangles and lines. `assets/render_icon.py` reproducibly exports `assets/app-icon.png` at 1024×1024 using Pillow for the build. The exported PNG has transparent corners around the original rounded background. `web/assets/app-icon.svg` is the same SVG for the favicon. Workflow and toolbar graphics are original inline vectors and simple symbols, with no external CDN.

Project graphics are distributed under the repository license. Do not substitute extracted vendor artwork into a public release. Build tooling may derive macOS `.icns` and Windows `.ico` files from the original PNG.

## Public profile isolation

Local browser preferences use the `ledger-reborn-*` namespace, including `ledger-reborn-book`. They do not reuse the private QuickBooks reconstruction's saved company or report preferences. Runtime company data and installed desktop storage are controlled by the backend; no company transactions are embedded in the frontend.

## Verification scope

A release should verify startup with an empty company, explicit synthetic-demo loading, creating and reading back contacts and transactions, reports, bank reconciliation and backups. Frontend syntax checks and an asset scan are necessary but do not establish end-to-end accounting parity. Candidate behavior must be verified in the actual public build. Existing functional boundaries and accounting-policy caveats remain applicable.
