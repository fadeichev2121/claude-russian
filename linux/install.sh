#!/bin/bash
# MIT License. Only the patch action can request administrator access.
set -euo pipefail
umask 077

usage() {
  cat <<'HELP'
Русский интерфейс Claude для Linux

  bash install.sh                         — меню
  bash install.sh install                 — установить перевод
  bash install.sh status                  — статус / совместимость
  bash install.sh restore                 — восстановить исходные файлы
  bash install.sh auto-enable             — включить автовосстановление
  bash install.sh auto-disable            — выключить автовосстановление
  bash install.sh auto-status             — состояние автовосстановления
  bash install.sh auto-check              — проверить обновление сейчас
  bash install.sh status --app /путь      — указать папку приложения
  bash install.sh --help                  — справка

Параметры: --app ПАПКА, --state-dir ПАПКА, --control-dir ПАПКА (автовосстановление).
По умолчанию: /usr/lib/claude-desktop. Нужны Linux и Python 3.9+.
Поддерживается установленный официальный DEB-пакет Claude.
Сначала полностью закрой Claude и его обновление.
Запускай установщик из обычного аккаунта, без sudo.
HELP
}

ACTION=menu
if [ "$#" -gt 0 ]; then
  case "$1" in
    menu|install|status|restore|auto-enable|auto-disable|auto-status|auto-check) ACTION="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
    --app|--state-dir|--control-dir) ;;
    *) printf '[Ошибка] Неизвестное действие: %s\n' "$1" >&2; exit 1 ;;
  esac
fi
APP=/usr/lib/claude-desktop
APP_EXPLICIT=0
STATE_DIR=
CONTROL_DIR=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --app|--state-dir|--control-dir)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        printf '[Ошибка] После %s нужна папка.\n' "$1" >&2; exit 1
      fi
      case "$1" in
        --app) APP="$2"; APP_EXPLICIT=1 ;;
        --state-dir) STATE_DIR="$2" ;;
        --control-dir) CONTROL_DIR="$2" ;;
      esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '[Ошибка] Неизвестный параметр: %s\n' "$1" >&2; exit 1 ;;
  esac
done

if [ "$(uname -s)" != Linux ]; then
  printf '[Ошибка] Этот установщик предназначен для Linux.\n' >&2; exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  printf '[Ошибка] Запусти установщик из обычного аккаунта, без sudo.\n' >&2
  printf 'Пароль администратора будет запрошен только для изменения файлов Claude.\n' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '[Ошибка] Установи Python 3.9+ и повтори запуск.\n' >&2; exit 1
fi
PYTHON_BIN="$(command -v python3)"
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  printf '[Ошибка] Нужен работающий Python 3.9 или новее.\n' >&2; exit 1
fi
PYTHON_BIN="$("$PYTHON_BIN" -c 'import os,sys; print(os.path.realpath(sys.executable))')"
APP="$("$PYTHON_BIN" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$APP")"
PATCH_ARGS=(--app "$APP")
MANAGER_ARGS=()
if [ "$APP_EXPLICIT" -eq 1 ]; then MANAGER_ARGS+=(--app "$APP"); fi
if [ -n "$STATE_DIR" ]; then
  STATE_DIR="$("$PYTHON_BIN" -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$STATE_DIR")"
  PATCH_ARGS+=(--state-dir "$STATE_DIR")
  MANAGER_ARGS+=(--state-dir "$STATE_DIR")
fi
if [ -n "$CONTROL_DIR" ]; then MANAGER_ARGS+=(--control-dir "$CONTROL_DIR"); fi
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"

# A standalone downloaded launcher obtains the whole source package in a
# private user directory. A clone or extracted package is used as-is.
if [ ! -f "$ROOT/manifest.json" ] || [ ! -f "$ROOT/portable/patch.py" ]; then
  printf '[i] Загружаю исходные файлы русификатора из GitHub…\n' >&2
  ROOT="$("$PYTHON_BIN" - <<'PYDOWNLOAD'
