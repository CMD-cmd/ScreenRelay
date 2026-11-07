"""Bounded synchronous AX verification driven by the controller's event loop."""
import copy
import json
import math
import os
from pathlib import Path
import subprocess
import time


class SessionRecheck:
    REASONS = frozenset(('uu_windows_unavailable', 'ax_snapshot_incomplete',
                         'uu_main_content_unavailable'))
    FIELDS = ('state', 'trusted', 'overlay', 'controllers', 'reason', 'sleeping', 'timestamp')
    OPTIONAL_FIELDS = ('activeControllerCount', 'axCachedWindows', 'uuPID',
                       'session_observed_at', 'ui_hidden', 'ui_minimized',
                       'ui_observable', 'session_negative_verified')
    RETRY_DELAYS = (.75, 2.0)

    def __init__(self, directory, executable, clock=time.time, runner=subprocess.run):
        self.directory = Path(directory)
        self.executable = str(executable)
        self.clock = clock
        self.runner = runner
        self._clear()

    def _clear(self):
        self._key = None
        self._attempts = 0
        self._deadline = None
        self._verified = None
        self._verified_at = None
        self._succeeded = False
        self._active = False

    @staticmethod
    def _fresh(timestamp, now, limit):
        try:
            return (isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
                    and math.isfinite(timestamp) and 0 <= now - timestamp < limit)
        except (OverflowError, ValueError):
            return False

    def next_timeout(self):
        """Seconds until a pending single retry; no periodic wakeups are created."""
        if not self._active or self._deadline is None:
            return None
        return max(0.0, self._deadline - self.clock())

    def _diagnostic(self, result, error=None):
        # No AX contents, controller names, command output, or exception text.
        payload = {'timestamp': self.clock(), 'attempts': self._attempts,
                   'last_result': result, 'last_error': error}
        temporary = self.directory / 'session-recheck.json.tmp'
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with temporary.open('w') as output:
                json.dump(payload, output, allow_nan=False)
            os.replace(temporary, self.directory / 'session-recheck.json')
        except (OSError, ValueError):
            # Diagnostics must never prevent fail-safe engine handling.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _probe(self):
        self._attempts += 1
        failure = None
        value = None
        try:
            result = self.runner([self.executable, '--probe'], timeout=3,
                                 check=False, capture_output=True, text=True)
            if result.returncode != 0:
                failure = 'nonzero_exit'
            else:
                candidate = json.loads(result.stdout)
                if (not isinstance(candidate, dict)
                        or candidate.get('state') not in ('connected', 'disconnected')
                        or candidate.get('trusted') is not True
                        or candidate.get('sleeping') is not False
                        or not isinstance(candidate.get('overlay'), bool)
                        or not isinstance(candidate.get('controllers'), list)
                        or any(not isinstance(name, str) for name in candidate['controllers'])
                        or not isinstance(candidate.get('reason'), str)
                        or not self._fresh(candidate.get('timestamp'), self.clock(), 3)):
                    failure = 'invalid_signal'
                else:
                    value = candidate
        except subprocess.TimeoutExpired:
            failure = 'timeout'
        except (ValueError, TypeError):
            failure = 'invalid_json'
        except Exception:
            failure = 'probe_error'
        if value is not None:
            self._verified = copy.deepcopy(value)
            self._verified_at = self.clock()
            self._succeeded = True
            self._deadline = None
        elif self._attempts < 3:
            self._deadline = self.clock() + self.RETRY_DELAYS[self._attempts - 1]
        else:
            self._deadline = None
        self._diagnostic('verified' if value is not None else 'failed', failure)

    def _merge(self, original):
        value = copy.deepcopy(original)
        for field in self.FIELDS:
            value[field] = copy.deepcopy(self._verified[field])
        for field in self.OPTIONAL_FIELDS:
            if field in self._verified:
                value[field] = copy.deepcopy(self._verified[field])
            else:
                value.pop(field, None)
        value['native_timestamp'] = original.get('timestamp')
        value['event'] = 'session'
        value['probePID'] = self._verified.get('detector_pid')
        value['session_verification'] = {
            'attempts': self._attempts, 'verified_at': self._verified_at,
            'probe_timestamp': self._verified['timestamp']}
        return value

    def resolve(self, original):
        if original.get('state') in ('connected', 'disconnected'):
            self._clear()
            return original
        key = (original.get('run_id'), original.get('detector_pid'),
               original.get('uuPID'), original.get('reason'))
        if key != self._key:
            self._clear()
            self._key = key
        eligible = (original.get('state') == 'unknown'
                    and original.get('trusted') is True
                    and original.get('sleeping') is False
                    and self._fresh(original.get('timestamp'), self.clock(), 90)
                    and isinstance(original.get('reason'), str)
                    and original.get('reason') in self.REASONS)
        self._active = eligible
        if not eligible:
            self._verified = None
            return original
        if self._verified is not None:
            if self._fresh(self._verified['timestamp'], self.clock(), 90):
                return self._merge(original)
            self._verified = None
        if (not self._succeeded and self._attempts < 3
                and (self._deadline is None or self.clock() >= self._deadline)):
            self._probe()
        if not self._fresh(original.get('timestamp'), self.clock(), 90):
            self._active = False
            self._verified = None
            return original
        if self._verified is not None:
            return self._merge(original)
        return original

    def refresh_for_command(self, original):
        """One bounded read on explicit user action, never a periodic poll."""
        if (original.get('trusted') is not True or original.get('sleeping') is not False
                or not self._fresh(original.get('timestamp'), self.clock(), 90)):
            return original
        self._clear()
        self._probe()
        self._active = False
        self._deadline = None
        return self._merge(original) if self._verified is not None else original
