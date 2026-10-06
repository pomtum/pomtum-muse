#!/usr/bin/env bash
# Install only the local companion overlay; the official SDK must already exist.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
exec python3 - "$repo_root" "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

REPO = Path(sys.argv.pop(1))
PREFIX = Path('/opt/pomtum-muse')
MARKER = '# Managed by pomtum-muse installer\n'
parser = argparse.ArgumentParser(description='Install a local companion for an already installed Muse Linux SDK.')
parser.add_argument('--user', required=True, help='same non-root desktop account as SDK --run-as')
parser.add_argument('--voice-model', required=True, help='absolute Piper .onnx path, with adjacent .json')
parser.add_argument('--voice-python', required=True, help='absolute Python executable in the Piper virtualenv')
parser.add_argument('--key-name', default='adc-keys-ai', help='exact dedicated input-device name')
parser.add_argument('--key-code', type=int, default=30, help='Linux key code, 1..767')
parser.add_argument('--dry-run', action='store_true', help='perform read-only preflight and print the complete plan')
args = parser.parse_args()

def fail(message):
    raise SystemExit('pomtum-muse: ' + message)

def command(argv):
    result = subprocess.run(argv, text=True, capture_output=True)
    if result.returncode:
        fail('preflight command failed: ' + argv[0] + '; check the required account, SDK and dependencies')
    return result.stdout.strip()

def clean(value, name):
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail(name + ' must not be empty or contain control characters')
    return value

def absolute(value, name):
    clean(value, name)
    path = Path(value)
    if not path.is_absolute():
        fail(name + ' must be an absolute path')
    return path

def quote(value, *, argument=False):
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if argument:
        value = value.replace('$', '$$')
    return '"' + value + '"'

if not args.dry_run and os.geteuid() != 0:
    fail('run with sudo, or use --dry-run for a read-only plan')
if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*\$?', args.user):
    fail('unsupported account name')
if not 1 <= args.key_code <= 767:
    fail('--key-code must be between 1 and 767')
clean(args.key_name, '--key-name')
if not args.key_name.strip() or len(args.key_name) > 255:
    fail('--key-name must be a non-blank device name of at most 255 characters')
for executable in ('systemctl', 'getent', 'runuser', 'pw-record', 'firefox'):
    if not shutil.which(executable):
        fail('missing prerequisite: ' + executable)
entry = command(['getent', 'passwd', args.user]).split(':')
if len(entry) != 7 or entry[0] != args.user:
    fail('the desktop account was not found')
uid, gid = int(entry[2]), int(entry[3])
if uid == 0:
    fail('the companion must run as a non-root desktop account')
home = absolute(entry[5], 'account home')
if not home.is_dir():
    fail('the desktop account home does not exist')

def show(prop):
    return command(['systemctl', 'show', '--value', '--property=' + prop, 'musegadget.service'])

if show('LoadState') != 'loaded':
    fail('install the official Muse Linux SDK first')
if show('EnvironmentFiles'):
    fail('SDK EnvironmentFile overrides need manual review; use the standard official service configuration')
environment = dict(value.split('=', 1) for value in shlex.split(show('Environment')) if '=' in value)
exec_match = re.search(r'argv\[\]=(.*?) ;', show('ExecStart'))
if not exec_match:
    fail('could not inspect the effective SDK ExecStart; use the standard official installer')
sdk_argv = shlex.split(exec_match.group(1))
if not sdk_argv:
    fail('SDK ExecStart is empty')
run_as = environment.get('MUSEGADGET_RUN_AS', '')
for index, value in enumerate(sdk_argv):
    if value.startswith('--run-as='):
        run_as = value.split('=', 1)[1]
    elif value == '--run-as' and index + 1 < len(sdk_argv):
        run_as = sdk_argv[index + 1]
service_user = show('User')
if service_user not in ('', 'root', '0'):
    run_as = service_user
if run_as != args.user:
    fail('SDK run-as account does not match --user; reconfigure it with the official SDK installer first')
if environment.get('PYTHONPATH', '') not in ('', str(PREFIX / 'sdk/src')):
    fail('SDK already has a different PYTHONPATH overlay; review it before installing this overlay')
