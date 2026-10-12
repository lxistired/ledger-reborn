"""Check the authored source distribution before publishing or building it."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {'.git', 'dist', 'build', 'release', '.venv', '__pycache__', '.cache', 'node_modules'}
TEXT_SUFFIXES = {'.py', '.js', '.css', '.html', '.md', '.txt', '.json', '.yml', '.yaml', '.svg', '.command', '.toml'}
BAD_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.xlsx', '.xls', '.dmg', '.exe', '.qb2024', '.qbw', '.qbb', '.bin'}
PATTERNS = {
    'private_user_path': re.compile(r'/Users/[A-Za-z0-9_.-]+/' + '|' + re.escape('/private' + '/var/folders/')),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'embedded_bitmap': re.compile(r'data:image/(?:png|jpeg|jpg|webp|gif);base64,'),
}


def files_to_check():
    if (ROOT / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True)
        candidates = [ROOT / p.decode() for p in result.stdout.split(b'\0') if p]
    else:
        candidates = sorted(ROOT.rglob('*'))
    return [p for p in candidates if p.is_file() and not EXCLUDED_PARTS.intersection(p.relative_to(ROOT).parts) and p.suffix != '.pyc' and p.suffix != '.spec' and p.name != '.DS_Store']


def main():
    findings = []
    checked = []
    for p in files_to_check():
        relative = str(p.relative_to(ROOT))
        if p.suffix.lower() in BAD_SUFFIXES or any(part.endswith('.app') for part in p.relative_to(ROOT).parts):
            findings.append({'file': relative, 'reason': 'binary, ledger, or vendor export in source tree'})
        raw = p.read_bytes()
        checked.append({'file': relative, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        if p.suffix.lower() in TEXT_SUFFIXES or p.name in ('LICENSE', 'VERSION', '.gitignore'):
            text = raw.decode('utf-8')
            for name, pattern in PATTERNS.items():
                for match in pattern.finditer(text):
                    findings.append({'file': relative, 'line': text.count('\n', 0, match.start()) + 1, 'reason': name})
    result = {'scope': 'Authored public source, not generated desktop distribution; flags potential disclosures and embedded vendor bitmaps for human review', 'files_checked': len(checked), 'findings': findings, 'passed': not findings, 'source_files': checked}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
