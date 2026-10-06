"""Isolated installer tests. Every destination and system command is a temporary fake.

Run on Linux: python3 -B -m unittest discover -s scripts/tests -v
No real systemctl, account, SDK, device, or root filesystem is modified.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pomtum-installer-test-')
        self.root = Path(self.temporary.name)
        self.repo = self.root/'repo'
        (self.repo/'scripts').mkdir(parents=True)
        self.fake = self.root/'commands'
        self.fake.mkdir()
        self.home = self.root/'home/player'
        self.home.mkdir(parents=True)
        self.prefix = self.root/'isolated/opt/pomtum-muse'
        self.units = self.root/'isolated/etc/systemd/system'
        self.applications = self.root/'isolated/usr/local/share/applications'
        self.log = self.root/'commands.jsonl'
        self.sdk_bin = self.root/'original-sdk/venv/bin'
        self.sdk_bin.mkdir(parents=True)
        for name in ('python', 'musegadget'):
            (self.sdk_bin/name).write_text('#!/bin/sh\nexit 0\n')
            (self.sdk_bin/name).chmod(0o755)
        self.model = self.root/'voice model.onnx'
        self.model.write_bytes(b'model fixture')
        Path(str(self.model)+'.json').write_text('{}')
        source = self.repo/'sdk/src/musegadget'
        source.mkdir(parents=True)
        (source/'__init__.py').write_text('')
        (self.repo/'device-io').mkdir()
        (self.repo/'device-io/companion_io.py').write_text('')
        self.avatar = self.repo/'ui/dist/esp32-avatar'
        self.avatar.mkdir(parents=True)
        (self.repo/'ui/dist/index.html').write_text('<canvas></canvas>')
        self.png = b'generated frame fixture'
        (self.avatar/'idle.png').write_bytes(self.png)
        (self.avatar/'manifest.json').write_text(json.dumps({'sequences': {'idle': {
            'file':'idle.png', 'sha256':hashlib.sha256(self.png).hexdigest()}}}))
        shim = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name=pathlib.Path(sys.argv[0]).name
with open(os.environ['FAKE_LOG'],'a') as f: f.write(json.dumps([name,*sys.argv[1:]])+'\\n')
if name=='getent': print('player:x:1432:1432::'+os.environ['FAKE_HOME']+':/bin/bash')
elif name=='systemctl' and 'show' in sys.argv:
    prop=next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--property='))
    values={'LoadState':'loaded','Environment':'MUSEGADGET_RUN_AS='+os.environ.get('FAKE_RUN_AS','player')+' SECRET=fake_secret_never_echo','ExecStart':'{ path='+os.environ['FAKE_SDK']+'/musegadget ; argv[]='+os.environ['FAKE_SDK']+'/musegadget run '+os.environ.get('FAKE_ARGS','')+' ; ignore_errors=no ; }','User':'','EnvironmentFiles':os.environ.get('FAKE_ENV_FILES','')}
    print(values[prop])
'''
        for name in ('getent','systemctl','runuser','firefox','pw-record'):
            path = self.fake/name
            path.write_text(shim)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.fake)+os.pathsep+os.environ['PATH'],
            FAKE_LOG=str(self.log), FAKE_HOME=str(self.home), FAKE_SDK=str(self.sdk_bin))
        # Rewrite constants only in disposable test copies, never in shipping scripts.
        for name in ('install-companion.sh','uninstall-companion.sh'):
            script = (SCRIPTS/name).read_text()
            script = script.replace('/opt/pomtum-muse',str(self.prefix)).replace('/etc/systemd/system',str(self.units))
            script = script.replace('/usr/local/share/applications',str(self.applications))
            script = script.replace('os.geteuid() != 0','False')
            (self.repo/'scripts'/name).write_text(script)

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, *extra):
        return subprocess.run(['bash', str(self.repo/'scripts/install-companion.sh'), '--user', 'player',
            '--voice-model', str(self.model), '--voice-python', str(self.sdk_bin/'python'), *extra],
            env=self.env, text=True, capture_output=True)

    def uninstall(self, *extra):
        return subprocess.run(['bash', str(self.repo/'scripts/uninstall-companion.sh'), *extra],
            env=self.env, text=True, capture_output=True)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_dry_run_is_read_only_and_dynamic(self):
        result = self.install('--dry-run','--key-name','Dedicated assistant key','--key-code','183')
        self.assertEqual(result.returncode,0,result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['uid'],1432)
        self.assertEqual(plan['home'],str(self.home))
        combined='\n'.join(plan['files'].values())
        self.assertIn('User=player',combined)
        self.assertIn('XDG_RUNTIME_DIR=/run/user/1432',combined)
        self.assertIn('"--key-name" "Dedicated assistant key" "--key-code" "183"',combined)
        self.assertIn('--no-remote --profile',plan['launcher'])
        self.assertNotIn('marionette',plan['launcher'].lower())
        self.assertIn('user_pref("signon.rememberSignons", false)',plan['launcher'])
        syntax=subprocess.run(['bash','-n'],input=plan['launcher'],text=True,capture_output=True)
        self.assertEqual(syntax.returncode,0,syntax.stderr)
        self.assertNotIn('fake_secret_never_echo',result.stdout)
        self.assertFalse(self.prefix.exists())
        self.assertTrue(all(command[1]=='show' for command in self.commands() if command[0]=='systemctl'))

    def test_firefox_preference_preserves_other_settings_and_is_idempotent(self):
        result=self.install('--dry-run')
        self.assertEqual(result.returncode,0,result.stderr)
        launcher=json.loads(result.stdout)['launcher']
        code=re.search(r"<<'PREFS'\n(.*?)\nPREFS\n",launcher,re.S).group(1)
        profile=self.root/'firefox profile'
        profile.mkdir()
        prefs=profile/'user.js'
        prefs.write_text('user_pref("test.existing", true);\nuser_pref("signon.rememberSignons", true);\n')
        for _ in range(2):
            run=subprocess.run([sys.executable,'-',str(profile)],input=code,text=True,capture_output=True)
            self.assertEqual(run.returncode,0,run.stderr)
        content=prefs.read_text()
        self.assertIn('user_pref("test.existing", true);',content)
        self.assertEqual(content.count('signon.rememberSignons'),1)
        self.assertIn('user_pref("signon.rememberSignons", false);',content)

    def test_mismatched_run_as_fails_before_writes(self):
        self.env['FAKE_RUN_AS']='someoneelse'
        result=self.install('--dry-run')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('does not match',result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_cli_run_as_takes_precedence_and_envfile_is_rejected(self):
        self.env['FAKE_RUN_AS']='someoneelse'
        self.env['FAKE_ARGS']='--run-as player'
        self.assertEqual(self.install('--dry-run').returncode,0)
        self.env['FAKE_ENV_FILES']='/etc/custom-secret-environment'
        result=self.install('--dry-run')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('EnvironmentFile',result.stderr)

    def test_missing_manifest_or_changed_asset_fails_before_writes(self):
        (self.avatar/'manifest.json').rename(self.avatar/'manifest.saved')
        self.assertNotEqual(self.install().returncode,0)
        self.assertFalse(self.prefix.exists())
        (self.avatar/'manifest.saved').rename(self.avatar/'manifest.json')
        (self.avatar/'idle.png').write_bytes(b'corrupted')
        result=self.install()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('hash',result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_isolated_install_upgrade_and_uninstall_preserve_sdk_and_profile(self):
        # Test copies use only the temporary namespace; suppress chown outside a root test process.
        path=self.repo/'scripts/install-companion.sh'
        path.write_text(path.read_text().replace('os.chown(path, 0, 0)','pass  # ownership is checked by production root preflight'))
        profile=self.home/'.config/pomtum-muse/firefox'
        profile.mkdir(parents=True)
        history=profile/'storage.sqlite'
        history.write_bytes(b'private local history fixture')
        credentials=self.root/'original-sdk/pairing.json'
        credentials.write_bytes(b'paired fixture')
        unrelated=self.units/'musegadget.service.d/20-existing.conf'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('[Service]\nEnvironment=UNCHANGED=1\n')
        for _ in range(2):
            result=self.install()
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.prefix/'sdk/src/musegadget/__init__.py').is_file())
        self.assertTrue((self.prefix/'ui/esp32-avatar/manifest.json').is_file())
        system_calls=[command for command in self.commands() if command[0]=='systemctl' and command[1]!='show']
        self.assertTrue(all(command[1] in ('daemon-reload','enable') for command in system_calls))
        self.assertFalse(any('--now' in command for command in system_calls))
        dry=self.uninstall('--dry-run')
        self.assertEqual(dry.returncode,0,dry.stderr)
        self.assertTrue(self.prefix.exists())
        result=self.uninstall()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertEqual(history.read_bytes(),b'private local history fixture')
        self.assertEqual(credentials.read_bytes(),b'paired fixture')
        self.assertTrue(self.model.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue((self.sdk_bin/'python').exists())

    def test_unmanaged_destination_is_not_overwritten_or_removed(self):
        self.prefix.mkdir(parents=True)
        (self.prefix/'foreign-data').write_text('keep')
        self.assertNotEqual(self.install().returncode,0)
        self.assertNotEqual(self.uninstall().returncode,0)
        self.assertEqual((self.prefix/'foreign-data').read_text(),'keep')

    def test_model_in_managed_prefix_is_rejected_so_uninstall_cannot_delete_it(self):
        self.prefix.mkdir(parents=True)
        self.model=self.prefix/'personal-model.onnx'
        self.model.write_bytes(b'keep this model')
        Path(str(self.model)+'.json').write_text('{}')
        result=self.install('--dry-run')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('must live outside',result.stderr)
        self.assertEqual(self.model.read_bytes(),b'keep this model')


if __name__=='__main__':
    unittest.main()
