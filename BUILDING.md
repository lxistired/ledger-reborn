# Building Ledger Reborn desktop packages

Ledger Reborn opens one native application window around its local accounting
server. The packaged application includes Python and the accounting engine; the
end user does not install Python. The server listens only on `127.0.0.1` and
chooses an available port. Closing the window stops its server and listening
socket. Explicit clicks on external links use the system browser.

The package uses `pywebview==6.2.1`, `PyInstaller==6.22.3` and `Pillow==12.3.0`.
macOS uses the system Cocoa/WebKit renderer. Windows uses EdgeChromium and needs
Microsoft's WebView2 runtime; that runtime is not bundled or silently installed
by this preview. See the [pywebview engine documentation](https://pywebview.flowrl.com/guide/web_engine.html).

## Native builds

Build each package on its target operating system. The build script does not
cross-compile. Python 3.13 is used by the three GitHub Actions jobs; the initial
local macOS ARM64 package was built with Python 3.14.5. Python, system SDK and
runner updates can change package bytes. The script records the actual versions
and artifact hashes; it does not claim bit-identical builds.

On macOS:

```sh
python3 -m venv work/release-build-venv
work/release-build-venv/bin/python -m pip install -r requirements-desktop.txt
work/release-build-venv/bin/python -m unittest discover -v
work/release-build-venv/bin/python tests/http_smoke.py
work/release-build-venv/bin/python scripts/build_desktop.py --work-dir work/desktop-build --output-dir dist
```

On Windows, in PowerShell:

```powershell
python -m venv work/release-build-venv
& work/release-build-venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
& work/release-build-venv/Scripts/python.exe -m unittest discover -v
& work/release-build-venv/Scripts/python.exe tests/http_smoke.py
& work/release-build-venv/Scripts/python.exe scripts/build_desktop.py --work-dir work/desktop-build --output-dir dist
```

The script converts the original `assets/app-icon.png` to a native icon, freezes
the application, runs the frozen executable's smoke test, and then packages it.
Only the public web assets, synthetic fixtures, runtime dependencies, VERSION,
LICENSE and authentic third-party notices are included as resources. User databases and private comparison
files are excluded by the build recipe.

Before freezing, `scripts/collect_licenses.py` copies actual LICENSE, COPYING,
NOTICE and copyright texts listed by each installed distribution's metadata.
It also reads the installed Python runtime's real PSF LICENSE; the build fails
if that text is absent. The output is `THIRDPARTY_NOTICES`, containing the
verbatim texts, `package-license-index.json` and `build-environment-sbom.json`.
The index retains versions, SHA256 checksums, relative source paths and roles.
Runtime dependency candidates, build tools and other environment packages are
classified separately; the inventory does not claim to describe every native
system library. PyInstaller's actual COPYING text and bootloader exception are
retained because its bootloader and runtime hooks are embedded in the app.

The pyobjc-core, Security and UniformTypeIdentifiers 12.2.2 wheels and the
proxy_tools 0.1.0 distribution omit license text.
Version-specific copies of their authentic upstream texts are stored under
`scripts/license_fallbacks`, with official source URLs, artifact/commit provenance
and SHA256. These are used only when installed files omit the texts. The proxy_tools
metadata and upstream license use different license names; both source records
are preserved without replacing the author's text. No license text is generated
from a short identifier. Missing runtime license text or a fallback checksum
mismatch stops the build. Collection itself performs no network request.

The notices are included in the frozen resources. Before creating a DMG or
Windows package, the build checks every recorded notice hash inside the actual
application. `third_party_notices_verified` in the build manifest records that
check. This is independent of the accounting/API smoke test.

macOS produces a `.app` and a DMG containing the application and an Applications
shortcut. Windows produces a portable ZIP. If Inno Setup is found on that Windows
machine, the script also produces a setup executable that installs for the
current user. If Inno Setup is absent, the manifest explicitly reports
`windows_installer_built: false`. It does not download an installer compiler.

The preview has no Apple Developer ID signature, Apple notarization or Windows
publisher certificate. PyInstaller applies the local ad hoc signature needed by
macOS. A downloaded preview can trigger operating-system checks and warnings.
The build recipe does not change quarantine, Gatekeeper or SmartScreen settings.

## Data location

The application creates `company.sqlite` and a separate `demo.sqlite` under:

| Platform | Default directory |
| --- | --- |
| macOS | `~/Library/Application Support/Ledger Reborn` |
| Windows | `%LOCALAPPDATA%\Ledger Reborn` |
| Source server on Linux | `$XDG_DATA_HOME/Ledger Reborn`, or `~/.local/share/Ledger Reborn` |

Webview browser storage is under that directory's `webview` subdirectory. A
package upgrade does not replace these databases. The source server and desktop
application accept `--data-dir PATH`; `LEDGER_REBORN_DATA_DIR` supplies an
environment override. The command-line argument takes priority. A frozen app
rejects paths inside its own installed bundle/resources; Windows also rejects
Program Files. Backups can be downloaded from the application.

The public app starts with the empty `company` book. Synthetic fixtures load only
into the explicitly selected `demo` book. `available_books` exposes those two
books. Vendor comparison data is absent: a request for the reserved reference
book returns 404 and all reference writes return 403.

## Packaged smoke test

For the macOS application:

```sh
"dist/Ledger Reborn.app/Contents/MacOS/Ledger Reborn" --smoke-test --smoke-report work/frozen-smoke.json
```

For the Windows application, in PowerShell:

```powershell
& "dist/Ledger Reborn/Ledger Reborn.exe" --smoke-test --smoke-report work/frozen-smoke.json
```

The test runs the executable without opening a GUI. It always creates a new
temporary data directory and removes it afterward, regardless of any real
company data or environment override. It checks bundled assets, the actual HTTP
API, persistence across connections, partial payment balances, independently
specified report amounts, SQLite backup, missing reference behavior, Origin/Host
rejection, and clean server shutdown. A passing frozen test requires
`all_passed: true` and `frozen: true`. GUI rendering still requires a separate
native window check; an API smoke test alone does not establish it.

The build writes `frozen-smoke-PLATFORM-ARCH.json` and
`build-manifest-PLATFORM-ARCH.json` alongside packages. The manifest records SHA256
checksums and the actual package size, Python version, pinned build dependencies,
architecture and signing status. Treat the files from each successful CI run as
that run's evidence.

## GitHub Actions

`.github/workflows/build-desktop.yml` runs the full public unittest suite and HTTP
smoke on `macos-15` (ARM64), `macos-15-intel` (x64) and `windows-2022` (x64). Every
job builds natively and runs its packaged executable's isolated smoke test before
uploading the DMG or Windows packages and evidence files. The workflow has read
permission and uploads workflow artifacts; it does not publish a release or
claim a platform succeeded until that job has actually completed.
