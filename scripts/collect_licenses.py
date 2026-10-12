#!/usr/bin/env python3
"""Copy authentic installed license texts and produce an environment inventory.

No network access, inferred license text or user-specific absolute paths. Two
audited upstream texts cover wheels that omit their own license files; those
fallbacks are version-specific and checked against their recorded SHA256.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import sysconfig

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent
LICENSE_NAME = re.compile(r'^(license(?:s)?|copying|notice|copyright)(?:$|[._-])',re.I)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def text_bytes(path):
    content = path.read_bytes()
    if not content or len(content)>2*1024*1024 or b'\0' in content:
        return None
    try:
        content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None
    return content


def dependency_closure(distributions,seeds):
    visited = set()
    pending = [canonicalize_name(name) for name in seeds]
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        if name not in distributions:
            raise RuntimeError(f'Required build dependency is absent: {name}')
        visited.add(name)
        for value in distributions[name].requires or []:
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({'extra':''}):
                pending.append(canonicalize_name(requirement.name))
    return visited


def python_license():
    stdlib = Path(sysconfig.get_path('stdlib'))
    prefix = Path(sys.base_prefix)
    candidates = [(stdlib/'LICENSE.txt','<python-stdlib>/LICENSE.txt'),
                  (stdlib/'LICENSE','<python-stdlib>/LICENSE'),
                  (prefix/'LICENSE.txt','<python-base-prefix>/LICENSE.txt'),
                  (prefix/'LICENSE','<python-base-prefix>/LICENSE'),
                  (prefix/'share'/'doc'/'python'/'LICENSE','<python-base-prefix>/share/doc/python/LICENSE')]
    for source,label in candidates:
        if source.is_file():
            content = text_bytes(source)
            if content and b'PYTHON SOFTWARE FOUNDATION' in content.upper():
                return content,label
    raise RuntimeError('Authentic Python PSF LICENSE text was not found in the installed Python runtime; package build stopped.')


def copy_record(output,component,relative_path,content,source_kind,source_path,**extra):
    relative = PurePosixPath(component)/PurePosixPath(relative_path)
    if relative.is_absolute() or '..' in relative.parts:
        raise RuntimeError('Unsafe relative license filename in distribution metadata')
    destination = output.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_bytes(content)
    return dict(file=relative.as_posix(),sha256=digest(content),bytes=len(content),
                source_kind=source_kind,source_path=source_path,**extra)


def collect_licenses(output):
    output = Path(output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    distributions = {canonicalize_name(distribution.metadata['Name']):distribution
                     for distribution in importlib.metadata.distributions()}
    runtime = dependency_closure(distributions,('pywebview',))
    tooling = dependency_closure(distributions,('PyInstaller','Pillow'))
    fallback_index = json.loads((ROOT/'license_fallbacks'/'index.json').read_text(encoding='utf-8'))
    fallback = {(canonicalize_name(entry['distribution']),entry['version']):entry
                for entry in fallback_index['files']}
    python_content,python_source = python_license()
    python_record = copy_record(output,'Python-'+platform.python_version(),'LICENSE.txt',python_content,
                                'installed_python_runtime',python_source)
    components = [dict(name='Python',version=platform.python_version(),roles=['runtime'],
                       license_metadata='Python PSF license text and bundled notices',license_files=[python_record])]
    missing_tooling = []
    for name,distribution in sorted(distributions.items()):
        roles = ['runtime_dependency'] if name in runtime else []
        if name=='pyinstaller':
            roles.append('build_tool_and_embedded_bootloader_runtime_hooks')
        elif name in tooling:
            roles.append('build_tooling')
        if not roles:
            roles=['build_environment_only']
        directory = name+'-'+distribution.version
        files = []
        declared_files = distribution.metadata.get_all('License-File') or []
        for filename in sorted(distribution.files or [],key=str):
            relative = PurePosixPath(str(filename).replace('\\','/'))
            if not (LICENSE_NAME.match(relative.name) or relative.name in declared_files):
                continue
            source = Path(distribution.locate_file(filename))
            if not source.is_file():
                continue
            content = text_bytes(source)
            if content is not None:
                files.append(copy_record(output,directory,relative,content,
                                         'installed_distribution_file',relative.as_posix()))
        if not files:
            upstream = fallback.get((name,distribution.version))
            if upstream:
                source = ROOT/'license_fallbacks'/upstream['file']
                content = text_bytes(source)
                if content is None or digest(content)!=upstream['sha256']:
                    raise RuntimeError(f'Authentic upstream license fallback checksum failed: {name}')
                files.append(copy_record(output,directory,upstream['file'],content,
                                         upstream['source_kind'],upstream['source_url'],
                                         provenance=upstream))
        if not files:
            # Preserve a full license embedded in METADATA when supplied, but
            # a short identifier such as "MIT" is not a replacement text.
            declared = distribution.metadata.get('License','')
            if len(declared)>200 and '\n' in declared:
                files.append(copy_record(output,directory,'LICENSE-from-METADATA.txt',declared.encode('utf-8'),
                                         'installed_distribution_metadata','METADATA:License'))
        if not files and (name in runtime or name=='pyinstaller'):
            raise RuntimeError(f'Authentic license text missing for packaged runtime dependency {name} {distribution.version}; build stopped.')
        if not files:
            missing_tooling.append(name)
        if name=='pyinstaller' and not any(b'Bootloader Exception' in output.joinpath(*PurePosixPath(record['file']).parts).read_bytes() for record in files):
            raise RuntimeError('Installed PyInstaller COPYING text lacks its bootloader exception; build stopped.')
        components.append(dict(name=distribution.metadata['Name'],version=distribution.version,roles=roles,
                               license_metadata=distribution.metadata.get('License-Expression') or distribution.metadata.get('License'),
                               license_files=files,requires_dist=distribution.requires or []))
    index = dict(schema_version=1,platform=sys.platform,python_version=platform.python_version(),
                 scope='Installed build environment; runtime dependency candidates are classified separately from build tooling. This is not a complete system-library or binary-level SBOM.',
                 source_path_policy='Installed paths are relative to their distribution or Python installation; no user absolute paths.',
                 missing_build_tooling_license_text=missing_tooling,components=components)
    (output/'package-license-index.json').write_text(json.dumps(index,indent=2)+'\n',encoding='utf-8')
    sbom = dict(schema_version=1,document_type='Ledger Reborn build environment SBOM',platform=sys.platform,
                scope=index['scope'],components=[{key:item[key] for key in ('name','version','roles','license_metadata','license_files')}
                                                 for item in components])
    (output/'build-environment-sbom.json').write_text(json.dumps(sbom,indent=2)+'\n',encoding='utf-8')
    (output/'README.txt').write_text(
        'THIRD-PARTY NOTICES\n\nAuthentic license and notice texts are retained verbatim.\n'
        'package-license-index.json records versions, relative sources, provenance and SHA256.\n'
        'Runtime dependencies, build tooling and other installed build-environment packages\n'
        'are identified separately. Build tooling entries do not claim those complete packages\n'
        'are shipped at runtime. PyInstaller includes an embedded bootloader and runtime hooks;\n'
        'its COPYING text includes the bootloader exception and runtime-hook terms.\n'
        'The inventory is not a complete system-library or binary-level SBOM.\n',encoding='utf-8')
    return index


if __name__=='__main__':
    parser = argparse.ArgumentParser(description='Collect authentic notices from this installed build environment')
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    index = collect_licenses(args.output)
    print(json.dumps({'components':len(index['components']),
                      'license_files':sum(len(item['license_files']) for item in index['components']),
                      'missing_build_tooling_license_text':index['missing_build_tooling_license_text']},indent=2))
