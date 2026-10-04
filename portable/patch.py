#!/usr/bin/env python3
"""Patch original Windows EXE / Linux DEB installs; keep every replaced byte."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'macos'))
from asar import Asar
from catalog import compile_catalog

SYSTEM = 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else None
FILES = ['resources/app.asar', 'resources/en-US.json'] + (['claude.exe'] if SYSTEM == 'windows' else [])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_path(path):
    """Reject symlinks and Windows junctions on every component."""
    path = Path(os.path.abspath(path))
    if SYSTEM == 'windows' and str(path).startswith('\\\\'):
        raise RuntimeError('Сетевые пути не поддерживаются.')
    for component in [path] + list(path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('Путь содержит ссылку или junction: ' + str(component))
    return path


def identity():
    if SYSTEM == 'linux':
        import pwd
        uid = int(os.environ.get('SUDO_UID', os.getuid())) if os.geteuid() == 0 else os.getuid()
        account = pwd.getpwuid(uid)
        if uid == 0:
            raise RuntimeError('Запускай меню из обычного аккаунта; sudo требуется только выбранному действию.')
        return uid, account.pw_gid, Path(account.pw_dir) / '.local/state/claude-russian'
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise RuntimeError('LOCALAPPDATA не найден. Запускай из своего обычного аккаунта Windows.')
    return None, None, Path(local) / 'claude-russian/state'


def private_directory(path, uid, gid):
    safe_path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        if SYSTEM == 'linux' and os.geteuid() == 0:
            os.chown(item, uid, gid)
    if not path.is_dir():
        raise RuntimeError('Папка состояния отсутствует: ' + str(path))
    if SYSTEM == 'linux':
        if path.stat().st_uid != uid:
            raise RuntimeError('Папка состояния принадлежит другому пользователю.')
        path.chmod(0o700)
    else:
        # Restrict this project's backup/state directory; never change app ACLs.
        command = """
