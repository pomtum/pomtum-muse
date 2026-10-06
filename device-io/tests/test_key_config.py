import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import companion_io as companion
from test_companion_io import Clock, FakeCapture


class KeyConfigurationTests(unittest.TestCase):
    def test_cli_defaults_and_custom_device(self):
        args = companion.parse_args([])
        self.assertEqual((args.key_name, args.key_code), ('adc-keys-ai', 30))
        args = companion.parse_args(['--key-name', 'Dedicated assistant key', '--key-code', '183'])
        self.assertEqual((args.key_name, args.key_code), ('Dedicated assistant key', 183))

    def test_invalid_config_is_rejected_before_starting(self):
        for arguments in (['--key-code', '0'], ['--key-code', '768'], ['--key-code', 'x'],
                          ['--key-name', ''], ['--key-name', '  '], ['--key-name', 'foo\nbar']):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                companion.parse_args(arguments)

    def test_custom_discovery_still_requires_one_exact_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, name in ((1, 'adc-keys-ai'), (2, 'Dedicated assistant key'), (3, 'Dedicated assistant key extra')):
                device = root / f'event{number}' / 'device'
                device.mkdir(parents=True)
                (device / 'name').write_text(name + '\n')
            self.assertEqual(companion.discover_key(root, root, key_name='Dedicated assistant key'), root/'event2')
            duplicate = root/'event4/device'
            duplicate.mkdir(parents=True)
            (duplicate/'name').write_text('Dedicated assistant key\n')
            self.assertIsNone(companion.discover_key(root, root, key_name='Dedicated assistant key'))
            self.assertEqual(companion.discover_key(root, root), root/'event1')

    def test_only_configured_key_can_capture_and_focus_is_still_required(self):
        FakeCapture.instances = []
        controller = companion.Controller(clock=Clock(), capture_factory=FakeCapture)
        reader = companion.KeyReader(controller, key_name='Dedicated assistant key', key_code=183)
        def event(code, value):
            return companion.INPUT_EVENT.pack(0, 0, companion.EV_KEY, code, value)
        try:
            reader._events(event(183, 1), False)
            self.assertEqual(FakeCapture.instances, [])
            controller.subscribe()
            controller.focus(True)
            reader._events(event(30, 1) + event(30, 0), False)
            self.assertEqual(FakeCapture.instances, [])
            waiting = reader._events(event(183, 1) + event(30, 0), True)
            self.assertTrue(waiting)
            waiting = reader._events(event(183, 0), waiting)
            self.assertFalse(waiting)
            reader._events(event(183, 1) + event(183, 2), waiting)
            self.assertEqual(len(FakeCapture.instances), 1)
            controller.focus(False)
            self.assertTrue(FakeCapture.instances[0].stop.is_set())
            self.assertFalse(FakeCapture.instances[0].send)
        finally:
            controller.close()


if __name__ == '__main__':
    unittest.main()
