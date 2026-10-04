#!/usr/bin/env python3
"""Localize the installed Claude bundle with a complete recoverable backup."""
import sys

if sys.version_info < (3, 9):
    print("Ошибка: требуется Python 3.9 или новее.", file=sys.stderr)
    sys.exit(1)

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from datetime import datetime, timezone
import uuid
from contextlib import contextmanager

from asar import Asar
from catalog import compile_catalog

PACKAGE = Path(__file__).resolve().parent
VERSION = "1.46388.4"
SOURCE_HASH = "c48a2abd9aeba23843a09f2d5e1ce9207f2bebe9af014ff9ccd70862b136e9d7"
PRELOADS = [".vite/build/mainView.js", ".vite/build/mainWindow.js"]
MAGIC = {
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def user_identity():
    uid = os.getuid()
    if uid == 0 and os.environ.get("SUDO_UID"):
        try:
            uid = int(os.environ["SUDO_UID"])
        except ValueError:
            raise RuntimeError("Некорректный SUDO_UID; запусти установщик без sudo.")
    account = pwd.getpwuid(uid)
    return uid, account.pw_gid, Path(account.pw_dir)


def path_arg(value, default, home):
    if value is None:
        return default
    if value == "~":
        value = str(home)
    elif value.startswith("~/"):
        value = str(home / value[2:])
    return Path(os.path.abspath(value))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def owner(path, uid, gid):
    if os.geteuid() == 0:
        os.chown(path, uid, gid, follow_symlinks=False)


def own_tree(root, uid, gid):
    owner(root, uid, gid)
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in subdirs + files:
            owner(Path(directory) / name, uid, gid)


def no_symlink_components(path):
    for part in [path] + list(path.parents):
        if part.is_symlink():
            raise RuntimeError("Путь содержит символическую ссылку: " + str(part))


def private_dir(path, uid, gid):
    no_symlink_components(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise RuntimeError("Вместо каталога найден файл: " + str(current))
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        owner(directory, uid, gid)
    if not path.is_dir():
        raise RuntimeError("Вместо каталога найден файл: " + str(path))
    if path.stat().st_uid != uid:
        raise RuntimeError("Каталог состояния принадлежит другому пользователю: " + str(path))
    path.chmod(0o700)


def require_private_directory(path, uid):
    if not path.is_dir() or path.stat().st_uid != uid or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise RuntimeError("Каталог состояния должен принадлежать текущему пользователю и иметь права 0700: " + str(path))


def recorded_path(value, label):
    if not isinstance(value, str) or not value or "\0" in value:
        raise RuntimeError("Некорректный путь в состоянии: " + label)
    path = Path(value)
    if not path.is_absolute() or str(Path(os.path.abspath(value))) != value:
        raise RuntimeError("Некорректный абсолютный путь в состоянии: " + label)
    return path


def validate_child(value, parent, prefix, suffix=""):
    path = recorded_path(value, prefix)
    if path.parent != parent or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}" + re.escape(suffix), path.name):
        raise RuntimeError("Некорректный служебный путь в состоянии.")
    no_symlink_components(path)
    return path


def read_state(state_dir, uid, source):
    no_symlink_components(state_dir)
    if state_dir.exists():
        require_private_directory(state_dir, uid)
    path = state_dir / "state.json"
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or metadata.st_size > 128 * 1024):
            raise RuntimeError("Небезопасный файл состояния: нужны владелец текущего пользователя, права 0600, одна ссылка и размер до 128 КБ.")
        raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise RuntimeError("Файл состояния превышает 128 КБ.")
        state = json.loads(raw)
    if (not isinstance(state, dict) or type(state.get("schema")) is not int or state["schema"] != 2
            or state.get("product") != "claude-russian" or state.get("version") != VERSION
            or state.get("phase") not in {"prepared", "installed", "restoring", "restored"}):
        raise RuntimeError("Неизвестный формат состояния; приложение не изменено.")
    for field in ("source_asar_sha256", "original_manifest_sha256", "patched_manifest_sha256"):
        value = state.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise RuntimeError("Некорректный SHA256 в состоянии: " + field)
    if state["source_asar_sha256"] != SOURCE_HASH:
        raise RuntimeError("Состояние относится к другой сборке Claude.")
    if recorded_path(state.get("app"), "app") != source:
        raise RuntimeError("Путь --app не совпадает с сохранённой установкой. Укажи тот же путь, что при установке.")
    validate_child(state.get("backup"), state_dir, "backup-", ".app")
    validate_child(state.get("original_manifest"), state_dir, "original-", ".json")
    validate_child(state.get("patched_manifest"), state_dir, "patched-", ".json")
    transaction = validate_child(state.get("transaction_dir"), source.parent, ".claude-russian-transaction-")
    if recorded_path(state.get("preserved_original"), "preserved_original") != transaction / "original.app":
        raise RuntimeError("Некорректный путь сохранённого оригинала в состоянии.")
    if type(state.get("original_uid")) is not int or type(state.get("original_gid")) is not int:
        raise RuntimeError("Некорректный владелец оригинального приложения в состоянии.")
    if state.get("phase") in {"restoring", "restored"}:
        restore_transaction = validate_child(state.get("restore_transaction_dir"), source.parent, ".claude-russian-transaction-")
        if recorded_path(state.get("recovered_copy"), "recovered_copy") != restore_transaction / "recovery-patched.app":
            raise RuntimeError("Некорректный путь восстановленной копии в состоянии.")
    return state


