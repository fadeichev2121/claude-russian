"""Claude layout/state adapter. Probes never create state or change the app."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import re
import struct
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = 'macos' if sys.platform == 'darwin' else 'windows' if sys.platform == 'win32' else 'linux'
_CORES = {}


def _core():
    name = 'macos' if SYSTEM == 'macos' else 'portable'
    if name not in _CORES:
        # macOS imports pwd/fcntl; never import that core on Windows.
        sys.path.insert(0, str(ROOT / 'macos'))
        spec = importlib.util.spec_from_file_location('claude_updater_' + name, ROOT / name / 'patch.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CORES[name] = module
    return _CORES[name]


def default_state():
    core = _core()
    if SYSTEM == 'macos':
        return core.user_identity()[2] / 'Library/Application Support/claude-russian/state'
    return core.identity()[2]


def _safe(path):
    core = _core()
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    if SYSTEM == 'macos':
        core.no_symlink_components(path)
        return path
    return core.safe_path(path)


def discover(app_hint=None):
    core = _core()
    if SYSTEM == 'macos':
        return _safe(app_hint or '/Applications/Claude.app')
    if SYSTEM != 'windows' or not app_hint:
        return core.discover(SimpleNamespace(app=str(app_hint) if app_hint else None), None)
    hinted = _safe(app_hint)
    if not re.fullmatch(r'app-\d+(\.\d+)+', hinted.name):
        return hinted
    # Squirrel replaces its app-* folder; an explicit old hint tracks siblings.
    candidates = []
    for item in hinted.parent.glob('app-*'):
        if re.fullmatch(r'app-\d+(\.\d+)+', item.name) and (item / 'resources/app.asar').is_file():
            candidates.append(_safe(item))
    return max(candidates, key=lambda p: tuple(map(int, p.name[4:].split('.')))) if candidates else hinted


def _fingerprint(app, version, source_files):
    raw = json.dumps({'app': os.path.normcase(str(app)), 'version': version, 'source': source_files},
                     sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _version(app):
    core = _core()
    if SYSTEM == 'macos':
        info_path = app / 'Contents/Info.plist'
        core.no_symlink_components(info_path)
        value = plistlib.loads(info_path.read_bytes()).get('CFBundleShortVersionString')
    else:
        value = json.loads(core.Asar((app / 'resources/app.asar').read_bytes()).read('package.json')).get('version')
    return value if isinstance(value, str) else ''


def _source(app):
    core = _core()
    if SYSTEM == 'macos':
        if app.suffix != '.app':
            raise RuntimeError('Путь Claude должен оканчиваться на .app.')
        version = _version(app)
        archive = app / 'Contents/Resources/app.asar'
        core.no_symlink_components(archive)
        core.source_profile(version, core.file_hash(archive))
        original = core.manifest(app)
        # The full bundle is part of the fingerprint: updater metadata must settle.
        if original['Contents/Resources/app.asar']['sha256'] != core.file_hash(archive):
            raise RuntimeError('Claude обновился во время проверки.')
        return version, original
    _, profile = core.match_profile(app)
    return profile['version'], {name: core.file_hash(app / name) for name in core.FILES}


def _writable(app):
    if SYSTEM == 'macos':
        return os.access(app.parent, os.W_OK | os.X_OK)
    return all(os.access(app / name, os.W_OK) and os.access((app / name).parent, os.W_OK | os.X_OK)
               for name in _core().FILES)


def _state(directory, uid):
    core = _core()
    directory = _safe(directory)
    if SYSTEM != 'macos':
        return core.load_state(directory, uid)
    core.require_private_directory(directory, uid)
    path = directory / 'state.json'
    core.no_symlink_components(path)
    if not path.exists():
        return None
    if path.stat().st_size > 128 * 1024:
        raise RuntimeError('Файл состояния слишком большой.')
    candidate = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(candidate, dict):
        raise RuntimeError('Неизвестный формат состояния Claude.')
    source = core.recorded_path(candidate.get('app'), 'app')
    return core.read_state(directory, uid, source)


def _validated_patch(app, directory, state, uid):
    core = _core()
    if SYSTEM == 'macos':
        original = core.load_manifest(state, 'original', uid)
        patched = core.load_manifest(state, 'patched', uid)
        core.require_manifest(Path(state['backup']), original, 'Резервная копия Claude')
        core.require_manifest(app, patched, 'Установленный Claude')
        return state['version'], original
    for name in core.FILES:
        for kind in ('original', 'patched'):
            core.backup_bytes(directory, state, name, kind, uid)
    core.require_hashes(app, state, ['patched'])
    profile = core.profiles()[state['profile']]
    return profile['version'], {name: record['original'] for name, record in state['files'].items()}


def probe(app, state_dirs):
    """Recognize supported original or validated patch; fail closed otherwise."""
    result = {'app': str(app), 'fingerprint': '', 'version': '', 'kind': 'unknown',
              'writable': False, 'state_dir': None, 'message': ''}
    try:
        app = _safe(app)
        core = _core()
        result.update(app=str(app), writable=_writable(app))
        layout_error = None
        if SYSTEM != 'macos':
            try:
                core.require_layout(app)
            except (RuntimeError, OSError, ValueError) as error:
                # Validate transaction state even if interrupted replacement
                # left a missing file; it must report recovery, not new source.
                layout_error = error
        try:
            # Squirrel can unpack a new app-* while the old app-* still runs.
            process_root = app.parent if SYSTEM == 'windows' and re.fullmatch(r'app-\d+(\.\d+)+', app.name) else app
            # ShipIt wait diagnostics must not corrupt the manager's JSON bridge.
            with contextlib.redirect_stdout(io.StringIO()):
                core.require_closed(process_root)
        except (RuntimeError, OSError, ValueError) as error:
            result.update(kind='busy', message=str(error))
            return result
        try:
            result['version'] = _version(app)
        except (RuntimeError, OSError, ValueError, KeyError, struct.error):
            pass
        source = None
        source_error = None
        try:
            if layout_error:
                raise layout_error
            source = _source(app)
        except (RuntimeError, OSError, ValueError, KeyError, struct.error) as error:
            source_error = error
        uid = core.user_identity()[0] if SYSTEM == 'macos' else core.identity()[0]
        mismatched_state = None
        for candidate in state_dirs:
            directory = Path(candidate)
            # Missing historical states do not create directories during probing.
            if not (directory / 'state.json').exists() and not (directory / 'state.json').is_symlink():
                continue
            try:
                state = _state(directory, uid)
            except (RuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                result.update(kind='recovery', state_dir=str(directory), message=str(error))
                return result
            if not state or os.path.normcase(state['app']) != os.path.normcase(str(app)):
                continue
            if state['phase'] in {'prepared', 'restoring'}:
                result.update(kind='recovery', state_dir=str(directory),
                              message='Предыдущая операция не завершена; нужен ручной откат.')
                return result
            if state['phase'] != 'installed':
                continue
            try:
                version, originals = _validated_patch(app, directory, state, uid)
            except (RuntimeError, OSError, ValueError, KeyError) as error:
                # Validate historical backup data even if a vendor update replaced the patch.
                try:
                    if SYSTEM == 'macos':
                        original = core.load_manifest(state, 'original', uid)
                        core.load_manifest(state, 'patched', uid)
                        core.require_manifest(Path(state['backup']), original, 'Резервная копия Claude')
                    else:
                        for name in core.FILES:
                            for kind in ('original', 'patched'):
                                core.backup_bytes(directory, state, name, kind, uid)
                except (RuntimeError, OSError, ValueError, KeyError) as backup_error:
                    result.update(kind='recovery', state_dir=str(directory), message=str(backup_error))
                    return result
                recorded_version = state.get('version') if SYSTEM == 'macos' else core.profiles()[state['profile']]['version']
                if not source and (not result['version'] or result['version'] == recorded_version):
                    mismatched_state = (directory, error)
                continue
            result.update(kind='patched', version=version, state_dir=str(directory),
                          fingerprint=_fingerprint(app, version, originals), message='Перевод установлен и проверен.')
            return result
        if source:
            version, originals = source
            result.update(kind='source', version=version, fingerprint=_fingerprint(app, version, originals),
                          message='Поддерживаемая исходная сборка Claude.')
        elif mismatched_state:
            result.update(kind='recovery', reason='app-changed', state_dir=str(mismatched_state[0]),
                          message=str(mismatched_state[1]))
        else:
            result['message'] = str(source_error or 'Сборка пока не поддерживается.')
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        result['message'] = str(error)
    return result


def command(action, app, state_dir, approved=False):
    if action not in {'install', 'restore', 'status'}:
        raise ValueError('Неизвестное действие ядра: ' + action)
    folder = 'macos' if SYSTEM == 'macos' else 'portable'
    arguments = [sys.executable, str(ROOT / folder / 'patch.py'), action,
                 '--app', str(app), '--state-dir', str(state_dir)]
    if action == 'install' and approved:
        if SYSTEM == 'macos':
            arguments.append('--approve-local-signature')
        elif SYSTEM == 'windows':
            arguments.append('--approve-exe-signature')
    return arguments
