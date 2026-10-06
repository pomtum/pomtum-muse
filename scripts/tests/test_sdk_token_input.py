"""Private-token installer contract, using synthetic tokens and inert install steps.

All writes stay under TemporaryDirectory. No real SDK install, sudo, systemctl,
network, Bluetooth, or device command is run. Linux/WSL only for shell/TTY checks.
"""
import errno
import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = 'mgst_' + 'A' * 42 + 'w'  # Synthetic canonical fixture, never a real token.


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'needs POSIX bash and terminal semantics')
class SDKTokenInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='muse-private-token-test-')
        self.root = Path(self.temporary.name)
        self.state = self.root/'state'
        self.commands = self.root/'commands'
        self.commands.mkdir()
        self.log = self.root/'python-calls.jsonl'
        self.action_log = self.root/'install-actions'
        self.script = self.root/'install.sh'
        source = (ROOT/'sdk/install.sh').read_text()
        if os.getuid() != 0:
            # Non-root test hosts cannot chown; the dedicated root-only case below
            # checks real ownership repair. Other tests still exercise safe input/save.
            source = source.replace('    os.chown(directory, 0, 0)', '    pass  # test host cannot chown root')
            source = source.replace('        os.fchown(target.fileno(), 0, 0)', '        pass  # test host cannot fchown root')
        # Only private test copies replace installation side effects with inert functions.
        harness = r'''
STATE_DIR="$TEST_STATE_DIR"
as_root() { "$@"; }
check_system() { printf 'checked\n' >> "$TEST_ACTION_LOG"; }
choose_account() { :; }
install_packages() { :; }
enable_bluez() { :; }
install_uv() { :; }
install_musegadget() { :; }
configure_bluez() { :; }
install_service() { :; }
pair() { :; }
summary() { [ -z "$SDK_TOKEN" ] || die "test: token variable was not cleared after save"; }
uninstall() { :; }
main "$@"
'''
        self.assertTrue(source.rstrip().endswith('main "$@"'))
        self.script.write_text(source.rsplit('main "$@"', 1)[0] + harness)
        self.env = dict(os.environ, PATH=str(self.commands)+os.pathsep+os.environ['PATH'],
                        TEST_STATE_DIR=str(self.state), TEST_ACTION_LOG=str(self.action_log), TEST_LOG=str(self.log))
        for key in ('BASH_ENV', 'ENV', 'SDK_TOKEN', 'MUSEGADGET_SDK_TOKEN', 'SHELLOPTS'):
            self.env.pop(key, None)
        # Log booleans, not environment contents. The real Python receives original stdin.
        recorder = f'''#!{sys.executable}
import json, os, sys
fixture='mgst_'+'A'*42+'w'
record={{'argv_contains_token':any(fixture in value for value in sys.argv),
        'env_contains_token':any(fixture in value for value in os.environ.values()),
        'token_env_names':[key for key in ('SDK_TOKEN','MUSEGADGET_SDK_TOKEN') if key in os.environ]}}
with open(os.environ['TEST_LOG'],'a') as stream: stream.write(json.dumps(record)+'\\n')
os.execv({sys.executable!r},[{sys.executable!r},*sys.argv[1:]])
'''
        (self.commands/'python3').write_text(recorder)
        (self.commands/'python3').chmod(0o755)
        (self.commands/'id').write_text('#!/bin/sh\nprintf "0\\n"\n')
        (self.commands/'id').chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def token_file(self, data=None, mode=0o600):
        path = self.root/'private input'
        path.write_bytes((TOKEN+'\n').encode() if data is None else data)
        path.chmod(mode)
        return path

    def run_installer(self, *args, data=None, trace=False):
        command = ['bash', *(['-x'] if trace else []), str(self.script), *map(str,args)]
        return subprocess.run(command, input=data, env=self.env, capture_output=True, timeout=8)

    def assert_private(self, result):
        combined = result.stdout + result.stderr
        self.assertNotIn(TOKEN.encode(), combined)
        if self.log.exists():
            records = [json.loads(line) for line in self.log.read_text().splitlines()]
            for record in records:
                self.assertFalse(record['argv_contains_token'])
                self.assertFalse(record['env_contains_token'])
                self.assertEqual(record['token_env_names'], [])

    def assert_saved(self):
        saved = self.state/'sdk_token'
        self.assertEqual(saved.read_bytes(), (TOKEN+'\n').encode())
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(saved.stat().st_uid, os.getuid())
        self.assertEqual(list(self.state.glob('.sdk_token-*')), [])

    def test_legacy_and_unknown_arguments_fail_without_echo(self):
        for args in (('--sdk-token',TOKEN), ('--sdk-token='+TOKEN,), ('--unknown',TOKEN), (TOKEN,)):
            with self.subTest(option=args[0].split('=')[0]):
                result=self.run_installer(*args, trace=True)
                self.assertNotEqual(result.returncode,0)
                self.assert_private(result)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.action_log.exists())
        self.assertIn(b'--sdk-token-file', self.run_installer('--sdk-token',TOKEN).stderr)

    def test_private_file_is_saved_atomically_with_private_modes(self):
        self.state.mkdir(mode=0o755)
        (self.state/'sdk_token').write_bytes(b'previous token fixture\n')
        (self.state/'pairing.json').write_bytes(b'pairing fixture unchanged')
        result=self.run_installer('--sdk-token-file',self.token_file(), '--yes', trace=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assert_private(result)
        self.assert_saved()
        self.assertEqual((self.state/'pairing.json').read_bytes(),b'pairing fixture unchanged')

    def test_stdin_and_crlf_file_are_supported(self):
        result=self.run_installer('--sdk-token-file','-', '--yes', data=(TOKEN+'\n').encode())
        self.assertEqual(result.returncode,0,result.stderr)
        self.assert_private(result)
        self.assert_saved()
        result=self.run_installer('--sdk-token-file',self.token_file((TOKEN+'\r\n').encode()), '--no-pair')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assert_private(result)
        self.assert_saved()

    def test_bad_empty_multiline_binary_and_oversized_inputs_fail_early(self):
        invalid = [b'', b'mgst_short', ('mgst_'+'A'*42+'B').encode(),
                   (TOKEN+'\n'+TOKEN).encode(), (TOKEN+'\x00').encode(),
                   (' '+TOKEN+' ').encode(), b'x'*257, b'x'*(1024*1024)]
        for data in invalid:
            with self.subTest(length=len(data)):
                result=self.run_installer('--sdk-token-file',self.token_file(data), '--yes')
                self.assertNotEqual(result.returncode,0)
                self.assert_private(result)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.action_log.exists())

    def test_nonprivate_missing_directory_and_fifo_sources_are_rejected(self):
        fifo=self.root/'token-fifo'
        os.mkfifo(fifo)
        for path in (self.token_file(mode=0o644), self.root/'missing', self.root, fifo):
            with self.subTest(kind=path.name):
                result=self.run_installer('--sdk-token-file',path,'--yes')
                self.assertNotEqual(result.returncode,0)
                self.assert_private(result)
                self.assertFalse(self.state.exists())

    def test_duplicate_or_missing_file_option_fails_without_installation(self):
        path=self.token_file()
        for args in (('--sdk-token-file',), ('--sdk-token-file',''),
                     ('--sdk-token-file',path,'--sdk-token-file',path)):
            result=self.run_installer(*args)
            self.assertNotEqual(result.returncode,0)
            self.assert_private(result)
            self.assertFalse(self.state.exists())

    def test_inherited_xtrace_verbose_allexport_and_token_environment_are_cleared(self):
        startup=self.root/'startup.sh'
        startup.write_text('set -x -v -a\n')
        self.env.update(BASH_ENV=str(startup), SDK_TOKEN=TOKEN, MUSEGADGET_SDK_TOKEN=TOKEN)
        result=self.run_installer('--sdk-token-file',self.token_file(),'--yes',trace=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assert_private(result)
        self.assert_saved()

    def test_yes_and_no_pair_keep_existing_token_without_prompt_or_environment_input(self):
        self.state.mkdir(mode=0o700)
        original=b'existing token fixture\n'
        (self.state/'sdk_token').write_bytes(original)
        self.env['MUSEGADGET_SDK_TOKEN']=TOKEN
        for option in ('--yes','--no-pair'):
            result=self.run_installer(option,data=(TOKEN+'\n').encode())
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn(b'input hidden',result.stdout+result.stderr)
            self.assert_private(result)
            self.assertEqual((self.state/'sdk_token').read_bytes(),original)

    def test_failed_atomic_replace_preserves_previous_file_and_removes_temp(self):
        self.state.mkdir(mode=0o700)
        saved=self.state/'sdk_token'
        saved.write_bytes(b'previous token fixture\n')
        original=self.script.read_text()
        self.script.write_text(original.replace('    os.replace(temporary, os.path.join(directory, "sdk_token"))',
                                                '    raise OSError("simulated replace failure")'))
        result=self.run_installer('--sdk-token-file',self.token_file(),'--yes')
        self.assertNotEqual(result.returncode,0)
        self.assert_private(result)
        self.assertEqual(saved.read_bytes(),b'previous token fixture\n')
        self.assertEqual(list(self.state.glob('.sdk_token-*')),[])

    def test_atomic_save_does_not_follow_an_existing_target_symlink(self):
        self.state.mkdir(mode=0o700)
        unrelated=self.root/'unrelated'
        unrelated.write_bytes(b'keep unrelated file')
        (self.state/'sdk_token').symlink_to(unrelated)
        result=self.run_installer('--sdk-token-file',self.token_file(),'--yes')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assert_private(result)
        self.assert_saved()
        self.assertFalse((self.state/'sdk_token').is_symlink())
        self.assertEqual(unrelated.read_bytes(),b'keep unrelated file')

    @unittest.skipUnless(getattr(os, 'geteuid', lambda: -1)() == 0, 'real ownership repair requires root in the isolated temp directory')
    def test_existing_wrong_directory_owner_is_repaired_before_private_save(self):
        self.state.mkdir(mode=0o777)
        (self.state/'sdk_token').write_bytes(b'old token fixture\n')
        os.chown(self.state,65534,65534)
        os.chown(self.state/'sdk_token',65534,65534)
        self.assertNotEqual(self.state.stat().st_uid,0)
        result=self.run_installer('--sdk-token-file',self.token_file(),'--yes')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assert_private(result)
        self.assert_saved()
        self.assertEqual((self.state.stat().st_uid,self.state.stat().st_gid),(0,0))
        self.assertEqual(((self.state/'sdk_token').stat().st_uid,(self.state/'sdk_token').stat().st_gid),(0,0))

    def tty_prompt(self, answer, *, args=()):
        import pty
        pid, master=pty.fork()
        if pid==0:
            os.execvpe('bash',['bash','-x',str(self.script),*args],self.env)
        output=bytearray()
        status=None
        sent=False
        deadline=time.monotonic()+8
        try:
            while time.monotonic()<deadline:
                if select.select([master],[],[],.05)[0]:
                    try:
                        chunk=os.read(master,4096)
                    except OSError as exc:
                        if exc.errno==errno.EIO: break
                        raise
                    if not chunk: break
                    output += chunk
                    if not sent and b'input hidden, Enter to keep/skip' in output:
                        os.write(master,answer+b'\n')
                        sent=True
                ended,result=os.waitpid(pid,os.WNOHANG)
                if ended:
                    status=result
                    break
            self.assertTrue(sent,'hidden prompt was not reached')
            if status is None:
                ended,status=os.waitpid(pid,os.WNOHANG)
                if not ended:
                    os.kill(pid,signal.SIGKILL)
                    _,status=os.waitpid(pid,0)
                    self.fail('hidden prompt timed out')
            return os.waitstatus_to_exitcode(status),bytes(output)
        finally:
            os.close(master)
            if status is None:
                try:
                    os.kill(pid,signal.SIGKILL)
                    os.waitpid(pid,0)
                except ProcessLookupError:
                    pass

    def test_default_prompt_is_hidden_and_saves_without_argv_or_environment_transport(self):
        code,output=self.tty_prompt(TOKEN.encode())
        self.assertEqual(code,0,output)
        self.assertNotIn(TOKEN.encode(),output)
        self.assert_saved()
        self.assert_private(subprocess.CompletedProcess([],code,output,b''))

    def test_blank_hidden_prompt_keeps_existing_token(self):
        self.state.mkdir(mode=0o700)
        (self.state/'sdk_token').write_bytes(b'keep existing token\n')
        code,output=self.tty_prompt(b'')
        self.assertEqual(code,0,output)
        self.assertEqual((self.state/'sdk_token').read_bytes(),b'keep existing token\n')

    def test_cli_guidance_and_help_do_not_suggest_value_arguments(self):
        cli=(ROOT/'sdk/src/musegadget/cli.py').read_text()
        self.assertNotIn('--sdk-token mgst_',cli)
        result=self.run_installer('--help')
        self.assertEqual(result.returncode,0)
        self.assertIn(b'--sdk-token-file PATH',result.stdout)
        self.assertNotIn(b'--sdk-token TOKEN',result.stdout)


if __name__=='__main__':
    unittest.main()
