#!/usr/bin/env python3
"""Disable login automation only after a fresh, acknowledged safe recovery."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from install import atomic_json

SUPPORT = Path.home() / 'Library/Application Support/UUDisplayAutomation'
AGENTS = Path.home() / 'Library/LaunchAgents'
LABEL = 'local.codex.uu-display'
APP = Path.home() / 'Applications/UUDisplayAutomation.app'


def safe_recovery(command_id, requested_at):
    """A stale PAUSED status cannot authorize stopping a recovery executor."""
    try:
        state = json.loads((SUPPORT / 'state.json').read_text())
        status = json.loads((SUPPORT / 'status.json').read_text())
        command = json.loads((SUPPORT / 'command.json').read_text())
        if not all(isinstance(value, dict) for value in (state, status, command)):
            return False
        now = time.time()
        stamp = status.get('updated_at', status.get('timestamp'))
        if (type(stamp) not in (int, float) or not requested_at <= stamp <= now
                or now - stamp > 5 or status.get('recovery_pending') is not False
                or state.get('mode') != 'PAUSED' or state.get('journal')
                or state.get('desired_enabled') is not False
                or state.get('last_command') != command_id
                or any(status.get(key) != value for key, value in state.items())
                or command.get('id') != command_id or command.get('action') != 'recover'
                or command.get('exit_after_restore') is not True):
            return False
        completed = state.get('exit_completed')
        if not isinstance(completed, dict) or completed.get('id') != command_id:
            return False
        stamp = completed.get('timestamp')
        pid = completed.get('controller_pid')
        if (type(stamp) not in (int, float) or not requested_at <= stamp <= now
                or now - stamp > 5 or type(pid) is not int or pid <= 0
                or status.get('controller_pid') != pid):
            return False
        cursor_path = SUPPORT / 'cursor-profile-state.json'
        if cursor_path.exists():
            cursor = json.loads(cursor_path.read_text())
            if (not isinstance(cursor, dict) or cursor.get('pending_originals') != []
                    or cursor.get('original_profile') is not None or cursor.get('error')
                    or cursor.get('restore_progress', [])
                    or cursor.get('status') not in ('idle', 'restored')):
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def main():
    requested_at = time.time()
    command_id = 'uninstall-recover-' + uuid.uuid4().hex
    atomic_json(SUPPORT / 'command.json', {'id': command_id, 'action': 'recover', 'exit_after_restore': True})
    try:
        fd = os.open(str(SUPPORT / 'wake.fifo'), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b'!')
        finally:
            os.close(fd)
    except OSError:
        pass
    try:
        subprocess.run([str(APP / 'Contents/MacOS/UUDisplayAutomation'),
                        '--request-quit', command_id], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # A surviving controller can consume the durable command directly.
    for _ in range(30):
        if safe_recovery(command_id, requested_at):
            break
        time.sleep(1)
    else:
        sys.exit('Recovery is not freshly confirmed; leaving recovery services running.')
    # Never kill a controller to obtain its recovery lock. A service that has
    # not handed the lock back remains the sole authorized recovery executor.
    with (SUPPORT / 'controller.lock').open('a') as lock:
        for _ in range(50):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.1)
        else:
            sys.exit('Controller still owns recovery lock; leaving services running.')
        if not safe_recovery(command_id, requested_at):
            sys.exit('Recovery changed before lock handoff; leaving services running.')
        for label in [LABEL, *[LABEL + '-' + name for name in ('detector', 'watchdog', 'controller')]]:
            path = AGENTS / (label + '.plist')
            result = subprocess.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
            if path.exists() and result.returncode != 0:
                sys.exit('Could not stop a login service; its registration and recovery data are retained.')
            if path.exists():
                path.rename(SUPPORT / (path.name + '.disabled'))
        subprocess.run(['/usr/bin/pkill', '-x', 'UUDisplayAutomation'], capture_output=True)
    print('Login automation removed; app, recovery data and backups preserved.')


if __name__ == '__main__':
    main()
