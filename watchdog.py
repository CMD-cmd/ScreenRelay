"""Independent launchd watchdog. Only restarts this controller if its heartbeat stalls.

Screen restoration is owned by the controller's durable journal on restart.
Two consecutive stale observations separated by 45s avoid killing after Mac sleep.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

DIRECTORY = Path.home() / 'Library/Application Support/UUDisplayAutomation'

def main():
    now = time.time()
    status_path = DIRECTORY / 'status.json'
    marker = DIRECTORY / 'watchdog-stale.json'
    try:
        status = json.loads(status_path.read_text())
        stamp = float(status.get('updated_at', status_path.stat().st_mtime))
    except (OSError, ValueError, TypeError):
        stamp = status_path.stat().st_mtime if status_path.exists() else now
    if now - stamp < 90:
        marker.unlink(missing_ok=True)
        return
    try:
        prior = json.loads(marker.read_text())
    except (OSError, ValueError):
        prior = {}
    if prior.get('stamp') != stamp or now - prior.get('seen_at', now) < 45:
        if prior.get('stamp') != stamp:
            marker.write_text(json.dumps({'stamp': stamp, 'seen_at': now}))
        return
    expected = str(DIRECTORY / 'controller.py')
    rows = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=5).stdout
    for row in rows.splitlines():
        fields = row.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid, cmd = fields
        if expected in cmd and 'python' in cmd and 'watchdog.py' not in cmd:
            # launchd owns restart; journal recovery happens before any new remote entry.
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    marker.unlink(missing_ok=True)

if __name__ == '__main__':
    main()