@contextmanager
def state_lock(state_dir, uid, gid):
    no_symlink_components(state_dir)
    if state_dir.exists():
        require_private_directory(state_dir, uid)
    private_dir(state_dir, uid, gid)
    lock_path = state_dir / "state.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
    except FileExistsError:
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
            raise RuntimeError("Небезопасный файл блокировки состояния; операция остановлена.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Другая установка или восстановление уже выполняется. Дождись её завершения.")
        yield
    finally:
        os.close(fd)


def save_state(state_dir, state, uid, gid):
    private_dir(state_dir, uid, gid)
    destination = state_dir / "state.json"
    if destination.is_symlink():
        raise RuntimeError("Файл состояния является символической ссылкой.")
    fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=state_dir)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()


def move_exclusive(source, destination):
    """macOS atomic rename without replacing an existing file or directory."""
    no_symlink_components(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # AT_FDCWD=-2; RENAME_EXCL=0x4 on Darwin.
    if rename(-2, os.fsencode(source), -2, os.fsencode(destination), 0x4):
        error = ctypes.get_errno()
        if error == errno.EXDEV:
            raise RuntimeError("Служебная папка и приложение находятся на разных дисках; безопасное перемещение остановлено.")
        if error == errno.EEXIST:
            raise RuntimeError("Путь уже существует и не будет перезаписан: " + str(destination))
        raise OSError(error, os.strerror(error), str(destination))


def tools_available():
    for tool in ("codesign", "ditto", "xcode-select", "ps"):
        if shutil.which(tool) is None:
            raise RuntimeError("Не найден " + tool + ". Установи инструменты разработчика командой xcode-select --install и повтори запуск.")
    result = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Не установлены Xcode Command Line Tools. Выполни xcode-select --install, дождись установки и повтори запуск.")


def require_closed(*apps):
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Не удалось определить, закрыт ли Claude. Подготовка остановлена.")
    prefixes = {str(app) + "/Contents/" for app in apps}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0] == str(os.getpid()):
            continue
        if any(prefix in parts[1] for prefix in prefixes):
            raise RuntimeError("Сначала полностью закрой Claude через Cmd+Q, затем повтори команду.")


