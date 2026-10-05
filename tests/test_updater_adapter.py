"""Read-only updater recognition; synthetic app fixtures, no scheduler or apps."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'updater' / 'adapter.py'


def archive(version):
    data = json.dumps({'version': version}).encode()
    header = json.dumps({'files': {'package.json': {'size': len(data), 'offset': '0'}}}).encode()
    payload = struct.pack('<I', len(header)) + header
    payload += b'\0' * (-len(payload) % 4)
    pickle = struct.pack('<I', len(payload)) + payload
    return struct.pack('<II', 4, len(pickle)) + pickle + data


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(ADAPTER.is_file(), 'Claude updater adapter is missing')
        spec = importlib.util.spec_from_file_location('claude_test_adapter', ADAPTER)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.app = self.base / 'Claude fixture'
        self.app.mkdir()
        self.adapter.SYSTEM = 'linux'
        self.core = self.adapter._core()
        self.core.SYSTEM = 'linux'
        self.core.FILES = ['resources/app.asar', 'resources/en-US.json']
        self.require_closed = self.core.require_closed
        self.profile = {'platform': 'linux', 'arch': 'amd64', 'version': '2.9939.4',
                        'preloads': ['.vite/build/mainView.js', '.vite/build/mainWindow.js']}
        self.original = {'resources/app.asar': archive(self.profile['version']),
                         'resources/en-US.json': b'{"hello":"Hello"}'}
        (self.app / 'resources').mkdir()
        for name, data in self.original.items():
            (self.app / name).write_bytes(data)
        self.profile['asar_sha256'] = self.core.sha(self.original['resources/app.asar'])
        self.profile['native_sha256'] = self.core.sha(self.original['resources/en-US.json'])
        self.key = 'linux-amd64-' + self.profile['version']
        self.addCleanup(patch.stopall)
        patch.object(self.core, 'profiles', return_value={self.key: self.profile}).start()
        patch.object(self.core, 'require_closed', return_value=None).start()
        patch.object(self.core, 'identity', return_value=(os.getuid(), os.getgid(), self.base / 'legacy-state')).start()

    def save_installed(self, phase='installed'):
        state_dir = self.base / 'state'
        state_dir.mkdir(mode=0o700)
        backup = 'backup-' + 'a' * 32
        records = {}
        for name, data in self.original.items():
            translated = data + b' translated'
            for kind, value in [('original', data), ('patched', translated)]:
                target = state_dir / backup / kind / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value)
                target.chmod(0o600)
            (self.app / name).write_bytes(translated)
            info = (self.app / name).stat()
            records[name] = {'original': self.core.sha(data), 'patched': self.core.sha(translated),
                             'mode': 0o644, 'uid': info.st_uid, 'gid': info.st_gid}
        state = {'schema': 1, 'product': 'claude-russian-portable', 'platform': 'linux',
                 'profile': self.key, 'phase': phase, 'app': str(self.app),
                 'backup': backup, 'files': records}
        (state_dir / 'state.json').write_text(json.dumps(state))
        (state_dir / 'state.json').chmod(0o600)
        return state_dir, state

    def test_supported_source_is_read_only(self):
        before = {name: (self.app / name).read_bytes() for name in self.original}
        result = self.adapter.probe(self.app, [])
        self.assertEqual(result['kind'], 'source')
        self.assertEqual(result['version'], self.profile['version'])
        self.assertRegex(result['fingerprint'], '^[a-f0-9]{64}$')
        self.assertEqual(before, {name: (self.app / name).read_bytes() for name in self.original})
        self.assertFalse((self.base / 'legacy-state').exists())

    def test_native_hash_is_required(self):
        (self.app / 'resources/en-US.json').write_bytes(b'other native catalog')
        self.assertEqual(self.adapter.probe(self.app, [])['kind'], 'unknown')

    def test_source_fingerprint_includes_installation_path(self):
        import shutil
        clone = self.base / 'Claude second'
        shutil.copytree(self.app, clone)
        self.assertNotEqual(self.adapter.probe(self.app, [])['fingerprint'],
                            self.adapter.probe(clone, [])['fingerprint'])

    def test_valid_patch_recognized_using_core_state(self):
        original_fingerprint = self.adapter.probe(self.app, [])['fingerprint']
        directory, _ = self.save_installed()
        result = self.adapter.probe(self.app, [directory])
        self.assertEqual(result['kind'], 'patched')
        self.assertEqual(result['state_dir'], str(directory))
        self.assertEqual(result['fingerprint'], original_fingerprint)

    def test_backup_corruption_fails_closed(self):
        directory, state = self.save_installed()
        (directory / state['backup'] / 'original/resources/app.asar').write_bytes(b'tampered')
        self.assertEqual(self.adapter.probe(self.app, [directory])['kind'], 'recovery')

    def test_unknown_changed_patch_is_recovery(self):
        directory, _ = self.save_installed()
        (self.app / 'resources/en-US.json').write_bytes(b'tampered')
        self.assertEqual(self.adapter.probe(self.app, [directory])['kind'], 'recovery')

    def test_vendor_update_can_outlive_valid_old_state(self):
        directory, _ = self.save_installed()
        for name, data in self.original.items():
            (self.app / name).write_bytes(data)
        self.assertEqual(self.adapter.probe(self.app, [directory])['kind'], 'source')

    def test_unfinished_operation_blocks_reapplication(self):
        directory, _ = self.save_installed('prepared')
        self.assertEqual(self.adapter.probe(self.app, [directory])['kind'], 'recovery')

    def test_missing_current_file_does_not_hide_unfinished_state(self):
        directory, _ = self.save_installed('prepared')
        (self.app / 'resources/app.asar').unlink()
        self.assertEqual(self.adapter.probe(self.app, [directory])['kind'], 'recovery')

    def test_running_app_is_busy(self):
        with patch.object(self.core, 'require_closed', side_effect=RuntimeError('Claude still running')):
            self.assertEqual(self.adapter.probe(self.app, [])['kind'], 'busy')

    def test_probe_does_not_print_shipit_messages_into_json_bridge(self):
        import contextlib
        import io
        def waiting(_):
            print('Waiting for ShipIt')
        output = io.StringIO()
        with patch.object(self.core, 'require_closed', side_effect=waiting), contextlib.redirect_stdout(output):
            self.assertEqual(self.adapter.probe(self.app, [])['kind'], 'source')
        self.assertEqual(output.getvalue(), '')

    def test_symlink_state_fails_closed(self):
        directory, _ = self.save_installed()
        linked = self.base / 'linked-state'
        linked.symlink_to(directory, target_is_directory=True)
        self.assertEqual(self.adapter.probe(self.app, [linked])['kind'], 'recovery')

    def test_windows_discovery_follows_latest_numeric_folder(self):
        self.adapter.SYSTEM = 'windows'
        for name in ['app-2.9.0', 'app-2.100.0', 'app-2.99.0']:
            resources = self.base / name / 'resources'
            resources.mkdir(parents=True)
            (resources / 'app.asar').write_bytes(b'fixture')
        self.assertEqual(self.adapter.discover(self.base / 'app-2.9.0'), self.base / 'app-2.100.0')

    def test_windows_probe_waits_for_entire_squirrel_installation(self):
        import shutil
        selected = self.base / 'app-2.100.0'
        shutil.copytree(self.app, selected)
        self.adapter.SYSTEM = 'windows'
        with patch.object(self.core, 'require_closed', return_value=None) as closed:
            self.adapter.probe(selected, [])
            closed.assert_called_once_with(self.base)

    def test_windows_core_rechecks_old_sibling_before_mutation(self):
        self.core.SYSTEM = 'windows'
        old = self.base / 'app-2.9.0/claude.exe'
        selected = self.base / 'app-2.100.0'
        processes = [{'ProcessId': 456, 'Name': 'claude.exe', 'ExecutablePath': str(old)}]
        response = subprocess.CompletedProcess([], 0, json.dumps(processes), '')
        with patch.object(self.core.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'ещё работает'):
                self.require_closed(selected)

    def test_command_never_approves_signature_without_consent(self):
        self.adapter.SYSTEM = 'windows'
        without = self.adapter.command('install', self.app, self.base / 'state', False)
        with_consent = self.adapter.command('install', self.app, self.base / 'state', True)
        restore = self.adapter.command('restore', self.app, self.base / 'state', True)
        self.assertNotIn('--approve-exe-signature', without)
        self.assertIn('--approve-exe-signature', with_consent)
        self.assertNotIn('--approve-exe-signature', restore)
        self.assertEqual(without[0], sys.executable)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS core state and manifest checks')
class MacAdapterTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('claude_mac_test_adapter', ADAPTER)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.app = self.base / 'Claude.app'
        (self.app / 'Contents/Resources').mkdir(parents=True)
        (self.app / 'Contents/MacOS').mkdir()
        (self.app / 'Contents/Resources/app.asar').write_bytes(archive('2.9939.4'))
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '2.9939.4'}))
        (self.app / 'Contents/MacOS/Claude').write_bytes(b'original executable')
        self.core = self.adapter._core()
        self.addCleanup(patch.stopall)
        patch.object(self.core, 'PROFILES', {'2.9939.4': {
            'source_asar_sha256': self.core.file_hash(self.app / 'Contents/Resources/app.asar')}}).start()
        patch.object(self.core, 'require_closed', return_value=None).start()

    def installed(self):
        import shutil
        directory = self.base / 'state'
        directory.mkdir(mode=0o700)
        original = self.core.manifest(self.app)
        backup = directory / ('backup-' + 'a' * 32 + '.app')
        shutil.copytree(self.app, backup)
        (self.app / 'Contents/Resources/app.asar').write_bytes(b'patched archive')
        patched = self.core.manifest(self.app)
        state = {'schema': 2, 'product': 'claude-russian', 'version': '2.9939.4', 'phase': 'installed',
                 'app': str(self.app), 'source_asar_sha256': original['Contents/Resources/app.asar']['sha256'],
                 'backup': str(backup), 'original_uid': os.getuid(), 'original_gid': os.getgid(),
                 'transaction_dir': str(self.base / ('.claude-russian-transaction-' + 'a' * 32)),
                 'preserved_original': str(self.base / ('.claude-russian-transaction-' + 'a' * 32) / 'original.app')}
        for kind, manifest in [('original', original), ('patched', patched)]:
            path = directory / (kind + '-' + 'a' * 32 + '.json')
            state[kind + '_manifest'] = str(path)
            state[kind + '_manifest_sha256'] = self.core.save_manifest(path, manifest, os.getuid(), os.getgid())
        self.core.save_state(directory, state, os.getuid(), os.getgid())
        return directory

    def test_mac_uses_entire_bundle_manifest_and_same_source_fingerprint(self):
        fingerprint = self.adapter.probe(self.app, [])['fingerprint']
        directory = self.installed()
        result = self.adapter.probe(self.app, [directory])
        self.assertEqual(result['kind'], 'patched')
        self.assertEqual(result['fingerprint'], fingerprint)
        (self.app / 'Contents/MacOS/Claude').write_bytes(b'changed outside ASAR')
        self.assertEqual(self.adapter.probe(self.app, [directory])['kind'], 'recovery')

    def test_mac_source_requires_info_version_and_archive_pin(self):
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '9.99.99'}))
        self.assertEqual(self.adapter.probe(self.app, [])['kind'], 'unknown')

    def test_mac_local_signing_requires_explicit_install_consent(self):
        self.assertNotIn('--approve-local-signature', self.adapter.command('install', self.app, self.base / 'state'))
        self.assertIn('--approve-local-signature', self.adapter.command('install', self.app, self.base / 'state', True))
        self.assertNotIn('--approve-local-signature', self.adapter.command('restore', self.app, self.base / 'state', True))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        import shutil
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.package = self.base / 'package'
        for folder in ('macos', 'portable', 'linux', 'windows', 'updater'):
            (self.package / folder).mkdir(parents=True, exist_ok=True)
        for relative in ('install.sh', 'linux/install.sh', 'windows/install.ps1'):
            shutil.copyfile(ROOT / relative, self.package / relative)
        (self.package / 'manifest.json').write_text('{"repository":"claude-russian","format":1}')
        driver = "import json,os,sys\nwith open(os.environ['UPDATER_TEST_LOG'],'a') as f: f.write(json.dumps(sys.argv)+'\\n')\nraise SystemExit(int(os.environ.get('UPDATER_TEST_RC','0')) if sys.argv[0].endswith('manager.py') else 0)\n"
        for relative in ('updater/manager.py', 'macos/patch.py', 'portable/patch.py'):
            (self.package / relative).write_text(driver)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.log = self.base / 'calls.jsonl'

    def run_launcher(self, system, action, extra=(), manager_rc=0):
        uname = self.bin / 'uname'
        uname.write_text('#!/bin/sh\nprintf "%s\\n" ' + ('Darwin' if system == 'macos' else 'Linux'))
        uname.chmod(0o700)
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                   UPDATER_TEST_LOG=str(self.log), UPDATER_TEST_RC=str(manager_rc))
        path = self.package / ('install.sh' if system == 'macos' else 'linux/install.sh')
        return subprocess.run(['/bin/bash', str(path), action, '--app', str(self.base / 'Claude.app'), *extra],
                              env=env, input='', text=True, capture_output=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_bash_auto_status_passes_flags_to_manager(self):
        for system in ('macos', 'linux'):
            with self.subTest(system=system):
                result = self.run_launcher(system, 'auto-status', ('--control-dir', str(self.base / 'control')))
                self.assertEqual(result.returncode, 0, result.stderr)
                call = self.calls()[-1]
                self.assertTrue(call[0].endswith('manager.py'))
                self.assertEqual(call[1], 'status')
                self.assertIn('--control-dir', call)

    def test_explicit_bash_auto_check_allows_manual_rights_prompt(self):
        for system in ('macos', 'linux'):
            with self.subTest(system=system):
                result = self.run_launcher(system, 'auto-check')
                self.assertEqual(result.returncode, 0, result.stderr)
                call = self.calls()[-1]
                self.assertEqual(call[1], 'check')
                self.assertIn('--manual', call)

    def test_mac_auto_enable_requires_signing_consent(self):
        result = self.run_launcher('macos', 'auto-enable')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        result = self.run_launcher('macos', 'auto-enable', ('--approve-signature',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1][1], 'enable')
        self.assertIn('--approve-signature', self.calls()[-1])

    def test_bash_restore_disables_auto_before_fallback(self):
        for system in ('macos', 'linux'):
            with self.subTest(system=system):
                if self.log.exists():
                    self.log.unlink()
                result = self.run_launcher(system, 'restore', manager_rc=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls()
                self.assertEqual(len(calls), 2)
                self.assertTrue(calls[0][0].endswith('manager.py'))
                self.assertEqual(calls[0][1], 'restore')
                self.assertIn('--manual', calls[0])
                self.assertTrue(calls[1][0].endswith('patch.py'))
                self.assertEqual(calls[1][1], 'restore')

    def test_bash_restore_does_not_fallback_on_corrupt_auto_state(self):
        result = self.run_launcher('linux', 'restore', manager_rc=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.calls()), 1)


if __name__ == '__main__':
    unittest.main()
