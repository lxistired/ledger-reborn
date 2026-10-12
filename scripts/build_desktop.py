#!/usr/bin/env python3
"""Build on the target OS, smoke-test the frozen program, then make installers.

No code signing credentials, notarization, quarantine changes, publishing,
network download or Git operation is performed by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

from collect_licenses import collect_licenses

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = 'Ledger Reborn'
VERSION = '2026.10.12-preview.1'
BUNDLE_ID = 'io.github.lxistired.ledgerreborn'


def run(args,**kwargs):
    print('Running:',repr([str(value) for value in args]),flush=True)
    subprocess.run([str(value) for value in args],check=True,**kwargs)


def icon_for_platform(work):
    from PIL import Image
    source = ROOT/'assets'/'app-icon.png'
    if not source.is_file():
        raise FileNotFoundError('Original assets/app-icon.png must exist before packaging')
    image = Image.open(source).convert('RGBA')
    icon = work/('app-icon.icns' if sys.platform=='darwin' else 'app-icon.ico')
    image.save(icon,format='ICNS' if sys.platform=='darwin' else 'ICO')
    return icon


def write_spec(work,icon,notices):
    hidden = ['webview.platforms.cocoa'] if sys.platform=='darwin' else ['webview.platforms.winforms','webview.platforms.edgechromium']
    data = [(str(ROOT/'web'),'web'),(str(ROOT/'fixtures'),'fixtures'),(str(notices),'THIRDPARTY_NOTICES')]
    data += [(str(ROOT/name),'.') for name in ('VERSION','LICENSE') if (ROOT/name).is_file()]
    architecture = platform.machine()
    spec = work/'Ledger Reborn.spec'
    text = f'''# Generated reproducible build recipe; no private reference data.
from PyInstaller.utils.hooks import collect_data_files
webview_data = collect_data_files('webview')
a = Analysis([{str(ROOT/'desktop.py')!r}], pathex=[{str(ROOT)!r}],
             binaries=[], datas={data!r}+webview_data,
             hiddenimports={hidden!r}, hookspath=[], runtime_hooks=[],
             excludes=['tkinter','pytest','PyQt5','PyQt6','PySide2','PySide6','cefpython3','wx','gi'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name={PRODUCT!r},
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, icon={str(icon)!r},
          argv_emulation=False, target_arch={architecture!r} if {sys.platform!r}=='darwin' else None,
          codesign_identity=None, entitlements_file=None)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name={PRODUCT!r})
'''
    if sys.platform=='darwin':
        info = {'CFBundleName':PRODUCT,'CFBundleDisplayName':PRODUCT,'CFBundleIdentifier':BUNDLE_ID,
                'CFBundleShortVersionString':'2026.10.12','CFBundleVersion':'2026.10.12',
                'NSHighResolutionCapable':True,'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True}}
        text += f'app = BUNDLE(collection, name={PRODUCT+".app"!r}, icon={str(icon)!r}, bundle_identifier={BUNDLE_ID!r}, info_plist={info!r})\n'
    spec.write_text(text,encoding='utf-8')
    return spec


def make_dmg(app,out,arch,work):
    stage = work/'dmg-stage'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    shutil.copytree(app,stage/app.name,symlinks=True)
    (stage/'Applications').symlink_to('/Applications',target_is_directory=True)
    image = out/f'Ledger-Reborn-{VERSION}-macOS-{arch}.dmg'
    run(['hdiutil','create','-volname',PRODUCT,'-srcfolder',stage,'-fs','HFS+','-format','UDZO','-ov',image])
    return image


def find_inno_setup():
    candidates = [shutil.which('ISCC.exe'),
                  os.environ.get('PROGRAMFILES(X86)','')+'\\Inno Setup 6\\ISCC.exe',
                  os.environ.get('PROGRAMFILES','')+'\\Inno Setup 6\\ISCC.exe']
    return next((Path(path) for path in candidates if path and Path(path).is_file()),None)


def make_windows_package(app_dir,out,work):
    archive = out/f'Ledger-Reborn-{VERSION}-Windows-x64-portable.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as package:
        for file in sorted(app_dir.rglob('*')):
            if file.is_file():
                package.write(file,Path(PRODUCT)/file.relative_to(app_dir))
    installer = find_inno_setup()
    if installer is None:
        return [archive],False
    script = work/'ledger-reborn.iss'
    def inno_quote(value):
        return str(value).replace('"','""')
    script.write_text(f'''[Setup]
AppId=io.github.lxistired.ledgerreborn
AppName={PRODUCT}
AppVersion={VERSION}
AppPublisher=lxistired
DefaultDirName={{localappdata}}\\Programs\\{PRODUCT}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={inno_quote(out)}
OutputBaseFilename=Ledger-Reborn-{VERSION}-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={{app}}\\Ledger Reborn.exe
[Files]
Source: "{inno_quote(app_dir)}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{{userprograms}}\\Ledger Reborn"; Filename: "{{app}}\\Ledger Reborn.exe"
[Run]
Filename: "{{app}}\\Ledger Reborn.exe"; Description: "Launch Ledger Reborn"; Flags: nowait postinstall skipifsilent
''',encoding='utf-8')
    run([installer,script])
    return [archive,out/f'Ledger-Reborn-{VERSION}-Windows-x64-Setup.exe'],True


def main():
    parser = argparse.ArgumentParser(description='Build Ledger Reborn on this target OS')
    parser.add_argument('--output-dir',type=Path,default=ROOT/'dist')
    parser.add_argument('--work-dir',type=Path,default=ROOT/'build'/'desktop')
    parser.add_argument('--skip-package',action='store_true',help='Build and frozen-smoke only, without DMG/ZIP/installer')
    args = parser.parse_args()
    if sys.platform not in ('darwin','win32'):
        parser.error('Native desktop builds require macOS or Windows; cross-compiling is not supported')
    for package,version in (('pywebview','6.2.1'),('pyinstaller','6.22.3'),('Pillow','12.3.0')):
        if importlib.metadata.version(package)!=version:
            parser.error(f'Install the pinned requirements-desktop.txt; {package} must be {version}')
    out,work = args.output_dir.resolve(),args.work_dir.resolve()
    out.mkdir(parents=True,exist_ok=True)
    work.mkdir(parents=True,exist_ok=True)
    notices = work/'THIRDPARTY_NOTICES'
    license_index = collect_licenses(notices)
    icon = icon_for_platform(work)
    spec = write_spec(work,icon,notices)
    run([sys.executable,'-m','PyInstaller','--clean','--noconfirm','--distpath',out,'--workpath',work/'pyinstaller',spec],cwd=ROOT)
    app = out/(PRODUCT+'.app' if sys.platform=='darwin' else PRODUCT)
    executable = app/'Contents'/'MacOS'/PRODUCT if sys.platform=='darwin' else app/(PRODUCT+'.exe')
    arch = 'arm64' if platform.machine().lower() in ('arm64','aarch64') else 'x64'
    report = out/f'frozen-smoke-{sys.platform}-{arch}.json'
    run([executable,'--smoke-test','--smoke-report',report],timeout=120)
    smoke = json.loads(report.read_text(encoding='utf-8'))
    if not smoke.get('all_passed') or not smoke.get('frozen'):
        raise ValueError('The built executable did not pass a real frozen smoke test')
    if sys.platform=='darwin':
        info = plistlib.loads((app/'Contents'/'Info.plist').read_bytes())
        if info.get('CFBundleIdentifier')!=BUNDLE_ID:
            raise ValueError('Mac bundle identifier mismatch')
    bundled_notices = app/'Contents'/'Resources'/'THIRDPARTY_NOTICES' if sys.platform=='darwin' else app/'_internal'/'THIRDPARTY_NOTICES'
    if not (bundled_notices/'package-license-index.json').is_file():
        raise ValueError('Built application is missing third-party notices')
    for component in license_index['components']:
        for record in component['license_files']:
            path = bundled_notices/record['file']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:
                raise ValueError(f'Bundled third-party license is missing or changed: {component["name"]}')
    packages,installer_built = [],False
    if not args.skip_package:
        if sys.platform=='darwin':
            packages=[make_dmg(app,out,arch,work)]
        else:
            packages,installer_built = make_windows_package(app,out,work)
    manifest = dict(product=PRODUCT,project_number='000001',version=VERSION,platform=sys.platform,
                    architecture=arch,python_version=platform.python_version(),frozen_smoke=report.name,
                    developer_id_signed=False,notarized=False,windows_installer_built=installer_built,
                    third_party_notices_verified=True,license_component_count=len(license_index['components']),
                    requirements={package:importlib.metadata.version(package) for package in ('pywebview','pyinstaller','Pillow')},
                    artifacts=[{'file':path.name,'bytes':path.stat().st_size,
                                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in packages])
    (out/f'build-manifest-{sys.platform}-{arch}.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest,indent=2),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
