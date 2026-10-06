#!/usr/bin/env python3
"""Check Git-index bytes before publication. Never print matching secret values."""
import re
import subprocess
import sys
from pathlib import PurePosixPath


def git(*args):
    return subprocess.check_output(['git', *args])


files = git('ls-files', '-z').decode().split('\0')
files = [name for name in files if name]
if not files:
    raise SystemExit('No tracked files: stage the intended source before checking.')
patterns = {
    'SDK token': re.compile(rb'mgst_[A-Za-z0-9_-]{24,}'),
    'GitHub token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    'API key': re.compile(rb'sk-(?:proj-)?[A-Za-z0-9_-]{24,}'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'JWT': re.compile(rb'eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}'),
    'personal Windows path': re.compile(rb'[A-Za-z]:[\\/]Users[\\/](?!USER(?:[\\/]|\b))[^\s"<>]+', re.I),
}
forbidden_names = {'sdk_token', 'pairing.json', 'credentials.json', '.env'}
forbidden_parts = {'node_modules', '__pycache__', '.venv', '.pytest_cache', 'evidence', 'dist'}
forbidden_suffixes = {'.onnx', '.wav', '.mp3', '.pem', '.key', '.log', '.pyc', '.tar'}
problems = []
for name in files:
    path = PurePosixPath(name)
    if (path.name in forbidden_names or path.suffix in forbidden_suffixes
            or any(part in forbidden_parts for part in path.parts)
            or name.startswith('ui/public/esp32-avatar/')):
        problems.append((name, 0, 'runtime/private file'))
    mode = git('ls-files', '-s', '--', name).split(None, 1)[0]
    if mode == b'120000':
        problems.append((name, 0, 'symlink requires review'))
    content = git('show', ':' + name)
    if name == 'docs/images/main-screen.png':
        if not content.startswith(b'\x89PNG\r\n\x1a\n') or len(content) > 2_000_000:
            problems.append((name, 0, 'unexpected preview image'))
        continue
    if b'\0' in content:
        problems.append((name, 0, 'unexpected binary'))
        continue
    for line_number, line in enumerate(content.splitlines(), 1):
        for label, pattern in patterns.items():
            if pattern.search(line):
                problems.append((name, line_number, label))
for name, line, label in problems:
    print(f'{name}:{line}: {label}')
if problems:
    raise SystemExit(f'Publication blocked: {len(problems)} findings. No matched values printed.')
print(f'PASS: {len(files)} indexed files checked; no runtime files or credential patterns found.')
print('This pattern check supplements review; it is not a guarantee about all possible secrets.')