def entitlements(target):
    result = subprocess.run(["codesign", "-d", "--entitlements", "-", "--xml", str(target)], capture_output=True)
    if result.returncode:
        raise RuntimeError("Не удалось прочитать подпись: " + target.name)
    raw = result.stdout
    if not raw.strip():
        return {}
    start = raw.find(b"<?xml")
    if start < 0:
        start = raw.find(b"<plist")
    if start < 0:
        raise RuntimeError("Не удалось прочитать разрешения подписи: " + target.name)
    data = plistlib.loads(raw[start:])
    # Identities bound to Anthropic's certificate cannot be claimed by our copy.
    remove = {
        "com.apple.application-identifier", "com.apple.developer.team-identifier",
        "keychain-access-groups", "com.apple.security.application-groups",
    }
    return {key: value for key, value in data.items() if key not in remove}


def executable_macho(target):
    if not target.is_file():
        return False
    with target.open("rb") as stream:
        header = stream.read(32)
        magic = header[:4]
        if magic in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
            endian = ">" if magic in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"} else "<"
            is64 = magic in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
            offset = struct.unpack(endian + ("Q" if is64 else "I"), header[16:24] if is64 else header[16:20])[0]
            stream.seek(offset)
            header = stream.read(16)
            magic = header[:4]
        if magic not in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}:
            return False
        endian = "<" if magic in {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"} else ">"
        return struct.unpack(endian + "I", header[12:16])[0] == 2


def sign_copy(copy, scratch):
    paths = list(copy.rglob("*"))
    binaries = []
    for path in paths:
        if not path.is_symlink() and path.is_file():
            with path.open("rb") as stream:
                if stream.read(4) in MAGIC:
                    binaries.append(path)
    bundles = [path for path in paths if not path.is_symlink() and path.is_dir()
               and path.suffix in {".app", ".framework", ".xpc", ".bundle"}]
    targets = sorted(binaries, key=lambda path: len(path.parts), reverse=True)
    targets += sorted(bundles, key=lambda path: len(path.parts), reverse=True)
    targets.append(copy)
    for index, target in enumerate(targets):
        filtered = entitlements(target)
        # Ad-hoc signing has no Team ID; the Electron framework needs this
        # exception. Explicit consent is mandatory before building the copy.
        if (target.is_dir() and target.suffix in {".app", ".xpc"}) or executable_macho(target):
            filtered["com.apple.security.cs.disable-library-validation"] = True
        path = scratch / ("entitlements-" + str(index) + ".plist")
        path.write_bytes(plistlib.dumps(filtered))
        path.chmod(0o600)
        result = subprocess.run([
            "codesign", "--force", "--sign", "-", "--preserve-metadata=identifier,flags,runtime",
            "--entitlements", str(path), str(target),
        ], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Не удалось подписать копию: " + str(target.relative_to(copy)) + "\n" + result.stderr[-1500:])
    return len(targets)


def manifest(app):
    """Hash all files, modes and symlink targets without following symlinks."""
    no_symlink_components(app)
    if not app.is_dir():
        raise RuntimeError("Приложение не найдено: " + str(app))
    result = {".": {"type": "directory", "mode": stat.S_IMODE(app.stat().st_mode)}}
    for directory, subdirs, files in os.walk(app, followlinks=False):
        for name in sorted(subdirs + files):
            path = Path(directory) / name
            item = path.lstat()
            key = str(path.relative_to(app))
            if stat.S_ISLNK(item.st_mode):
                result[key] = {"type": "symlink", "target": os.readlink(path)}
            elif stat.S_ISDIR(item.st_mode):
                result[key] = {"type": "directory", "mode": stat.S_IMODE(item.st_mode)}
            elif stat.S_ISREG(item.st_mode):
                result[key] = {"type": "file", "mode": stat.S_IMODE(item.st_mode), "size": item.st_size, "sha256": file_hash(path)}
            else:
                raise RuntimeError("В приложении найден неподдерживаемый специальный файл: " + key)
    return result


def save_manifest(path, data, uid, gid):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, gid)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return sha(raw)


def load_manifest(state, kind, uid):
    path = Path(state[kind + "_manifest"])
    no_symlink_components(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or metadata.st_size > 32 * 1024 * 1024):
            raise RuntimeError("Небезопасный файл контрольных сумм.")
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or sha(raw) != state[kind + "_manifest_sha256"]:
        raise RuntimeError("Файл контрольных сумм изменён; восстановление остановлено.")
    data = json.loads(raw)
    if not isinstance(data, dict) or not data:
        raise RuntimeError("Некорректный список контрольных сумм.")
    if kind == "original" and data.get("Contents/Resources/app.asar", {}).get("sha256") != SOURCE_HASH:
        raise RuntimeError("Резервная копия относится к другой сборке.")
    return data


def require_manifest(app, expected, description):
    if manifest(app) != expected:
        raise RuntimeError(description + " изменилась. Операция остановлена; данные не перезаписаны.")


def make_transaction(source, uid, gid):
    path = source.parent / (".claude-russian-transaction-" + uuid.uuid4().hex)
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    owner(path, uid, gid)
    return path


def replace_bundle(source, replacement, saved_old):
    move_exclusive(source, saved_old)
    try:
        move_exclusive(replacement, source)
    except Exception:
        if not source.exists() and not source.is_symlink():
            move_exclusive(saved_old, source)
        raise


def install(args, source, state_dir, uid, gid):
    previous = read_state(state_dir, uid, source)
    if previous and previous["phase"] != "restored":
        if previous["phase"] == "installed":
            require_manifest(source, load_manifest(previous, "patched", uid), "Установленный Claude")
            print("[OK] Русский патч уже установлен: " + str(source))
            return
        raise RuntimeError("Предыдущая операция не завершена. Сначала выполни restore с теми же --app и --state-dir.")
    if not args.approve_local_signature:
        raise RuntimeError("Нужно согласие --approve-local-signature: оригинальный Claude получит локальную подпись и исключение проверки библиотек. Возможен повторный вход и запрос системных разрешений.")
    if not source.is_dir():
        raise RuntimeError("Claude.app не найден: " + str(source))
    info_path = source / "Contents/Info.plist"
    archive_path = source / "Contents/Resources/app.asar"
    no_symlink_components(info_path)
    no_symlink_components(archive_path)
    source_info_bytes = info_path.read_bytes()
    info = plistlib.loads(source_info_bytes)
    version = info.get("CFBundleShortVersionString", "неизвестна")
    if version != VERSION:
        raise RuntimeError("Версия " + str(version) + " несовместима. Поддерживается только Claude " + VERSION + ".")
    source_data = archive_path.read_bytes()
    if sha(source_data) != SOURCE_HASH:
        raise RuntimeError("Архив другой сборки Claude. Установка остановлена до изменения приложения.")
    tools_available()
    require_closed(source)
    if not os.access(source.parent, os.W_OK):
        raise RuntimeError("Нет права изменять каталог приложения. Повтори установку через sudo.")
    dictionary = json.loads((PACKAGE / "ru.json").read_text(encoding="utf-8"))
    native = json.loads((PACKAGE / "native-ru.json").read_text(encoding="utf-8"))
    if not dictionary or not all(isinstance(key, str) and isinstance(value, str) for key, value in dictionary.items()):
        raise RuntimeError("Некорректный словарь интерфейса.")
    asar = Asar(source_data)
    exact, templates = compile_catalog(dictionary)
    runtime = (PACKAGE / "ui-runtime.js").read_text(encoding="utf-8")
    runtime = runtime.replace("__RU_DICTIONARY__", json.dumps(exact, ensure_ascii=False))
    runtime = runtime.replace("__RU_TEMPLATES__", json.dumps(templates, ensure_ascii=False))
    changes = {name: asar.read(name) + b"\n;\n// Claude RU interface translator v3\n" + runtime.encode("utf-8") + b"\n" for name in PRELOADS}
    patched, header_hash = asar.replace(changes)
    source_metadata = source.stat()
    original = manifest(source)
    if (original.get("Contents/Resources/app.asar", {}).get("sha256") != SOURCE_HASH
            or original.get("Contents/Info.plist", {}).get("sha256") != sha(source_info_bytes)):
        raise RuntimeError("Claude обновился во время подготовки. Установка остановлена до резервного копирования и изменения приложения.")
    identity = uuid.uuid4().hex
    backup = state_dir / ("backup-" + identity + ".app")
    transaction = make_transaction(source, uid, gid)
    copy = transaction / "patched.app"
    scratch = transaction / "signing"
    scratch.mkdir(mode=0o700)
    owner(scratch, uid, gid)
    print("Сохраняю полный оригинальный Claude с официальной подписью…", flush=True)
    try:
        subprocess.run(["ditto", str(source), str(backup)], check=True)
        own_tree(backup, uid, gid)
        require_manifest(backup, original, "Резервная копия")
        subprocess.run(["ditto", str(backup), str(copy)], check=True)
        require_manifest(copy, original, "Промежуточная копия")
        (copy / "Contents/Resources/app.asar").write_bytes(patched)
        info["ElectronAsarIntegrity"] = {"Resources/app.asar": {"algorithm": "SHA256", "hash": header_hash}}
        (copy / "Contents/Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=False))
        native_path = copy / "Contents/Resources/en-US.json"
        original_native = json.loads(native_path.read_text(encoding="utf-8"))
        for key, value in native.items():
            if key not in original_native or not isinstance(value, str):
                raise RuntimeError("Некорректная запись нативного словаря.")
            original_native[key] = value
        native_path.write_text(json.dumps(original_native, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Подготавливаю перевод и локальную подпись…", flush=True)
        signed = sign_copy(copy, scratch)
        own_tree(copy, source_metadata.st_uid, source_metadata.st_gid)
        patched_manifest = manifest(copy)
        original_file = state_dir / ("original-" + identity + ".json")
        patched_file = state_dir / ("patched-" + identity + ".json")
        state = {
            "schema": 2, "product": "claude-russian", "phase": "prepared", "version": VERSION,
            "app": str(source), "backup": str(backup), "transaction_dir": str(transaction),
            "preserved_original": str(transaction / "original.app"),
            "original_uid": source_metadata.st_uid, "original_gid": source_metadata.st_gid,
            "original_manifest": str(original_file), "patched_manifest": str(patched_file),
            "source_asar_sha256": SOURCE_HASH,
            "original_manifest_sha256": save_manifest(original_file, original, uid, gid),
            "patched_manifest_sha256": save_manifest(patched_file, patched_manifest, uid, gid),
            "dictionary_entries": len(dictionary), "native_entries": len(native), "signed_targets": signed,
            "installed_at": datetime.now(timezone.utc).isoformat(), "signature": "local ad-hoc",
            "library_validation_exception": True, "launched": False, "ui_verified": False,
        }
        save_state(state_dir, state, uid, gid)
        require_closed(source)
        require_manifest(source, original, "Оригинальное приложение во время подготовки")
        replace_bundle(source, copy, transaction / "original.app")
        state["phase"] = "installed"
        save_state(state_dir, state, uid, gid)
        print("[OK] Русский патч установлен в оригинальный Claude: " + str(source))
        print("Полная резервная копия сохранена: " + str(backup))
        print("Приложение не запущено. Профиль пользователя не изменён.")
    except Exception:
        print("Резервная и промежуточная папки сохранены: " + str(backup) + "; " + str(transaction), file=sys.stderr)
        raise


def describe_compatibility(source):
    print("Приложение: " + str(source))
    if not source.is_dir():
        print("[i] Claude.app не найден. Укажи его путь через --app.")
        return
    info_path = source / "Contents/Info.plist"
    archive_path = source / "Contents/Resources/app.asar"
    no_symlink_components(info_path)
    no_symlink_components(archive_path)
    if not info_path.is_file() or not archive_path.is_file():
        print("[i] Не найдены Info.plist или app.asar; совместимость не подтверждена.")
        return
    info = plistlib.loads(info_path.read_bytes())
    version = info.get("CFBundleShortVersionString", "неизвестна")
    digest = file_hash(archive_path)
    print("Найденная версия: " + str(version))
    print("SHA256 архива: " + digest)
    if version == VERSION and digest == SOURCE_HASH:
        print("[OK] Совместимая исходная версия и сборка.")
    elif version != VERSION:
        print("[i] Несовместимая версия. Поддерживается только Claude " + VERSION + ".")
    else:
        print("[i] Архив отличается от исходного: возможны установленный патч или другая сборка.")


def status(args, source, state_dir, uid):
    describe_compatibility(source)
    state = read_state(state_dir, uid, source)
    if not state:
        print("Этот пакет ещё не записывал установку.")
        return
    print("Резервная копия: " + state["backup"])
    if state["phase"] == "installed":
        require_manifest(source, load_manifest(state, "patched", uid), "Установленное приложение")
        print("[OK] Русский патч установлен; файлы приложения совпадают с сохранённым состоянием.")
    elif state["phase"] == "restored":
        print("[OK] Оригинальное приложение восстановлено. Предыдущий bundle сохранён: " + state["recovered_copy"])
    else:
        print("[i] Операция не завершена. Выполни restore с теми же параметрами.")


def restore(args, source, state_dir, uid, gid):
    state = read_state(state_dir, uid, source)
    if not state:
        print("Нет записанной установки для восстановления.")
        return
    if state["phase"] == "restored":
        print("Восстановление уже выполнено. Предыдущий bundle сохранён: " + state["recovered_copy"])
        return
    original = load_manifest(state, "original", uid)
    patched = load_manifest(state, "patched", uid)
    backup = Path(state["backup"])
    require_manifest(backup, original, "Резервная копия")
    require_closed(source)
    if state["phase"] == "prepared" and not source.exists():
        # A process may have stopped between the two atomic install renames.
        # Recover only the exact recorded original and prepared candidate.
        transaction = Path(state["transaction_dir"])
        require_private_directory(transaction, uid)
        preserved = Path(state["preserved_original"])
        prepared = transaction / "patched.app"
        recovered = transaction / "recovery-patched.app"
        require_manifest(preserved, original, "Сохранённый оригинальный bundle")
        require_manifest(prepared, patched, "Подготовленный patched bundle")
        if recovered.exists() or recovered.is_symlink():
            raise RuntimeError("Путь recovery-patched.app уже существует; сохранённые приложения не будут перезаписаны.")
        if not os.access(source.parent, os.W_OK):
            raise RuntimeError("Нет права изменять каталог приложения. Повтори восстановление через sudo.")
        require_closed(source, preserved, prepared)
        # RENAME_EXCL refuses any source app created by an updater or the user
        # after the checks above; no existing bundle is replaced or removed.
        move_exclusive(preserved, source)
        move_exclusive(prepared, recovered)
        state["phase"] = "restored"
        state["restore_transaction_dir"] = str(transaction)
        state["recovered_copy"] = str(recovered)
        state["restored_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state_dir, state, uid, gid)
        print("[OK] Прерванная установка отменена: полный оригинальный Claude возвращён на прежнее место.")
        print("Подготовленный патч сохранён: " + str(recovered))
        return
    if state["phase"] == "restoring":
        transaction = Path(state["restore_transaction_dir"])
        recovered = Path(state["recovered_copy"])
        if source.exists():
            current = manifest(source)
            if current == original and recovered.exists():
                require_manifest(recovered, patched, "Сохранённый patched bundle")
                state["phase"] = "restored"
                save_state(state_dir, state, uid, gid)
                print("[OK] Прерванное восстановление завершено.")
                return
            if current != patched:
                raise RuntimeError("Claude изменился после установки. Восстановление остановлено.")
        elif recovered.exists() and (transaction / "restored.app").exists():
            require_manifest(recovered, patched, "Сохранённый patched bundle")
            require_manifest(transaction / "restored.app", original, "Подготовленный оригинал")
            move_exclusive(transaction / "restored.app", source)
            state["phase"] = "restored"
            save_state(state_dir, state, uid, gid)
            print("[OK] Прерванное восстановление завершено.")
            return
        else:
            raise RuntimeError("Прерванное восстановление требует ручного разбора сохранённых папок.")
    else:
        current = manifest(source)
        if current != patched and not (state["phase"] == "prepared" and current == original):
            raise RuntimeError("Claude обновился или был изменён после установки. Неизвестное приложение не будет перезаписано.")
        if state["phase"] == "prepared" and current == original:
            # A failed install may have rolled the official bundle back already.
            # Preserve the prepared patch without replacing the intact original.
            transaction = Path(state["transaction_dir"])
            prepared = transaction / "patched.app"
            recovered = transaction / "recovery-patched.app"
            if prepared.exists():
                require_manifest(prepared, patched, "Подготовленный patched bundle")
                move_exclusive(prepared, recovered)
            elif recovered.exists():
                require_manifest(recovered, patched, "Сохранённый patched bundle")
            else:
                raise RuntimeError("Оригинал цел, но промежуточная папка отсутствует; требуется ручной разбор состояния.")
            state["phase"] = "restored"
            state["restore_transaction_dir"] = str(transaction)
            state["recovered_copy"] = str(recovered)
            state["restored_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state_dir, state, uid, gid)
            print("[OK] Оригинальный Claude уже был сохранён целиком и остался неизменным. Подготовленный патч сохранён: " + str(recovered))
            return
    if not os.access(source.parent, os.W_OK):
        raise RuntimeError("Нет права изменять каталог приложения. Повтори восстановление через sudo.")
    transaction = make_transaction(source, uid, gid)
    replacement = transaction / "restored.app"
    recovered = transaction / "recovery-patched.app"
    print("Подготавливаю полный оригинальный bundle из резервной копии…", flush=True)
    subprocess.run(["ditto", str(backup), str(replacement)], check=True)
    require_manifest(replacement, original, "Подготовленный оригинал")
    own_tree(replacement, state["original_uid"], state["original_gid"])
    require_closed(source)
    if manifest(source) != current:
        raise RuntimeError("Claude изменился во время восстановления; операция остановлена.")
    state["phase"] = "restoring"
    state["restore_transaction_dir"] = str(transaction)
    state["recovered_copy"] = str(recovered)
    save_state(state_dir, state, uid, gid)
    replace_bundle(source, replacement, recovered)
    state["phase"] = "restored"
    state["restored_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state_dir, state, uid, gid)
    print("[OK] Полный оригинальный Claude восстановлен: " + str(source))
    print("Предыдущий bundle сохранён: " + str(recovered))
    print("Профиль пользователя не изменён. Приложение не запущено.")


def main():
    parser = argparse.ArgumentParser(description="Русский интерфейс оригинального Claude для macOS с полным восстановлением.")
    parser.add_argument("command", choices=("install", "status", "restore"), help="Установить патч, показать состояние или восстановить оригинал")
    parser.add_argument("--app", help="Claude.app; по умолчанию /Applications/Claude.app")
    parser.add_argument("--state-dir", help="Состояние и backup; по умолчанию ~/Library/Application Support/claude-russian/state")
    parser.add_argument("--approve-local-signature", action="store_true", help="Согласие на локальную подпись и исключение проверки библиотек в оригинальном Claude")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("Этот патч предназначен только для macOS.")
    uid, gid, home = user_identity()
    source = path_arg(args.app, Path("/Applications/Claude.app"), home)
    state_dir = path_arg(args.state_dir, home / "Library/Application Support/claude-russian/state", home)
    for path in (source, state_dir):
        no_symlink_components(path)
    if source.suffix != ".app":
        raise RuntimeError("Путь --app должен оканчиваться на .app.")
    if source == state_dir or source in state_dir.parents or state_dir in source.parents:
        raise RuntimeError("Приложение и каталог состояния должны находиться отдельно друг от друга.")
    if args.command == "status":
        status(args, source, state_dir, uid)
    else:
        with state_lock(state_dir, uid, gid):
            if args.command == "install":
                install(args, source, state_dir, uid, gid)
            else:
                restore(args, source, state_dir, uid, gid)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print("Ошибка: " + (str(error) or "операция прервана пользователем") + ".", file=sys.stderr)
        sys.exit(1)