socket = absolute(environment.get('MUSEGADGET_SOCKET', '/run/musegadget/musegadget.sock'), 'SDK socket')
sdk_program = absolute(sdk_argv[0], 'SDK executable')
sdk_python = sdk_program if sdk_program.name.startswith('python') else sdk_program.parent / 'python'
voice_python = absolute(args.voice_python, '--voice-python')
model = absolute(args.voice_model, '--voice-model')
for path in (home, model, voice_python, sdk_python):
    if path.resolve().is_relative_to(PREFIX.resolve()):
        fail('the account home, model, voice environment and original SDK must live outside /opt/pomtum-muse')
for path in (sdk_python, voice_python):
    if not path.is_file() or not os.access(path, os.X_OK):
        fail('required Python executable is missing: ' + str(path))
for path in (model, Path(str(model) + '.json')):
    if not path.is_file():
        fail('Piper model and adjacent .onnx.json must both exist')
    command(['runuser', '-u', args.user, '--', 'test', '-r', str(path)])
command(['runuser', '-u', args.user, '--', str(voice_python), '-c', 'from piper import PiperVoice'])
command(['runuser', '-u', args.user, '--', str(sdk_python), '-c', 'import musegadget'])

dist = REPO / 'ui/dist'
manifest_path = dist / 'esp32-avatar/manifest.json'
if not (dist / 'index.html').is_file() or not manifest_path.is_file():
    fail('ui/dist and its esp32-avatar/manifest.json are required; generate the official assets and build the UI first')
try:
    manifest = json.loads(manifest_path.read_text())
    for sequence in manifest['sequences'].values():
        filename = sequence['file']
        if Path(filename).name != filename:
            fail('invalid avatar manifest filename')
        data = (manifest_path.parent / filename).read_bytes()
        if hashlib.sha256(data).hexdigest() != sequence['sha256']:
            fail('avatar asset hash does not match the generated manifest')
    if not manifest['sequences']:
        fail('avatar manifest is empty')
except (KeyError, ValueError, OSError):
    fail('avatar manifest or generated frames are invalid; rebuild the UI assets')
for tree in (REPO/'sdk/src', REPO/'device-io', dist):
    if not tree.is_dir() or tree.is_symlink() or any(path.is_symlink() for path in tree.rglob('*')):
        fail('source trees must exist and contain no symlinks')

base_environment = '\n'.join('Environment=' + quote(value) for value in (
    'HOME=' + str(home), 'XDG_RUNTIME_DIR=/run/user/' + str(uid),
    'PYTHONDONTWRITEBYTECODE=1', 'PYTHONUNBUFFERED=1'))
def service(description, argv, sdk=False):
    return (MARKER + '[Unit]\nDescription=' + description + '\nAfter=user@' + str(uid) + '.service' +
            (' musegadget.service\nWants=musegadget.service' if sdk else '') + '\n\n[Service]\nType=simple\nUser=' + args.user +
            '\n' + base_environment + '\nEnvironment="PYTHONPATH=/opt/pomtum-muse/sdk/src"\nExecStart=' +
            ' '.join(quote(value, argument=True) for value in argv) + '\nRestart=on-failure\nRestartSec=3\nUMask=0077\n' +
            'NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=read-only\n\n[Install]\nWantedBy=multi-user.target\n')

files = {
    '/etc/systemd/system/musegadget.service.d/70-pomtum-muse.conf': MARKER + '[Service]\nEnvironment="PYTHONPATH=/opt/pomtum-muse/sdk/src"\n',
    '/etc/systemd/system/pomtum-muse-bridge.service': service('PomTum Muse local UI bridge',
        [sdk_python, '-m', 'musegadget.http_bridge', '--socket', socket, '--static-dir', PREFIX/'ui'], sdk=True),
    '/etc/systemd/system/pomtum-muse-io.service': service('PomTum Muse dedicated key and local voice',
        [voice_python, PREFIX/'device-io/companion_io.py', '--model', model, '--key-name', args.key_name, '--key-code', str(args.key_code)]),
    '/usr/local/share/applications/pomtum-muse.desktop': MARKER + '[Desktop Entry]\nType=Application\nName=PomTum Muse\nComment=Open the local Muse companion\nExec=/opt/pomtum-muse/bin/open-companion\nIcon=face-smile\nTerminal=false\nCategories=Utility;\n',
}
firefox = shutil.which('firefox')
launcher = ('#!/usr/bin/env bash\nset -euo pipefail\n' +
    '[[ $(id -un) == ' + shlex.quote(args.user) + ' && $EUID -ne 0 ]] || { echo "Open PomTum Muse as its configured desktop user." >&2; exit 1; }\n' +
    'profile="${XDG_CONFIG_HOME:-$HOME/.config}/pomtum-muse/firefox"\nmkdir -p -- "$profile"\nchmod 700 -- "$profile"\n' +
    'python3 - "$profile" <<\'PREFS\'\nfrom pathlib import Path\nimport re, sys\n' +
    'prefs = Path(sys.argv[1]) / "user.js"\ncontent = prefs.read_text() if prefs.exists() else ""\n' +
    'content = re.sub(r\'^\\s*user_pref\\(\\s*"signon.rememberSignons"\\s*,[^\\n]*\\);[ \\t]*$\', "", content, flags=re.M)\n' +
    'prefs.write_text(content.rstrip() + \'\\nuser_pref("signon.rememberSignons", false);\\n\')\nprefs.chmod(0o600)\nPREFS\n' +
    'exec ' + shlex.quote(firefox) + ' --no-remote --profile "$profile" --new-window http://127.0.0.1:17863/\n')
