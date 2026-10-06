#!/usr/bin/env python3
"""Retrieve the fixed official renderer for a user's local build; no vendored art."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'b139b45064b4dcecf7bfe97e75bc7f99c10c28b6'
BASE = f'https://raw.githubusercontent.com/facebookincubator/muse-gadget-sdk/{COMMIT}/'
FILES = {
    'esp32/avatar/muse_pixel.c': '72369c740fb9450a11491a9e5e71a5e031f3a835ab037bf03a2c17914b3fb915',
    'esp32/components/muse/muse_pixel.h': 'ce62194bd26e364056dfb507c25b3dce3266a32459764b8cc3479d8ab8f6be97',
    'esp32/components/muse/muse_state.h': 'd9a0f9e6163936e7252a5d9e84ebf63d52fc228c5d19e948ae81f17a09209d7b',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download-only', action='store_true', help='Download on any host; compile later on Linux/WSL with GCC')
    args = parser.parse_args()
    out = ROOT / 'ui/public/esp32-avatar'
    source = out / 'source'
    source.mkdir(parents=True, exist_ok=True)
    print('Fetching official Jollybot renderer for your local build.')
    print('The character is excluded from the SDK Apache license; see THIRD_PARTY.md.')
    for relative, expected in FILES.items():
        target = source / Path(relative).name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        request = urllib.request.Request(BASE + relative, headers={'User-Agent': 'pomtum-muse-avatar-prepare'})
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(2 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != expected:
            raise SystemExit(f'Upstream hash mismatch: {relative}; nothing from this response was saved')
        target.write_bytes(content)
    (out / 'NOTICE.txt').write_text(
        'Jollybot avatar: Copyright (c) Meta Platforms, Inc. and affiliates.\n'
        'The avatar is excluded from the upstream SDK Apache license.\n'
        f'Original source commit: {COMMIT}\n'
        'Generated locally from the unmodified original C renderer.\n'
        'The two components/muse headers retain their own Apache-2.0 notices.\n', encoding='utf-8')
    (out / 'APACHE-2.0-headers.txt').write_bytes((ROOT / 'LICENSE').read_bytes())
    if not args.download_only:
        subprocess.run([sys.executable, str(ROOT / 'ui/tools/avatar-build.py')], check=True)
    print('Avatar preparation complete. These files are gitignored; do not add them to a release archive.')


if __name__ == '__main__':
    main()