import json, os, pwd, shutil, sys, tarfile, tempfile, urllib.request
from pathlib import Path, PurePosixPath

LIMIT = 25 * 1024 * 1024
home = Path(pwd.getpwuid(os.getuid()).pw_dir)
support = home / '.local' / 'share' / 'claude-russian'

def private_directory(path):
    for part in reversed([path] + list(path.parents)):
        if part.is_symlink():
            raise SystemExit('Ошибка: папка загрузки содержит символическую ссылку.')
        if part.exists() and not part.is_dir():
            raise SystemExit('Ошибка: путь загрузки не является папкой.')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise SystemExit('Ошибка: папка загрузки принадлежит другому пользователю.')
    path.chmod(0o700)

private_directory(support)
packages = support / 'packages'
private_directory(packages)
temporary = Path(tempfile.mkdtemp(prefix='download.', dir=support))
try:
    archive = temporary / 'source.tar.gz'
    request = urllib.request.Request(
        'https://codeload.github.com/fadeichev2121/claude-russian/tar.gz/refs/heads/main',
        headers={'User-Agent': 'claude-russian-installer'})
    size = 0
    with urllib.request.urlopen(request, timeout=30) as response, archive.open('xb') as output:
        if not response.geturl().startswith('https://codeload.github.com/'):
            raise SystemExit('Ошибка: неожиданный адрес загрузки.')
        while True:
            block = response.read(65536)
            if not block:
                break
            size += len(block)
            if size > LIMIT:
                raise SystemExit('Ошибка: размер загрузки превышает 25 МБ.')
            output.write(block)
    destination = temporary / 'package'
    destination.mkdir(mode=0o700)
    with tarfile.open(archive, 'r:gz') as source:
        members, names, total = [], set(), 0
        for item in source:
            if len(members) >= 2000:
                raise SystemExit('Ошибка: в пакете больше 2000 файлов и папок.')
            parts = PurePosixPath(item.name).parts
            if (not parts or parts[0] != 'claude-russian-main'
                    or '..' in parts or '\\' in item.name
                    or PurePosixPath(item.name).is_absolute()
                    or any(':' in part for part in parts)):
                raise SystemExit('Ошибка: небезопасный путь в архиве.')
            if not (item.isdir() or item.isfile()) or item.size < 0:
                raise SystemExit('Ошибка: ссылки и специальные файлы в пакете запрещены.')
            name = '/'.join(parts)
            if name in names or (len(parts) == 1 and not item.isdir()):
                raise SystemExit('Ошибка: повторяющийся или некорректный путь в пакете.')
            names.add(name)
            total += item.size
            if total > LIMIT:
                raise SystemExit('Ошибка: распакованный пакет превышает 25 МБ.')
            members.append(item)
        for item in members:
            parts = PurePosixPath(item.name).parts
            if len(parts) == 1:
                continue
            target = destination.joinpath(*parts[1:])
            if item.isdir():
                target.mkdir(parents=True, mode=0o700, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with source.extractfile(item) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output, length=65536)
            target.chmod(0o600)
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('repository') != 'claude-russian' or manifest.get('format') != 1:
        raise SystemExit('Ошибка: загружен пакет другого проекта или неподдерживаемого формата.')
    for name in ('portable/patch.py', 'portable/profiles.json', 'portable/windows-profiles.json',
                 'portable/linux-profiles.json', 'portable/pe_integrity.py',
                 'macos/asar.py', 'macos/catalog.py', 'macos/ru.json',
                 'macos/native-ru.json', 'macos/ui-runtime.js',
                 'linux/install.sh', 'windows/install.ps1', 'updater/manager.py',
                 'updater/adapter.py', 'updater/package.py', 'updater/service.py'):
        if not (destination / name).is_file():
            raise SystemExit('Ошибка: в пакете не хватает файлов.')
    retained = packages / temporary.name
    destination.rename(retained)
    print(retained)
except Exception as error:
    raise SystemExit('Ошибка загрузки пакета: ' + str(error))
finally:
    shutil.rmtree(temporary)
