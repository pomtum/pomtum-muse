#!/usr/bin/env bash
# Remove only files carrying this installer's namespace/ownership marker.
set -euo pipefail
exec python3 - "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description='Remove the companion, preserving SDK pairing, voice models and Firefox/chat profiles.')
parser.add_argument('--dry-run', action='store_true', help='inspect and print owned files without modifying anything')
args = parser.parse_args()
prefix = Path('/opt/pomtum-muse')
marker = '# Managed by pomtum-muse installer\n'
files = [Path(name) for name in (
    '/etc/systemd/system/musegadget.service.d/70-pomtum-muse.conf',
    '/etc/systemd/system/pomtum-muse-bridge.service',
    '/etc/systemd/system/pomtum-muse-io.service',
    '/usr/local/share/applications/pomtum-muse.desktop')]
def fail(message): raise SystemExit('pomtum-muse: ' + message)
if not args.dry_run and os.geteuid() != 0: fail('run with sudo, or use --dry-run')
if any(parent.is_symlink() for parent in prefix.parents): fail('installation prefix parents must not be symlinks')
if prefix.exists() or prefix.is_symlink():
    try:
        metadata = json.loads((prefix/'.installed.json').read_text())
        if prefix.is_symlink() or metadata.get('managed_by') != 'pomtum-muse' or metadata.get('format') != 1:
            fail('refusing to remove an unmanaged /opt/pomtum-muse')
    except (ValueError, OSError): fail('refusing to remove an unmanaged /opt/pomtum-muse')
owned = []
for path in files:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        fail('refusing a symlinked installation path: ' + str(path))
    if path.exists():
        if not path.read_text().startswith(marker): fail('refusing to remove an unowned file: ' + str(path))
        owned.append(path)
if args.dry_run:
    print(json.dumps(dict(remove=[str(path) for path in owned] + ([str(prefix)] if prefix.exists() else []),
        preserve=['/opt/musegadget', '/var/lib/musegadget', 'voice model and Python environment', 'all Firefox/chat profiles'],
        sdk_restart=False), indent=2))
else:
    units = [path.name for path in owned if path.name.startswith('pomtum-muse-') and path.suffix == '.service']
    if units:
        subprocess.run(['systemctl', 'disable', '--now', *units], check=True)
    for path in owned: path.unlink()
    if prefix.exists(): shutil.rmtree(prefix)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('Companion removed. SDK installation, pairing, model and Firefox/chat profiles are preserved.')
    print('Restore the running SDK to its original installed Python package when convenient:')
    print('  sudo systemctl restart musegadget.service')
PY
