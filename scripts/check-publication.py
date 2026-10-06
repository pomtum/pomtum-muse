#!/usr/bin/env python3
"""Check staged bytes and optionally Git history; never print secret values."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


def git(*args):
    result = subprocess.run(['git', *args], capture_output=True)
    if result.returncode:
        raise SystemExit('Git inspection failed; raw output withheld. Check repository state.')
    return result.stdout


patterns = {
    'SDK token': re.compile(rb'mgst_[A-Za-z0-9_-]{24,}'),
    'GitHub token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    'API key': re.compile(rb'sk-(?:proj-)?[A-Za-z0-9_-]{24,}'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'JWT': re.compile(rb'eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}'),
    'personal Windows path': re.compile(rb'[A-Za-z]:[\\/]Users[\\/](?!USER(?:[\\/]|\b))[^\s"<>]+', re.I),
    'personal POSIX path': re.compile(rb'/(?:home|Users)/(?!pi/|user/|USER/|ubuntu/|example/|runner/)[A-Za-z0-9_.-]+/'),
}
forbidden_names = {'sdk_token', 'pairing.json', 'credentials.json', '.env'}
forbidden_parts = {'node_modules', '__pycache__', '.venv', '.pytest_cache', 'evidence', 'dist'}
forbidden_suffixes = {'.onnx', '.wav', '.mp3', '.pem', '.key', '.log', '.pyc', '.tar'}

def check_index():
    entries = []
    for row in git('ls-files', '--stage', '-z').split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            mode, oid, stage = metadata.split()
            if stage != b'0':
                raise SystemExit('Resolve unmerged index entries before publication.')
            entries.append((mode, oid, name.decode('utf-8', errors='replace')))
    if not entries:
        raise SystemExit('No tracked files: stage the intended source before checking.')
    result = subprocess.run(['git', 'cat-file', '--batch'],
                            input=b'\n'.join(oid for _, oid, _ in entries) + b'\n', capture_output=True)
    if result.returncode:
        raise SystemExit('Could not inspect indexed objects; publication blocked.')
    problems, offset = [], 0
    for mode, oid, name in entries:
        end = result.stdout.find(b'\n', offset)
        header = result.stdout[offset:end].split()
        if len(header) != 3 or header[:2] != [oid, b'blob']:
            raise SystemExit('Non-blob index entry requires a separate review.')
        size = int(header[2])
        content = result.stdout[end + 1:end + 1 + size]
        offset = end + 2 + size
        path = PurePosixPath(name)
        if (path.name in forbidden_names or path.name.startswith('.env.')
                or path.suffix in forbidden_suffixes
                or any(part in forbidden_parts or part.startswith('.venv') for part in path.parts)
                or name.startswith('ui/public/esp32-avatar/')):
            problems.append((name, 0, 'runtime/private file'))
        if mode not in (b'100644', b'100755'):
            problems.append((name, 0, 'symlink requires review'))
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
    print(f'PASS: {len(entries)} indexed files checked; no runtime files or credential patterns found.')


def check_gitleaks(history=False, push_input=None):
    executable = shutil.which('gitleaks')
    if not executable:
        raise SystemExit('Gitleaks is required. Install 8.30.1+; see docs/development.md.')
    revisions = []
    if push_input is not None:
        for line in push_input:
            fields = line.split()
            if len(fields) != 4 or not all(re.fullmatch(r'[0-9a-f]{40,64}', fields[i]) for i in (1, 3)):
                raise SystemExit('Invalid pre-push input; refusing to omit a pushed revision.')
            if set(fields[1]) != {'0'}:
                revisions.append(fields[1])
    if history and git('rev-parse', '--is-shallow-repository').strip() == b'true':
        raise SystemExit('Full-history scan requires a complete clone: git fetch --unshallow --tags.')
    with tempfile.TemporaryDirectory(prefix='pomtum-secret-check-') as directory:
        report = Path(directory) / 'redacted.json'
        ignore = Path(directory) / 'empty-ignore'
        ignore.write_text('')
        command = [executable, 'git', '--config', str(Path(__file__).resolve().parents[1] / '.gitleaks.toml'),
                   '--redact=100', '--no-banner', '--no-color', '--ignore-gitleaks-allow',
                   '--gitleaks-ignore-path', str(ignore), '--report-format', 'json', '--report-path', str(report)]
        if history:
            # Include refs, detached HEAD and even an otherwise unreachable SHA being pushed.
            command += ['--log-opts=' + ' '.join(['--all', '--full-history', 'HEAD', *revisions])]
        else:
            command += ['--pre-commit', '--staged']
        result = subprocess.run(command, capture_output=True)
        try:
            findings = json.loads(report.read_text()) if report.exists() else []
        except (OSError, ValueError):
            raise SystemExit('Gitleaks report unreadable; publication blocked.')
        for finding in findings or []:
            print(f"{finding.get('File', '?')}:{finding.get('StartLine', 0)}: Gitleaks/{finding.get('RuleID', '?')}")
        if findings:
            raise SystemExit(f'Gitleaks blocked publication: {len(findings)} findings; matched values withheld.')
        if result.returncode:
            raise SystemExit('Gitleaks failed; raw output withheld. Check the tool and configuration.')
    print('PASS: Gitleaks ' + ('full reachable history' if history else 'staged diff') + ' scan.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-gitleaks', action='store_true', help='require Gitleaks on staged additions')
    parser.add_argument('--history', action='store_true', help='require Gitleaks on all reachable history')
    parser.add_argument('--pre-push', action='store_true', help='read ref updates from Git on stdin')
    args = parser.parse_args()
    check_index()
    if args.with_gitleaks or args.history or args.pre_push:
        # Both are necessary: history omits staged changes; staged diffs omit deleted old secrets.
        check_gitleaks()
        if args.history or args.pre_push:
            check_gitleaks(history=True, push_input=sys.stdin if args.pre_push else None)
    print('Pattern checks supplement review and cannot guarantee absence of all secrets.')


if __name__ == '__main__':
    main()
