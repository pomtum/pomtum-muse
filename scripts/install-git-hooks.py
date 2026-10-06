#!/usr/bin/env python3
"""Enable this checkout's publication hooks without replacing another setup."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not shutil.which('gitleaks'):
    raise SystemExit('Install Gitleaks 8.30.1+ first; see docs/development.md.')
existing = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], cwd=ROOT,
                          capture_output=True, text=True)
if existing.returncode not in (0, 1):
    raise SystemExit('Cannot inspect Git hooks configuration.')
if existing.stdout.strip() not in ('', '.githooks'):
    raise SystemExit('Existing core.hooksPath is preserved. Integrate these checks into your hooks manually.')
hooks = subprocess.check_output(['git', 'rev-parse', '--git-path', 'hooks'], cwd=ROOT, text=True).strip()
for name in ('pre-commit', 'pre-push'):
    path = Path(hooks) / name
    if not path.is_absolute():
        path = ROOT / path
    if existing.stdout.strip() == '' and path.exists():
        raise SystemExit('Existing Git hooks are preserved; integrate publication checks manually.')
    (ROOT / '.githooks' / name).chmod(0o755)
subprocess.run(['git', 'config', '--local', 'core.hooksPath', '.githooks'], cwd=ROOT, check=True)
print('Enabled local pre-commit and full-history pre-push checks. Other repositories are unchanged.')