PYDOWNLOAD
)"
fi
"$PYTHON_BIN" - "$ROOT/manifest.json" <<'PYMANIFEST'
import json, sys
with open(sys.argv[1], encoding='utf-8') as source:
    manifest = json.load(source)
if manifest.get('repository') != 'claude-russian' or manifest.get('format') != 1:
    raise SystemExit('Ошибка: установщик и пакет относятся к разным проектам или формат пакета не поддерживается.')
PYMANIFEST
PATCH="$ROOT/portable/patch.py"
MANAGER="$ROOT/updater/manager.py"
run_manager() {
  local action="$1"
  shift
  "$PYTHON_BIN" "$MANAGER" "$action" ${MANAGER_ARGS[@]+"${MANAGER_ARGS[@]}"} "$@"
}

run_patch() {
  local action="$1"
  local need_admin=0
  if [ "$action" != status ]; then
    # Both targets are replaced atomically; their parent must be writable.
    if [ -d "$APP/resources" ] && [ ! -w "$APP/resources" ]; then
      need_admin=1
    fi
    for target in "$APP/resources/app.asar" "$APP/resources/en-US.json"; do
      if [ -e "$target" ] && [ ! -w "$target" ]; then need_admin=1; fi
    done
  fi
  if [ "$need_admin" -eq 1 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
      printf '[Ошибка] Для изменения %s нужен sudo; он не установлен.\n' "$APP/resources" >&2
      return 1
    fi
    printf '[i] Для изменения файлов в %s нужен доступ администратора.\n' "$APP/resources"
    printf '[i] Введи пароль в Терминале; символы при вводе не отображаются.\n'
    # sudo supplies SUDO_UID; the portable core keeps state for this user.
    sudo -- "$PYTHON_BIN" "$PATCH" "$action" "${PATCH_ARGS[@]}"
  else
    "$PYTHON_BIN" "$PATCH" "$action" "${PATCH_ARGS[@]}"
  fi
}

perform_action() {
  local action="$1"
  case "$action" in
    auto-enable) run_manager enable; return $? ;;
    auto-disable) run_manager disable; return $? ;;
    auto-status) run_manager status; return $? ;;
    auto-check) run_manager check --manual; return $? ;;
    restore)
      if run_manager restore --manual; then return 0; else
        local result=$?
        if [ "$result" -ne 3 ]; then return "$result"; fi
      fi ;;
  esac
  if [ "$action" = install ]; then
    printf '\nЗакрой Claude и дождись завершения его обновления.\n'
    printf 'Будут изменены app.asar и en-US.json; исходные два файла сохранятся для отката.\n'
  fi
  run_patch "$action"
}

if [ "$ACTION" != menu ]; then
  perform_action "$ACTION"
  exit $?
fi
if [ ! -t 0 ]; then
  printf '[Ошибка] Запусти меню в Терминале или укажи действие install, status, restore.\n' >&2
  exit 1
fi
while true; do
  printf '\n==== Русский интерфейс Claude для Linux ====\n'
  printf 'Папка приложения: %s\n' "$APP"
  printf ' 1) Установить русский интерфейс\n 2) Статус / совместимость\n 3) Откат\n'
  printf ' 4) Включить автовосстановление после обновлений\n 5) Выключить автовосстановление\n'
  printf ' 6) Статус автовосстановления\n 7) Проверить обновление сейчас\n 0) Выход\nВыбор: '
  if ! IFS= read -r choice; then printf '\n'; exit 0; fi
  case "$choice" in
    1) operation=install ;;
    2) operation=status ;;
    3) operation=restore ;;
    4) operation=auto-enable ;;
    5) operation=auto-disable ;;
    6) operation=auto-status ;;
    7) operation=auto-check ;;
    0) exit 0 ;;
    *) printf '[Ошибка] Выбери пункт от 0 до 7.\n' >&2; continue ;;
  esac
  if perform_action "$operation"; then
    :
  else
    printf '[Ошибка] Действие не завершено. Причина указана выше.\n' >&2
  fi
done
