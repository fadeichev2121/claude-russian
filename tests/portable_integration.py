#!/usr/bin/env python3
"""Verify installers on native Windows/Linux using isolated official package files.

Claude and its vendor installer are never executed. Reports contain no vendor bytes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else None
FILES = ['resources/app.asar', 'resources/en-US.json'] + (['claude.exe'] if SYSTEM == 'windows' else [])
sys.path[:0] = [str(ROOT / 'macos'), str(ROOT / 'portable')]
from asar import Asar
from pe_integrity import inspect_integrity

MARKER = b'Claude RU interface translator v3'
PROBE = 'Claude CI translation probe'
PROBE_RU = 'Проверка обновления перевода'


def digest(path, algorithm='sha256'):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def run(command, expected=0, input_text=None):
    result = subprocess.run([str(item) for item in command], input=input_text,
                            stdin=subprocess.DEVNULL if input_text is None else None,
                            capture_output=True, encoding='utf-8', errors='replace', timeout=300)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    if (expected == 0 and result.returncode != 0) or (expected != 0 and result.returncode == 0):
        raise AssertionError('Unexpected return code ' + str(result.returncode) + ': ' + str(command[0]))
    return result


def approved_url(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and parsed.netloc == 'downloads.claude.ai'
            and parsed.path.startswith(('/releases/win32/', '/claude-desktop/apt/stable/'))
            and not parsed.query and not parsed.fragment)


def download(fixture, destination):
    if (not approved_url(fixture['url']) or fixture['hash_algorithm'] not in {'sha1', 'sha256'}
            or type(fixture['size']) is not int or not 0 < fixture['size'] < 1024 ** 3):
        raise RuntimeError('Unsupported official fixture identity')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(fixture['url'], timeout=90) as source, destination.open('wb') as output:
                if not approved_url(source.geturl()):
                    raise RuntimeError('Unexpected fixture redirect')
                received = 0
                while block := source.read(1024 * 1024):
                    received += len(block)
                    if received > fixture['size']:
                        raise RuntimeError('Official package exceeds its recorded size')
                    output.write(block)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(3)
    if destination.stat().st_size != fixture['size']:
        raise AssertionError('Official package size mismatch')
    if digest(destination, fixture['hash_algorithm']) != fixture['digest']:
        raise AssertionError('Official package digest mismatch')


def write_member(incoming, target, mode):
    target.parent.mkdir(exist_ok=True)
    with target.open('xb') as output:
        shutil.copyfileobj(incoming, output, length=1024 * 1024)
    target.chmod(mode)


def extract(profile, fixture, folder):
    app = folder / 'Claude с пробелами'
    app.mkdir()
    package = folder / ('package.nupkg' if SYSTEM == 'windows' else 'package.deb')
    print('Download official fixture: ' + profile['version'] + ' / ' + profile['arch'], flush=True)
    download(fixture, package)
    if SYSTEM == 'windows':
        with zipfile.ZipFile(package) as source:
            for name in FILES:
                members = [item for item in source.infolist() if item.filename == 'lib/net45/' + name]
                if len(members) != 1 or members[0].is_dir():
                    raise AssertionError('Fixture member is not a unique regular file: ' + name)
                member = members[0]
                kind = stat.S_IFMT(member.external_attr >> 16)
                if kind not in {0, stat.S_IFREG}:
                    raise AssertionError('Fixture member is a link or special file: ' + name)
                with source.open(member) as incoming:
                    write_member(incoming, app / name, 0o644)
    else:
        command = ['dpkg-deb', '--fsys-tarfile', str(package)]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
            extracted = set()
            try:
                with tarfile.open(fileobj=process.stdout, mode='r|') as source:
                    for member in source:
                        relative = member.name.removeprefix('./')
                        if not relative.startswith('usr/lib/claude-desktop/'):
                            continue
                        name = relative[len('usr/lib/claude-desktop/'):]
                        if name not in set(FILES) | {'claude-desktop'}:
                            continue
                        if not member.isfile() or name in extracted:
                            raise AssertionError('Fixture member is not a unique regular file: ' + name)
                        with source.extractfile(member) as incoming:
                            write_member(incoming, app / name, 0o755 if name == 'claude-desktop' else 0o644)
                        extracted.add(name)
                # Drain tar padding before waiting, avoiding a producer blocked on its pipe.
                while process.stdout.read(1024 * 1024):
                    pass
                process.stdout.close()
                if process.wait(timeout=300) != 0:
                    errors.seek(0)
                    raise RuntimeError('DEB extraction failed: ' + errors.read().decode('utf-8', 'replace'))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
            if extracted != set(FILES) | {'claude-desktop'}:
                raise AssertionError('Official DEB does not contain the expected files')
    package.unlink()
    expected = {'resources/app.asar': profile['asar_sha256'], 'resources/en-US.json': profile['native_sha256']}
    if SYSTEM == 'windows':
        expected['claude.exe'] = profile['exe_sha256']
    for name in FILES:
        if digest(app / name) != expected[name]:
            raise AssertionError('Official extracted file SHA-256 mismatch: ' + name)
    return app


def launcher(app, state, action, shell=None, standalone=None):
    if SYSTEM == 'linux':
        return ['bash', standalone or ROOT / 'linux/install.sh', action, '--app', app, '--state-dir', state]
    return [shell or 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            standalone or ROOT / 'windows/install.ps1', action, '-AppPath', app, '-StateDirectory', state]


def install(app, state):
    if SYSTEM == 'linux':
        run(launcher(app, state, 'install'))
    else:
        # The interactive launcher refuses redirected consent; its core has an explicit opt-in.
        run([sys.executable, ROOT / 'portable/patch.py', 'install', '--app', app,
             '--state-dir', state, '--approve-exe-signature'])


def state_bytes(state):
    path = state / 'state.json'
    return path.read_bytes() if path.exists() else None


def read_state(state):
    return json.loads((state / 'state.json').read_text(encoding='utf-8'))


def file_bytes(app):
    return {name: (app / name).read_bytes() for name in FILES}


def permissions(app):
    return {name: (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            for name in FILES for info in [(app / name).stat()]}


def assert_original(app, originals, metadata):
    assert file_bytes(app) == originals, 'Restore must reproduce every original byte'
    if SYSTEM == 'linux':
        assert permissions(app) == metadata, 'Restore must preserve original owner/group/mode'


def header_hash(data):
    size = struct.unpack_from('<I', data, 12)[0]
    return hashlib.sha256(data[16:16 + size]).hexdigest()


def assert_translation(app, originals, profile, saved, probe=False):
    patched_asar = (app / 'resources/app.asar').read_bytes()
    assert patched_asar != originals['resources/app.asar']
    archive = Asar(patched_asar)
    for name in profile['preloads']:
        preload = archive.read(name)
        assert preload.count(MARKER) == 1, 'Translator must be injected exactly once per preload'
        assert 'Настройки'.encode('utf-8') in preload
        if probe:
            assert PROBE.encode('utf-8') in preload and PROBE_RU.encode('utf-8') in preload
    native_before = json.loads(originals['resources/en-US.json'])
    native_after = json.loads((app / 'resources/en-US.json').read_bytes())
    native_ru = json.loads((ROOT / 'macos/native-ru.json').read_text(encoding='utf-8'))
    keys = set(native_before) & set(native_ru)
    assert saved['native_entries'] == len(keys) and keys
    assert set(native_after) == set(native_before), 'Native catalogue keys must stay intact'
    assert all(native_after[key] == native_ru[key] for key in keys)
    assert any(native_after[key] != native_before[key] and any('А' <= char <= 'я' for char in native_after[key])
               for key in keys), 'A visible native string must have a Russian translation'
    if SYSTEM == 'windows':
        original_exe = originals['claude.exe']
        patched_exe = (app / 'claude.exe').read_bytes()
        before = inspect_integrity(original_exe, header_hash(originals['resources/app.asar']))
        after = inspect_integrity(patched_exe, header_hash(patched_asar))
        assert len(original_exe) == len(patched_exe) and original_exe != patched_exe
        assert before['machine'] == after['machine'] == (0x8664 if profile['arch'] == 'x64' else 0xAA64)
        assert before['resource_count'] == after['resource_count'] > 0
        assert (before['certificate_offset'], before['certificate_size']) == (after['certificate_offset'], after['certificate_size'])
        certificate = slice(before['certificate_offset'], before['certificate_offset'] + before['certificate_size'])
        assert original_exe[certificate] == patched_exe[certificate]
        # Every changed byte must belong to a recorded fixed-size hash or the PE checksum.
        ranges = [(before['checksum_offset'], before['checksum_offset'] + 4)]
        ranges += [(item['hash_offset'], item['hash_offset'] + 64) for item in before['resources']]
        position = 0
        for start, end in sorted(ranges):
            assert original_exe[position:start] == patched_exe[position:start]
            position = end
        assert original_exe[position:] == patched_exe[position:]


def exercise(profile_key, profile, fixture, folder, report):
    app = extract(profile, fixture, folder)
    report['checks'].append('verified-official-package-and-all-extracted-file-identities')
    state = folder / 'state с пробелами'
    originals, metadata = file_bytes(app), permissions(app)
    binary = app / 'claude-desktop' if SYSTEM == 'linux' else None
    binary_hash = digest(binary) if binary else None
    run(launcher(app, state, 'status'))
    if SYSTEM == 'windows':
        snapshot = file_bytes(app), state_bytes(state)
        run(launcher(app, state, 'install'), expected=1)
        assert (file_bytes(app), state_bytes(state)) == snapshot
        report['checks'].append('redirected-install-refuses-without-signature-consent')
    install(app, state)
    saved = read_state(state)
    assert saved['phase'] == 'installed' and saved['profile'] == profile_key
    assert_translation(app, originals, profile, saved)
    if SYSTEM == 'linux':
        assert permissions(app) == metadata and digest(binary) == binary_hash
    report['checks'].append('install-original-with-translated-preloads-native-catalogue-and-valid-asar-integrity')
    run(launcher(app, state, 'status'))
    installed = file_bytes(app), state_bytes(state)
    install(app, state)
    assert (file_bytes(app), state_bytes(state)) == installed
    report['checks'].append('idempotent-install-preserves-all-files-and-backup')

    # A dictionary refresh uses the documented restore/reinstall sequence.
    run(launcher(app, state, 'restore'))
    assert_original(app, originals, metadata)
    dictionary_path = ROOT / 'macos/ru.json'
    dictionary_bytes = dictionary_path.read_bytes()
    try:
        dictionary = json.loads(dictionary_bytes)
        assert PROBE not in dictionary
        dictionary[PROBE] = PROBE_RU
        dictionary_path.write_text(json.dumps(dictionary, ensure_ascii=False), encoding='utf-8')
        install(app, state)
        upgraded = read_state(state)
        assert upgraded['phase'] == 'installed' and upgraded['backup'] != saved['backup']
        assert_translation(app, originals, profile, upgraded, probe=True)
        for backup in (saved['backup'], upgraded['backup']):
            assert {name: (state / backup / 'original' / name).read_bytes() for name in FILES} == originals
        saved = upgraded
    finally:
        dictionary_path.write_bytes(dictionary_bytes)
    report['checks'].append('translation-refresh-via-restore-reinstall-preserves-original-backups')

    patched = file_bytes(app)
    asar = app / 'resources/app.asar'
    asar.write_bytes(patched['resources/app.asar'] + b'CI tamper probe')
    for action in ('install', 'restore'):
        snapshot = file_bytes(app), state_bytes(state)
        if action == 'install':
            command = [sys.executable, ROOT / 'portable/patch.py', action, '--app', app,
                       '--state-dir', state] + (['--approve-exe-signature'] if SYSTEM == 'windows' else [])
        else:
            command = launcher(app, state, action)
        run(command, expected=1)
        assert (file_bytes(app), state_bytes(state)) == snapshot
    asar.write_bytes(patched['resources/app.asar'])
    report['checks'].append('tamper-refusal-preserves-every-app-file-and-state')

    backup = state / saved['backup'] / 'original/resources/app.asar'
    backup.write_bytes(originals['resources/app.asar'] + b'CI corrupt backup probe')
    snapshot = file_bytes(app), state_bytes(state)
    run(launcher(app, state, 'restore'), expected=1)
    assert (file_bytes(app), state_bytes(state)) == snapshot
    backup.write_bytes(originals['resources/app.asar'])
    report['checks'].append('corrupt-backup-refusal-preserves-every-app-file-and-state')
    run(launcher(app, state, 'restore'))
    assert_original(app, originals, metadata)
    assert read_state(state)['phase'] == 'restored'
    snapshot = file_bytes(app), state_bytes(state)
    run(launcher(app, state, 'restore'))
    assert (file_bytes(app), state_bytes(state)) == snapshot
    report['checks'].append('byte-identical-restore-and-idempotent-restore')

    standalone_dir = folder / 'standalone'
    standalone_dir.mkdir()
    source = ROOT / ('windows/install.ps1' if SYSTEM == 'windows' else 'linux/install.sh')
    standalone = standalone_dir / source.name
    shutil.copyfile(source, standalone)
    run(launcher(app, state, 'status', standalone=standalone))
    install(app, state)
    run(launcher(app, state, 'restore', standalone=standalone))
    assert_original(app, originals, metadata)
    report['checks'].append('standalone-download-bootstrap-status-and-restore')
    if SYSTEM == 'windows' and shutil.which('pwsh'):
        run(launcher(app, state, 'status', shell='pwsh', standalone=standalone))
        report['checks'].append('powershell-7-standalone-bootstrap')
    if SYSTEM == 'linux':
        paths = [app, app / 'resources'] + [app / name for name in FILES] + [binary]
        try:
            run(['sudo', '-n', '--', 'chown', '0:0', *paths])
            run(['sudo', '-n', '--', 'chmod', '0755', app, app / 'resources'])
            root_metadata = permissions(app)
            install(app, state)
            assert_translation(app, originals, profile, read_state(state))
            assert permissions(app) == root_metadata and digest(binary) == binary_hash
            run(launcher(app, state, 'restore'))
            assert_original(app, originals, root_metadata)
            assert digest(binary) == binary_hash
            assert (state / 'state.json').stat().st_uid == os.getuid()
            assert stat.S_IMODE((state / 'state.json').stat().st_mode) == 0o600
            report['checks'].append('root-owned-deb-files-sudo-install-restore-preserves-permissions-and-user-state')
        finally:
            # Reclaim only this test's selected paths so TemporaryDirectory can clean up.
            run(['sudo', '-n', '--', 'chown', str(os.getuid()) + ':' + str(os.getgid()), *paths])
        assert digest(binary) == binary_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    profiles = {}
    for name in ('profiles.json', 'windows-profiles.json', 'linux-profiles.json'):
        for key, value in json.loads((ROOT / 'portable' / name).read_text(encoding='utf-8')).items():
            if key in profiles and profiles[key] != value:
                raise RuntimeError('Conflicting portable profiles')
            profiles[key] = value
    fixture = json.loads((ROOT / 'tests/fixtures.json').read_text(encoding='utf-8'))[args.profile]
    profile = profiles[args.profile]
    if profile['platform'] != SYSTEM:
        raise RuntimeError('Run this check on its actual native operating system')
    native_arch = os.environ.get('RUNNER_ARCH', '').lower()
    expected_arch = 'x64' if profile['arch'] == 'amd64' else profile['arch']
    if native_arch and native_arch != expected_arch:
        raise RuntimeError('Runner architecture does not match the selected profile')
    report = {'profile': args.profile, 'runner_os': SYSTEM, 'runner_arch': native_arch,
              'python_version': sys.version.split()[0], 'checks': [], 'app_executed': False,
              'claude_login_tested': False, 'result': 'failure'}
    output = ROOT / 'test-results'
    output.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='claude-ci-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
            exercise(args.profile, profile, fixture, Path(temporary), report)
        report['result'] = 'success'
        print('PASS: ' + args.profile + ' (' + str(len(report['checks'])) + ' checks)', flush=True)
    finally:
        (output / (args.profile + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
