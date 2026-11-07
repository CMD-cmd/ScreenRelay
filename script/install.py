#!/usr/bin/env python3
"""Migrate this automation to one native login service, preserving recovery data."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
SUPPORT = Path.home() / 'Library/Application Support/UUDisplayAutomation'
APP = Path.home() / 'Applications/UUDisplayAutomation.app'
AGENTS = Path.home() / 'Library/LaunchAgents'
LABEL = 'local.codex.uu-display'
LEGACY = [LABEL + '-' + suffix for suffix in ('watchdog', 'controller', 'detector')]


def refresh_app_metadata():
    """Invalidate this bundle's old metadata after copytree preserved its dates.

    Only directory dates and OS registrations change. Signed files and user
    configuration stay untouched. Indexing is best-effort: a paused/read-only
    Spotlight index must never prevent the recovery service from starting.
    """
    for path in (APP / 'Contents', APP):
        if path.exists():
            try:
                os.utime(path, None)
            except OSError as error:
                print('Could not update application metadata date:', error, file=sys.stderr)
    commands = [
        ['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-f', str(APP)],
        ['/usr/bin/mdimport', '-i', str(APP)],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, timeout=5)
            if result.returncode:
                print('Application metadata refresh deferred:', Path(command[0]).name, file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            print('Application metadata refresh deferred:', error, file=sys.stderr)

def exact_app_pids():
    output = subprocess.check_output(['/bin/ps', '-axo', 'pid=,command='], text=True)
    owned = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == str(APP / 'Contents/MacOS/UUDisplayAutomation'):
            owned.append(int(parts[0]))
    return owned

def atomic_json(path, value):
    """Durably replace one installer barrier without importing installed code."""
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def paused_state():
    path = SUPPORT / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else {'version': 1}
    if (not isinstance(state, dict) or state.get('journal')
            or state.get('mode', 'DISABLED') not in ('PAUSED', 'DISABLED')):
        sys.exit('Pause and recover the running automation before replacing its files.')
    return state


def install_pause_barrier():
    """Caller holds controller.lock after every old owner has stopped."""
    state = paused_state()  # Shutdown may have changed the initial snapshot.
    cursor_path = SUPPORT / 'cursor-profile-state.json'
    if cursor_path.exists():
        cursor = json.loads(cursor_path.read_text())
        if (not isinstance(cursor, dict) or cursor.get('pending_originals') != []
                or cursor.get('original_profile') is not None or cursor.get('error')
                or cursor.get('restore_progress', [])
                or cursor.get('status') not in ('idle', 'restored')):
            sys.exit('Cursor recovery is incomplete; not replacing files. Backups retained.')
    command = {'id': 'install-pause-' + uuid.uuid4().hex, 'action': 'recover'}
    # The mailbox is written first. If any later write is interrupted, a future
    # controller can execute only this safe recovery, never an old enable/profile.
    atomic_json(SUPPORT / 'command.json', command)
    for key in ('exit_after_restore', 'exit_resume_enabled', 'exit_previous_command_id', 'exit_recovery_pending'):
        state.pop(key, None)
    state.update(mode='PAUSED', desired_enabled=False, restore_destination='PAUSED',
                 last_command=command['id'])
    atomic_json(SUPPORT / 'state.json', state)


def main():
    # Preserve the user's persistent login preference when updating the app.
    disabled_output = subprocess.check_output(
        ['/bin/launchctl', 'print-disabled', f'gui/{os.getuid()}'], text=True)
    if not disabled_output.strip().startswith('disabled services = {'):
        sys.exit('Cannot verify the existing login preference; not replacing files.')
    override = re.search(r'^\s*"' + re.escape(LABEL) + r'"\s*=>\s*(disabled|enabled|true|false)\s*$',
                         disabled_output, re.MULTILINE)
    startup_disabled = override is not None and override.group(1) in ('disabled', 'true')
    SUPPORT.mkdir(parents=True, exist_ok=True)
    paused_state()
    SUPPORT.chmod(0o700)
    APP.parent.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    backups = SUPPORT / 'backups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backups.mkdir(parents=True)
    owned = exact_app_pids()
    for label in [*LEGACY, LABEL]:
        path = AGENTS / (label + '.plist')
        if path.exists():
            shutil.copy2(path, backups / path.name)
            subprocess.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
            path.rename(backups / (path.name + '.disabled'))
    for pid in owned:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    for _ in range(50):
        if not exact_app_pids(): break
        time.sleep(.1)
    else:
        sys.exit('Old app is still exiting; retry after recovery. Backups retained.')
    # A supervised child can survive an abrupt supervisor exit. Its PID is only
    # used after validating both the installed script path and event-mode flag.
    record = SUPPORT / 'supervisor-child.json'
    if record.exists():
        pid = int(json.loads(record.read_text())['pid'])
        output = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
        if str(SUPPORT / 'controller.py') in output and '--event-driven' in output:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
    with (SUPPORT / 'controller.lock').open('a') as lock:
        for _ in range(100):
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError: time.sleep(.1)
        else: sys.exit('Controller still owns recovery lock; not replacing files.')
        install_pause_barrier()
        if APP.exists():
            shutil.copytree(APP, backups / APP.name)
            shutil.rmtree(APP)  # Generated app only, with recoverable copy above.
        shutil.copytree(ROOT / 'dist/UUDisplayAutomation.app', APP)
        for name, source in [('controller.py', ROOT / 'controller.py'),
                             ('storage_recovery.py', ROOT / 'storage_recovery.py'),
                             ('cursor_profile.py', ROOT / 'cursor_profile.py'),
                             ('office_cursor_relay.py', ROOT / 'office_cursor_relay.py'),
                             ('session_recheck.py', ROOT / 'session_recheck.py'),
                             ('session_policy.py', ROOT / 'session_policy.py'),
                             ('device_profiles.py', ROOT / 'device_profiles.py'),
                             ('profile_runtime.py', ROOT / 'profile_runtime.py'),
                             ('cursor-image-probe', ROOT / 'dist/cursor-image-probe'),
                             ('office-cursor-relay', ROOT / 'dist/office-cursor-relay'),
                             ('native-topology', ROOT / 'dist/native-topology'),
                             ('betterdisplaycli', ROOT / 'dist/betterdisplaycli')]:
            installed = SUPPORT / name
            if installed.exists(): shutil.copy2(installed, backups / name)
            shutil.copy2(source, installed)
    refresh_app_metadata()
    path = AGENTS / (LABEL + '.plist')
    data = {'Label': LABEL,
            'ProgramArguments': [str(APP / 'Contents/MacOS/UUDisplayAutomation')],
            'AssociatedBundleIdentifiers': ['local.codex.UUDisplayAutomation'],
            'LimitLoadToSessionType': 'Aqua', 'ProcessType': 'Interactive',
            'KeepAlive': {'SuccessfulExit': False}, 'RunAtLoad': True, 'ThrottleInterval': 10,
            'StandardOutPath': str(SUPPORT / 'supervisor.stdout.log'),
            'StandardErrorPath': str(SUPPORT / 'supervisor.stderr.log')}
    path.write_bytes(plistlib.dumps(data))
    if startup_disabled:
        subprocess.run(['/usr/bin/open', '-g', str(APP)], check=True)
    else:
        subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    print('Installed paused with ONE native login service:', path,
          '(login disabled)' if startup_disabled else '(login enabled)')
    print('Backups:', backups)

if __name__ == '__main__': main()