metadata = dict(managed_by='pomtum-muse', format=1, user=args.user, uid=uid, home=str(home))
if PREFIX.exists() or PREFIX.is_symlink():
    try:
        previous = json.loads((PREFIX/'.installed.json').read_text())
        if PREFIX.is_symlink() or previous.get('managed_by') != 'pomtum-muse' or previous.get('format') != 1 or previous.get('user') != args.user:
            fail('existing /opt/pomtum-muse is not this account\'s managed install')
    except (OSError, ValueError):
        fail('refusing to overwrite an unmanaged /opt/pomtum-muse')
if any(parent.is_symlink() for parent in PREFIX.parents):
    fail('installation prefix parents must not be symlinks')
for filename in files:
    path = Path(filename)
    if path.is_symlink() or (path.exists() and not path.read_text().startswith(MARKER)):
        fail('refusing to overwrite an unmanaged file: ' + filename)
    if any(parent.is_symlink() for parent in path.parents):
        fail('installation directories must not be symlinks: ' + filename)
plan = dict(user=args.user, uid=uid, home=str(home), sdk_python=str(sdk_python), socket=str(socket),
    prefix=str(PREFIX), files=files, launcher=launcher, enable=['pomtum-muse-bridge.service', 'pomtum-muse-io.service'],
    sdk_restart=False, credentials_touched=False, groups_changed=False)
if args.dry_run:
    print(json.dumps(plan, indent=2))
    sys.exit(0)

# All preflight checks complete before the first persistent write.
PREFIX.parent.mkdir(parents=True, exist_ok=True)
stage = Path(tempfile.mkdtemp(prefix='.pomtum-muse-install-', dir=PREFIX.parent))
try:
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache', '.git')
    shutil.copytree(REPO/'sdk/src', stage/'sdk/src', ignore=ignore)
    shutil.copytree(REPO/'device-io', stage/'device-io', ignore=ignore)
    shutil.copytree(dist, stage/'ui', ignore=ignore)
    (stage/'bin').mkdir()
    (stage/'bin/open-companion').write_text(launcher)
    (stage/'.installed.json').write_text(json.dumps(metadata, indent=2)+'\n')
    for path in [stage, *stage.rglob('*')]:
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() or path == stage/'bin/open-companion' else 0o644)
    previous = PREFIX.with_name('.pomtum-muse-previous-' + str(os.getpid()))
    if previous.exists():
        fail('stale replacement directory exists; inspect it before retrying')
    if PREFIX.exists():
        PREFIX.rename(previous)
    try:
        stage.rename(PREFIX)
    except BaseException:
        if previous.exists(): previous.rename(PREFIX)
        raise
    if previous.exists(): shutil.rmtree(previous)
    for filename, content in files.items():
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            stream.write(content)
            temporary = Path(stream.name)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'enable', 'pomtum-muse-bridge.service', 'pomtum-muse-io.service'])
finally:
    if stage.exists(): shutil.rmtree(stage)
print('Installed and enabled. Existing SDK credentials, groups, network and Firefox profiles were preserved.')
print('After the desktop user has logged in, activate explicitly:')
print('  sudo systemctl restart musegadget.service pomtum-muse-bridge.service pomtum-muse-io.service')
print('Then open PomTum Muse from the desktop applications menu (or /opt/pomtum-muse/bin/open-companion).')
print('No service was started/restarted and no browser window was opened by this installer.')
PY