$ErrorActionPreference='Stop'
$p=$env:CLAUDE_RU_PRIVATE_DIRECTORY
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$old=[System.IO.Directory]::GetAccessControl($p)
if ($env:CLAUDE_RU_NEW_DIRECTORY -ne '1' -and $old.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'State directory belongs to another user.' }
$acl=New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
$acl.SetOwner($sid)
$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,[System.Security.AccessControl.FileSystemRights]::FullControl,([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),[System.Security.AccessControl.PropagationFlags]::None,[System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl($p,$acl)
"""
        environment = dict(os.environ, CLAUDE_RU_PRIVATE_DIRECTORY=str(path),
                           CLAUDE_RU_NEW_DIRECTORY='1' if path in missing else '0')
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                       env=environment, check=True, stdout=subprocess.DEVNULL)


def owned_file(path, uid):
    safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError('Ожидался обычный файл с одной ссылкой: ' + str(path))
    if SYSTEM == 'linux' and (info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('Файл состояния должен принадлежать пользователю и иметь права 0600.')
    return info


def write_private(path, data, uid, gid):
    safe_path(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        if SYSTEM == 'linux' and os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, gid)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save_state(directory, state, uid, gid):
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    pending = directory / ('state-' + uuid.uuid4().hex + '.tmp')
    write_private(pending, raw, uid, gid)
    os.replace(pending, directory / 'state.json')


def profiles():
    result = {}
    for filename in ('profiles.json', 'windows-profiles.json', 'linux-profiles.json'):
        values = json.loads((ROOT / 'portable' / filename).read_text(encoding='utf-8'))
        if not isinstance(values, dict):
            raise RuntimeError('Некорректный список совместимых сборок: ' + filename)
        for key, profile in values.items():
            if not isinstance(profile, dict):
                raise RuntimeError('Некорректный профиль сборки: ' + str(key))
            system, arch, version = profile.get('platform'), profile.get('arch'), profile.get('version')
            allowed_arches = {'windows': {'x64', 'arm64'}, 'linux': {'amd64', 'arm64'}}
            if (system not in allowed_arches or arch not in allowed_arches[system]
                    or not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
                    or key != system + '-' + arch + '-' + version
                    or profile.get('preloads') != ['.vite/build/mainView.js', '.vite/build/mainWindow.js']):
                raise RuntimeError('Некорректная версия или структура профиля: ' + str(key))
            fields = ['asar_sha256', 'native_sha256'] + (['exe_sha256'] if system == 'windows' else [])
            if any(not isinstance(profile.get(field), str) or not re.fullmatch(r'[a-f0-9]{64}', profile[field]) for field in fields):
                raise RuntimeError('Некорректные контрольные суммы профиля: ' + key)
            if key in result and result[key] != profile:
                raise RuntimeError('Профили одной сборки различаются: ' + key)
            result[key] = profile
    return result


def load_state(directory, uid, app=None):
    path = directory / 'state.json'
    safe_path(path)
    if not path.exists():
        return None
    info = owned_file(path, uid)
    if info.st_size > 128 * 1024:
        raise RuntimeError('Файл состояния слишком большой.')
    state = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(state, dict) or state.get('schema') != 1 or state.get('product') != 'claude-russian-portable'
            or state.get('platform') != SYSTEM or state.get('phase') not in {'prepared', 'installed', 'restoring', 'restored'}):
        raise RuntimeError('Неизвестный формат состояния.')
    selected = profiles().get(state.get('profile'))
    if not selected or selected['platform'] != SYSTEM:
        raise RuntimeError('Неизвестная сборка в состоянии.')
    original_app = state.get('app')
    if not isinstance(original_app, str) or str(safe_path(original_app)) != original_app:
        raise RuntimeError('Некорректный путь приложения в состоянии.')
    if app is not None and os.path.normcase(str(app)) != os.path.normcase(original_app):
        raise RuntimeError('Путь --app не совпадает с сохранённой установкой.')
    backup_name = state.get('backup')
    if not isinstance(backup_name, str) or not re.fullmatch(r'backup-[0-9a-f]{32}', backup_name):
        raise RuntimeError('Некорректный путь резервной копии.')
    safe_path(directory / backup_name)
    if set(state.get('files', {})) != set(FILES):
        raise RuntimeError('Некорректный список изменённых файлов.')
    for name, record in state['files'].items():
        for key in ('original', 'patched'):
            if not re.fullmatch(r'[a-f0-9]{64}', str(record.get(key, ''))):
                raise RuntimeError('Некорректная контрольная сумма.')
        for key in ('mode', 'uid', 'gid'):
            if type(record.get(key)) is not int or record[key] < 0:
                raise RuntimeError('Некорректные права файла в состоянии.')
        if record['mode'] > 0o777:
            raise RuntimeError('Особые права файлов не поддерживаются.')
    if state['files']['resources/app.asar']['original'] != selected['asar_sha256']:
        raise RuntimeError('Резервная копия относится к другой сборке.')
    if state['files']['resources/en-US.json']['original'] != selected['native_sha256']:
        raise RuntimeError('Нативный каталог относится к другой сборке.')
    if SYSTEM == 'windows' and state['files']['claude.exe']['original'] != selected['exe_sha256']:
        raise RuntimeError('EXE относится к другой сборке.')
    return state


@contextmanager
def lock(directory, uid, gid):
    private_directory(directory, uid, gid)
    path = directory / 'state.lock'
    safe_path(path)
    if not path.exists():
        try:
            write_private(path, b'0', uid, gid)
        except FileExistsError:
            pass
    owned_file(path, uid)
    with path.open('r+b') as stream:
        try:
            if SYSTEM == 'windows':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Другой установщик уже работает с этой папкой состояния.')
        try:
            yield
        finally:
            if SYSTEM == 'windows':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def discover(args, state):
    if args.app:
        return safe_path(os.path.expanduser(args.app))
    if state:
        saved = safe_path(state['app'])
        if SYSTEM == 'windows' and re.fullmatch(r'app-\d+(\.\d+)+', saved.name):
            old_version = tuple(int(x) for x in saved.name[4:].split('.'))
            for candidate in saved.parent.glob('app-*'):
                if re.fullmatch(r'app-\d+(\.\d+)+', candidate.name):
                    current_version = tuple(int(x) for x in candidate.name[4:].split('.'))
                    if current_version > old_version and (candidate / 'resources/app.asar').is_file():
                        raise RuntimeError('Claude обновился; запись патча относится к старой папке ' + str(saved) + '. Для новой сборки укажи --app и отдельный --state-dir. Для отката старой укажи --app ' + str(saved) + '.')
        return saved
    if SYSTEM == 'linux':
        return safe_path('/usr/lib/claude-desktop')
    local = Path(os.environ['LOCALAPPDATA'])
    found = []
    for parent in (local / 'AnthropicClaude', local / 'Claude'):
        if parent.is_dir():
            safe_path(parent)
            found.extend(safe_path(item) for item in parent.glob('app-*') if (item / 'resources/app.asar').is_file())
    if not found:
        raise RuntimeError('Обычная EXE-установка Claude не найдена. MSIX/Microsoft Store не поддерживаются. Укажи --app с папкой, где находятся claude.exe и resources.')
    # Squirrel keeps old app-* folders: choose the newest numeric version.
    def version(path):
        suffix = path.name[4:]
        return tuple(int(x) for x in suffix.split('.')) if re.fullmatch(r'\d+(\.\d+)+', suffix) else ()
    found.sort(key=version, reverse=True)
    if len(found) > 1 and version(found[0]) == version(found[1]):
        raise RuntimeError('Найдено несколько установок. Укажи --app явно.')
    return found[0]


def separate_state(directory, app):
    for other in (app, ROOT):
        left, right = os.path.normcase(str(directory)), os.path.normcase(str(other))
        try:
            common = os.path.commonpath([left, right])
        except ValueError:
            continue
        if common == left or common == right:
            raise RuntimeError('Папка состояния должна находиться отдельно от приложения и файлов установщика.')


def require_layout(app):
    safe_path(app)
    if SYSTEM == 'windows' and any(x.lower() == 'windowsapps' for x in app.parts):
        raise RuntimeError('Пакеты MSIX/Microsoft Store в WindowsApps нельзя менять этим патчем. Используй обычную EXE-установку Claude.')
    for name in FILES:
        path = safe_path(app / name)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o7000:
            raise RuntimeError('Неподдерживаемый файл приложения: ' + name)
    if SYSTEM == 'windows':
        for parent in [app] + list(app.parents):
            if (parent / 'AppxManifest.xml').exists() or (parent / 'AppxSignature.p7x').exists():
                raise RuntimeError('Обнаружен MSIX-пакет. Его подпись и регистрацию установщик не меняет.')


def require_closed(app):
    """Inspect executable paths; never kill apps or alter updater settings."""
    if SYSTEM == 'windows':
        command = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $p=@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath); ConvertTo-Json -InputObject $p -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, encoding='utf-8', check=True)
        items = json.loads(result.stdout)
        prefix = os.path.normcase(str(app) + os.sep)
        updater = os.path.normcase(str(app.parent / 'Update.exe'))
        for item in items:
            executable = item.get('ExecutablePath')
            name = (item.get('Name') or '').lower()
            path = os.path.normcase(executable) if executable else ''
            if path.startswith(prefix) or path == updater or (not path and name in {'claude.exe', 'claude-desktop.exe'}):
                raise RuntimeError('Claude или его обновление ещё работает: ' + str(item['Name']) + ' (PID ' + str(item['ProcessId']) + '). Заверши Claude через меню выхода и повтори действие.')
    else:
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                command = (proc / 'comm').read_text().strip()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                raise RuntimeError('Не удалось прочитать список процессов. Повтори выбранное действие с sudo.')
            if command in {'apt', 'apt-get', 'dpkg', 'unattended-upgr'}:
                raise RuntimeError('Менеджер пакетов сейчас работает. Дождись завершения обновления.')
            try:
                exe = os.readlink(proc / 'exe').removesuffix(' (deleted)')
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                if command.lower().startswith(('claude', 'chrome')):
                    raise RuntimeError('Не удалось определить путь процесса Claude. Полностью закрой Claude или повтори с sudo.')
                continue
            if exe.startswith(str(app) + '/'):
                raise RuntimeError('Claude ещё работает (PID ' + proc.name + '). Полностью выйди из приложения и повтори действие.')


def match_profile(app):
    hashes = {name: file_hash(app / name) for name in FILES}
    for key, profile in profiles().items():
        if profile['platform'] == SYSTEM and hashes['resources/app.asar'] == profile['asar_sha256']:
            if hashes['resources/en-US.json'] != profile['native_sha256']:
                raise RuntimeError('Нативный каталог изменён или относится к другой сборке.')
            if SYSTEM == 'windows' and hashes['claude.exe'] != profile['exe_sha256']:
                raise RuntimeError('EXE изменён или относится к другой сборке.')
            archive = Asar((app / 'resources/app.asar').read_bytes())
            if json.loads(archive.read('package.json'))['version'] != profile['version']:
                raise RuntimeError('Версия в ASAR не соответствует профилю.')
            return key, profile
    raise RuntimeError('Эта исходная сборка пока не поддерживается. Выбери статус; неизвестные файлы не изменены.')


def build(app, profile):
    originals = {name: (app / name).read_bytes() for name in FILES}
    expected = {'resources/app.asar': profile['asar_sha256'], 'resources/en-US.json': profile['native_sha256']}
    if SYSTEM == 'windows':
        expected['claude.exe'] = profile['exe_sha256']
    if any(sha(originals[name]) != expected[name] for name in FILES):
        raise RuntimeError('Приложение обновилось во время подготовки.')
    dictionary = json.loads((ROOT / 'macos/ru.json').read_text(encoding='utf-8'))
    native_ru = json.loads((ROOT / 'macos/native-ru.json').read_text(encoding='utf-8'))
    exact, templates = compile_catalog(dictionary)
    runtime = (ROOT / 'macos/ui-runtime.js').read_text(encoding='utf-8')
    runtime = runtime.replace('__RU_DICTIONARY__', json.dumps(exact, ensure_ascii=False))
    runtime = runtime.replace('__RU_TEMPLATES__', json.dumps(templates, ensure_ascii=False)).encode('utf-8')
    archive = Asar(originals['resources/app.asar'])
    changes = {name: archive.read(name) + b'\n;\n// Claude RU interface translator v3\n' + runtime + b'\n' for name in profile['preloads']}
    asar, header_hash = archive.replace(changes)
    native = json.loads(originals['resources/en-US.json'])
    count = 0
    for key, value in native_ru.items():
        if key in native:
            native[key] = value
            count += 1
    patched = {'resources/app.asar': asar,
               'resources/en-US.json': (json.dumps(native, ensure_ascii=False, indent=2) + '\n').encode('utf-8')}
    if SYSTEM == 'windows':
        from pe_integrity import replace_integrity
        size = struct.unpack('<I', originals['resources/app.asar'][12:16])[0]
        old_header = sha(originals['resources/app.asar'][16:16 + size])
        patched['claude.exe'] = replace_integrity(originals['claude.exe'], old_header, header_hash)
    return originals, patched, count


def require_hashes(app, state, choices):
    for name, record in state['files'].items():
        if file_hash(safe_path(app / name)) not in {record[x] for x in choices}:
            raise RuntimeError('Файл изменился после установки/подготовки: ' + name + '. Автоматическая перезапись остановлена.')


def backup_bytes(directory, state, name, kind, uid):
    path = safe_path(directory / state['backup'] / kind / name)
    owned_file(path, uid)
    data = path.read_bytes()
    if sha(data) != state['files'][name][kind]:
        raise RuntimeError('Резервная копия повреждена: ' + name)
    return data


def replace_file(target, data, record):
    safe_path(target)
    descriptor, temporary = tempfile.mkstemp(prefix='.claude-ru-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if SYSTEM == 'linux':
                os.fchmod(stream.fileno(), record['mode'])
                if os.geteuid() == 0:
                    os.fchown(stream.fileno(), record['uid'], record['gid'])
        os.replace(temporary, target)
    except Exception:
        # Keep the prepared file for diagnosis; the original backup remains.
        print('Сохранён промежуточный файл: ' + temporary, file=sys.stderr)
        raise


def install(args, app, directory, uid, gid, state):
    require_layout(app)
    require_closed(app)
    if state and state['phase'] == 'installed':
        require_hashes(app, state, ['patched'])
        print('[OK] Патч уже установлен: ' + str(app))
        return
    if state and state['phase'] != 'restored':
        raise RuntimeError('Предыдущая операция не завершена. Сначала выбери откат с тем же --app и --state-dir.')
    if SYSTEM == 'windows' and not args.approve_exe_signature:
        raise RuntimeError('Нужно согласие --approve-exe-signature: после изменения EXE его подпись Anthropic будет недействительной.')
    key, profile = match_profile(app)
    originals, patched, count = build(app, profile)
    backup_name = 'backup-' + uuid.uuid4().hex
    backup = directory / backup_name
    private_directory(backup, uid, gid)
    records = {}
    for name in FILES:
        info = (app / name).stat()
        records[name] = {'original': sha(originals[name]), 'patched': sha(patched[name]),
                         'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
        for kind, data in (('original', originals[name]), ('patched', patched[name])):
            path = backup / kind / name
            private_directory(path.parent, uid, gid)
            write_private(path, data, uid, gid)
    state = {'schema': 1, 'product': 'claude-russian-portable', 'platform': SYSTEM, 'profile': key,
             'phase': 'prepared', 'app': str(app), 'backup': backup_name, 'files': records,
             'native_entries': count, 'installed_at': datetime.now(timezone.utc).isoformat()}
    save_state(directory, state, uid, gid)
    require_closed(app)
    require_hashes(app, state, ['original'])
    for name in FILES:
        require_hashes(app, state, ['original', 'patched'])
        replace_file(app / name, patched[name], records[name])
    state['phase'] = 'installed'
    save_state(directory, state, uid, gid)
    print('[OK] Патч применён к исходной установке: ' + str(app))
    print('Резервная копия всех заменённых файлов: ' + str(backup))
    print('Claude не запущен. Запусти его самостоятельно.')


def restore(app, directory, uid, gid, state):
    if not state:
        raise RuntimeError('Запись установки не найдена в указанной папке состояния.')
    require_layout(app)
    require_closed(app)
    require_hashes(app, state, ['original', 'patched'])
    originals = {name: backup_bytes(directory, state, name, 'original', uid) for name in FILES}
    if state['phase'] == 'restored':
        require_hashes(app, state, ['original'])
        print('[OK] Исходные файлы уже восстановлены.')
        return
    # Backup contains both byte variants; preserve interrupted transactions too.
    for name in FILES:
        backup_bytes(directory, state, name, 'patched', uid)
    state['phase'] = 'restoring'
    save_state(directory, state, uid, gid)
    for name in FILES:
        require_hashes(app, state, ['original', 'patched'])
        replace_file(app / name, originals[name], state['files'][name])
    state['phase'] = 'restored'
    save_state(directory, state, uid, gid)
    print('[OK] Исходные файлы восстановлены: ' + str(app))
    print('Резервные копии сохранены. Профиль и чаты не изменены.')


def status(app, directory, state):
    print('Приложение: ' + str(app))
    print('Состояние: ' + str(directory))
    supported = [p['version'] + ' (' + p['arch'] + ')' for p in profiles().values() if p['platform'] == SYSTEM]
    print('Поддерживаемые сборки: ' + ', '.join(supported))
    require_layout(app)
    print('SHA256 app.asar: ' + file_hash(app / 'resources/app.asar'))
    if state:
        print('Фаза установки: ' + state['phase'])
        print('Резервная копия: ' + str(directory / state['backup']))
        require_hashes(app, state, ['original', 'patched'] if state['phase'] in {'prepared', 'restoring'} else ['patched'] if state['phase'] == 'installed' else ['original'])
        print('[OK] Файлы соответствуют сохранённому состоянию.')
    else:
        key, profile = match_profile(app)
        print('[OK] Совместимая исходная сборка: ' + key)


def main():
    if sys.version_info < (3, 9) or SYSTEM is None:
        raise RuntimeError('Нужны Windows/Linux и Python 3.9+. Для macOS используй основной install.sh.')
    parser = argparse.ArgumentParser(description='Русский интерфейс установленного Claude для Windows/Linux')
    parser.add_argument('action', choices=['install', 'status', 'restore'])
    parser.add_argument('--app', help='Папка установленного Claude: resources и исполняемый файл')
    parser.add_argument('--state-dir')
    parser.add_argument('--approve-exe-signature', action='store_true')
    args = parser.parse_args()
    uid, gid, default = identity()
    directory = safe_path(os.path.expanduser(args.state_dir)) if args.state_dir else safe_path(default)
    initial_state = load_state(directory, uid)
    initial_app = discover(args, initial_state)
    separate_state(directory, initial_app)
    if args.action == 'status':
        state, app = initial_state, initial_app
        if state:
            load_state(directory, uid, app)
        status(app, directory, state)
        return
    with lock(directory, uid, gid):
        state = load_state(directory, uid)
        app = discover(args, state)
        separate_state(directory, app)
        if app != initial_app:
            raise RuntimeError('Установка изменилась во время подготовки. Повтори действие.')
        if state:
            load_state(directory, uid, app)
        if args.action == 'install':
            install(args, app, directory, uid, gid, state)
        else:
            restore(app, directory, uid, gid, state)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, struct.error, subprocess.SubprocessError) as error:
        print('[Ошибка] ' + str(error), file=sys.stderr)
        print('Если подготовка уже началась, резервные копии сохранены. После закрытия Claude выбери откат с той же папкой состояния.', file=sys.stderr)
        sys.exit(1)
