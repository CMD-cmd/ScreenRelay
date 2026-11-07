"""Event-driven, opt-in native cursor profiles with durable original recovery.

One controller owns this object. Call sync at existing display events, and close
on shutdown. Helper exit status includes cursor geometry/pixel readback checks.
"""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid
import hashlib
import copy


class CursorCancelled(RuntimeError):
    pass


class CursorProfile:
    NAMES = ('ArrowS', 'IBeamS')
    SCALES = (1.5, 2, 2.5, 3, 3.5, 4)
    RETRY_SECONDS = 5

    def __init__(self, directory, helper=None, clock=time.time, runner=subprocess.run,
                 cancelled=None, monotonic=time.monotonic, restore_budget=8):
        self.directory = Path(directory)
        self.helper = str(helper or Path(__file__).resolve().parent / 'dist' / 'cursor-image-probe')
        self.clock, self.runner = clock, runner
        self.cancelled = cancelled or (lambda: False)
        self.monotonic, self.restore_budget = monotonic, restore_budget
        self.state_path = self.directory / 'cursor-profile-state.json'
        self.config_path = self.directory / 'cursor-profile-config.json'
        self._active = None
        self._catalog_request = None
        self._catalog_profile = None
        self._catalog_error = None
        self._saved_payload = None
        self._load_error = None
        self.state = dict(version=1, status='idle', desired_profile=None,
                          original_profile=None, pending_originals=[], error=None,
                          retry_at=0, diagnostic='', selected_names=[],
                          unsupported_diagnostics=[], restore_progress=[], restore_cursor=0)
        try:
            if self.state_path.exists():
                loaded = json.loads(self.state_path.read_text())
                if not isinstance(loaded, dict) or not isinstance(loaded.get('pending_originals'), list):
                    raise ValueError('Invalid cursor recovery journal')
                self.state.update(loaded)
                # Validate paths before allowing recovery or replacement of a journal.
                root = (self.directory / 'backups' / 'cursor-profile').resolve()
                for record in self.state['pending_originals']:
                    path = Path(record['path']).resolve()
                    if not self._safe_name(record['name']) or root not in path.parents:
                        raise ValueError('Invalid cursor original backup record')
        except Exception as exc:
            self._load_error = str(exc)
            self.state.update(status='error', error=self._load_error)

    def _save(self):
        payload = json.dumps(self.state, indent=2, sort_keys=True) + '\n'
        if payload == self._saved_payload:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.cursor-profile-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
            self._fsync_directory(self.directory)
            self._saved_payload = payload
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _fsync_directory(directory):
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _config(self):
        if not self.config_path.exists():
            return None
        config = json.loads(self.config_path.read_text())
        if not isinstance(config, dict):
            raise ValueError('Cursor profile configuration must be an object')
        if config.get('enabled') is not True:
            return None
        scale, names = config.get('scale'), config.get('names')
        if type(scale) not in (int, float) or scale not in self.SCALES:
            raise ValueError('Cursor scale must be 1.5, 2, 2.5, 3, 3.5 or 4')
        if 'scope' in config:
            if config['scope'] != 'all_supported':
                raise ValueError('Cursor scope must be all_supported')
            return dict(scale=scale, scope='all_supported')
        if (not isinstance(names, list) or not names
                or any(not isinstance(name, str) or name not in self.NAMES for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('Cursor names must be unique ArrowS and/or IBeamS')
        return dict(scale=scale, names=[name for name in self.NAMES if name in names])

    @staticmethod
    def _safe_name(name):
        return isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_]{1,64}', name) is not None

    @staticmethod
    def _identifier(name):
        core = re.fullmatch(r'Core([0-9]+)', name)
        if core:
            number = int(core[1])
            if not 0 <= number <= 43 or name != f'Core{number}':
                raise ValueError('Cursor catalog Core number must be 0 through 43')
            return f'com.apple.cursor.{number}'
        return 'com.apple.coregraphics.' + name

    def _resolve(self, request):
        """Discover once per changed all-supported request, including failures.

        Metadata rejection leaves an already active profile untouched. Discovery
        is never repeated by steady controller events or recovery backoff.
        """
        if request.get('scope') != 'all_supported':
            self._catalog_request = None
            return request
        if self._catalog_request != request:
            self._catalog_request = dict(request)
            self._catalog_profile, self._catalog_error = None, None
            try:
                catalog = json.loads(self._run('--list'))
                if not isinstance(catalog, list):
                    raise ValueError('Cursor catalog must be a JSON list')
                names = []
                for entry in catalog:
                    if not isinstance(entry, dict) or not self._safe_name(entry.get('name')):
                        raise ValueError('Cursor catalog contains an unsafe name')
                    name = entry['name']
                    if entry.get('identifier') != self._identifier(name) or name in names:
                        raise ValueError('Cursor catalog identifier mismatch or duplicate name')
                    for key in ('frames', 'imageCount'):
                        if type(entry.get(key)) is not int or entry[key] <= 0:
                            raise ValueError('Cursor catalog has invalid ' + key)
                    if entry['frames'] > 64:
                        raise ValueError('Cursor catalog frames must not exceed 64')
                    for key in ('duration', 'width', 'height'):
                        value = entry.get(key)
                        if (type(value) not in (int, float) or not math.isfinite(value)
                                or value < 0 or (key != 'duration' and value == 0)):
                            raise ValueError('Cursor catalog has invalid ' + key)
                    if entry['duration'] > 10:
                        raise ValueError('Cursor catalog duration must not exceed 10')
                    names.append(name)
                if not all(name in names for name in self.NAMES):
                    raise ValueError('Cursor catalog must include ArrowS and IBeamS')
                self._catalog_profile = dict(request, names=names)
            except CursorCancelled:
                # Cancellation is a transient mailbox transition, not evidence
                # that this catalog/configuration is invalid.
                self._catalog_request = None
                self._catalog_profile = None
                self._catalog_error = None
                raise
            except Exception as exc:
                self._catalog_error = str(exc)
        if self._catalog_error:
            raise ValueError(self._catalog_error)
        return self._catalog_profile

    def _check_cancelled(self):
        if self.cancelled():
            raise CursorCancelled('Cursor request superseded; restoring originals')

    def _run(self, *arguments, timeout=5):
        if arguments[0] != '--restore':
            self._check_cancelled()
        result = self.runner([self.helper, *map(str, arguments)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, timeout=timeout, check=False)
        output = result.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        self.state['diagnostic'] = output[-2048:]
        if result.returncode != 0:
            raise RuntimeError(f'Cursor helper {arguments[0]} failed ({result.returncode}): {output[-512:]}')
        return output

    def _restore(self):
        """Attempt every original, retaining the complete journal on any failure."""
        deadline = self.monotonic() + self.restore_budget
        self._active = None
        self.state['status'] = 'restoring'
        self._save()
        errors = []
        progress = set(self.state.get('restore_progress', []))
        restored = set()
        deadline_exceeded = False
        originals = self.state['pending_originals']
        count = len(originals)
        cursor = self.state.get('restore_cursor', 0)
        if type(cursor) is not int or cursor < 0:
            cursor = 0
        cursor = cursor % count if count else 0
        next_cursor = cursor
        for offset in range(count):
            index = (cursor + offset) % count
            record = originals[index]
            try:
                path = Path(record['path'])
                if str(path) in progress:
                    continue
                # An interrupted backup may have left only a durable intent.
                if not record.get('captured') and not path.exists():
                    continue
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    errors.append('Cursor restore deadline exceeded; originals remain pending')
                    deadline_exceeded = True
                    break
                # Advance even if this helper fails or exhausts the remaining
                # budget, so later originals get a fair chance on the next event.
                next_cursor = (index + 1) % count
                self._run('--restore', path, timeout=min(5, remaining))
                restored.add(str(path))
            except Exception as exc:
                errors.append(str(exc))
        if errors and self.monotonic() >= deadline:
            deadline_exceeded = True
            if not any('deadline exceeded' in error for error in errors):
                errors.append('Cursor restore deadline exceeded; originals remain pending')
        if errors:
            # Keep every original record, but persist verified progress when the
            # total budget expires so repeated bounded recovery can finish.
            if deadline_exceeded:
                self.state['restore_progress'] = sorted(progress | restored)
            self.state.update(status='recovery_pending', error='; '.join(errors),
                              retry_at=self.clock() + self.RETRY_SECONDS,
                              restore_cursor=next_cursor)
            self._save()
            return False
        self.state.update(status='restored', pending_originals=[], original_profile=None,
                          retry_at=0, error=None, selected_names=[], restore_progress=[], restore_cursor=0)
        self._save()
        return True

    def _apply(self, profile, request):
        folder = self.directory / 'backups' / 'cursor-profile' / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=False)
        self._fsync_directory(folder.parent)
        self._fsync_directory(folder.parent.parent)
        self.state.update(status='backing_up', original_profile=profile,
                          pending_originals=[dict(name=name, path=str((folder / f'{name}.plist').resolve()),
                                                  captured=False) for name in profile['names']],
                          error=None, retry_at=0, restore_progress=[], restore_cursor=0)
        self._save()
        for record in self.state['pending_originals']:
            self._run('--backup', record['path'], self._identifier(record['name']))
            # Make helper-created originals durable before any cursor mutation.
            with open(record['path'], 'rb') as stream:
                os.fsync(stream.fileno())
            self._fsync_directory(folder)
            record['captured'] = True
            self._save()
        self.state['status'] = 'validating'
        self._save()
        for record in self.state['pending_originals']:
            self._check_cancelled()
            self._run('--restore', record['path'])
        self._apply_saved(profile, request)

    def _apply_saved(self, profile, request):
        """Apply from immutable originals, including an active scale-only change."""
        self.state.update(status='applying', original_profile=profile,
                          desired_profile=request, error=None, retry_at=0)
        self._save()
        for record in self.state['pending_originals']:
            self._run('--apply', record['path'], profile['scale'])
        self._check_cancelled()
        self._active = request
        self.state.update(status='active', error=None, selected_names=list(profile['names']),
                          unsupported_diagnostics=(
                              ['Only currently registered, safely restorable system cursors are included; '
                               'conditional unregistered and application-defined cursors are outside this catalog.']
                              if request.get('scope') == 'all_supported' else []))
        self._save()

    def sync(self, active):
        """Synchronize at an existing event; errors never escape to display logic.

        Returns True only while this instance has a verified applied profile.
        Restart recovery returns before a new capture. An active configuration
        change restores and reapplies immediately within this same event.
        """
        if self._load_error:
            return False  # Preserve an unreadable journal for manual recovery.
        try:
            config_error = None
            try:
                profile = self._config()
            except Exception as exc:
                profile, config_error = None, str(exc)
            desired = profile if active else None
            self.state['desired_profile'] = desired
            # Startup/recovery originals always take precedence over discovery.
            if self.state['pending_originals'] and self._active is None:
                if self.clock() < self.state.get('retry_at', 0):
                    self._save()
                    return False
                self._restore()
                return False
            if self._active == desired and desired is not None:
                if self.state.get('error') or self._saved_payload is None:
                    self.state['error'] = None
                    self._save()
                return True
            if config_error:
                if self.state['pending_originals']:
                    if not self._restore():
                        return False
                self.state.update(status='error', error=config_error)
                self._save()
                return False
            if desired is None:
                self._catalog_request = None
                if self.state['pending_originals']:
                    self._restore()
                    return False
                if self.state['status'] != 'idle' or self.state.get('error'):
                    self.state.update(status='idle', error=None, retry_at=0)
                    self._save()
                return False
            if self.clock() < self.state.get('retry_at', 0):
                return False
            # Validate the new catalog before restoring a valid active profile.
            try:
                selected = self._resolve(desired)
            except CursorCancelled:
                raise
            except Exception as exc:
                self.state.update(status='active' if self._active else 'error', error=str(exc))
                self._save()
                return self._active is not None
            originals = self.state['pending_originals']
            if (self._active is not None and self.state['status'] == 'active'
                    and not self.state.get('error') and not self.state.get('restore_progress')
                    and len(originals) == len(selected['names'])
                    and {record['name'] for record in originals} == set(selected['names'])
                    and all(record.get('captured') is True and Path(record['path']).is_file()
                            for record in originals)):
                self._apply_saved(selected, desired)
                return True
            if self.state['pending_originals'] and not self._restore():
                return False
            self._apply(selected, desired)
            return True
        except Exception as exc:
            error = str(exc)
            self._active = None
            try:
                if self.state['pending_originals']:
                    self._restore()
                if self.state['status'] != 'recovery_pending':
                    if isinstance(exc, CursorCancelled):
                        self.state.update(status='restored', error=None, retry_at=0)
                    else:
                        self.state.update(status='error', error=error,
                                          retry_at=self.clock() + self.RETRY_SECONDS)
                else:
                    self.state['error'] = error + '; ' + (self.state.get('error') or '')
                self._save()
            except Exception:
                # Existing durable originals are intentionally retained.
                pass
            return False

    def close(self):
        """Best-effort shutdown restoration; failed originals remain recoverable."""
        if self._load_error:
            return
        try:
            had_desired = self.state.get('desired_profile') is not None
            self.state['desired_profile'] = None
            if self.state['pending_originals']:
                self._restore()
            elif had_desired:
                # Recovery may have finished on the previous event while its
                # desired profile still represented an active request. Persist
                # shutdown intent even when no helper work remains.
                self._save()
        except Exception:
            pass


class CursorProfileWorker:
    """Latest-request mailbox, serviced by one daemon thread without polling.

    The worker is the sole manager owner. Display callbacks only read a small
    config fingerprint and notify the condition; no helper runs on the caller.
    """
    MAX_AUTOMATIC_RECOVERY_BATCHES = 3

    def __init__(self, directory, helper=None, *, factory=CursorProfile,
                 clock=time.time, runner=subprocess.run, monotonic=time.monotonic,
                 join_timeout=10):
        self.directory = Path(directory)
        self.helper, self.factory = helper, factory
        self.clock, self.runner, self.monotonic = clock, runner, monotonic
        self.join_timeout = min(10, max(0, join_timeout))
        self._condition = threading.Condition()
        self._request = None
        self._generation = 0
        self._sequence = 0
        self._working_generation = 0
        self._closing = False
        self._state = {'status': 'starting', 'pending_originals': []}
        self._error = None
        self._thread = threading.Thread(target=self._work, name='cursor-profile', daemon=True)
        self._thread.start()

    def _fingerprint(self):
        try:
            with (self.directory / 'cursor-profile-config.json').open('rb') as stream:
                data = stream.read(65537)
            return hashlib.sha256(data).hexdigest()
        except FileNotFoundError:
            return 'missing'
        except OSError as exc:
            return 'unreadable:' + str(exc)

    def sync(self, active):
        request = (bool(active), self._fingerprint())
        with self._condition:
            if self._closing:
                return
            if request != self._request:
                self._request = request
                self._generation += 1
            self._sequence += 1
            self._condition.notify()

    def _cancelled(self):
        with self._condition:
            return self._closing or self._generation != self._working_generation

    @property
    def state(self):
        with self._condition:
            return copy.deepcopy(self._state)

    def _snapshot(self, manager):
        with self._condition:
            self._state = copy.deepcopy(manager.state)

    def _wait_for_change(self, processed, timeout):
        return self._condition.wait_for(
            lambda: self._closing or self._sequence != processed, timeout=timeout)

    def _work(self):
        manager = None
        try:
            manager = self.factory(self.directory, helper=self.helper, clock=self.clock,
                                   runner=self.runner, cancelled=self._cancelled,
                                   monotonic=self.monotonic)
            # Recovery ignores cancellation and precedes any new application.
            manager.sync(False)
            self._snapshot(manager)
            processed = 0
            budget_generation = 0
            automatic_batches = 0
            while True:
                with self._condition:
                    if budget_generation != self._generation:
                        budget_generation = self._generation
                        automatic_batches = 0
                    timeout = None
                    if (self._state.get('status') == 'recovery_pending'
                            and self._state.get('pending_originals')
                            and automatic_batches < self.MAX_AUTOMATIC_RECOVERY_BATCHES):
                        timeout = max(0, self._state.get('retry_at', 0) - self.clock())
                    external_event = self._wait_for_change(processed, timeout)
                    if self._closing:
                        break
                    if budget_generation != self._generation:
                        budget_generation = self._generation
                        automatic_batches = 0
                    if not external_event:
                        automatic_batches += 1
                    active = self._request[0] if self._request is not None else False
                    processed = self._sequence
                    self._working_generation = self._generation
                had_pending = bool(manager.state.get('pending_originals'))
                manager.sync(active)
                # A recovery-only sync intentionally returns before applying.
                # Drain that final step immediately for the same latest request.
                with self._condition:
                    latest_active = (not self._closing and self._request is not None
                                     and self._request[0]
                                     and self._working_generation == self._generation)
                if (had_pending and latest_active and manager.state.get('status') == 'restored'
                        and not manager.state.get('pending_originals')
                        and manager.state.get('desired_profile') is not None):
                    manager.sync(True)
                self._snapshot(manager)
        except Exception as exc:
            with self._condition:
                self._error = str(exc)
                self._state['worker_error'] = self._error
        finally:
            if manager is not None:
                manager.close()
                self._snapshot(manager)

    def close(self):
        """Request restoration, wait at most ten seconds, and report completion."""
        with self._condition:
            self._closing = True
            self._generation += 1
            self._condition.notify()
        self._thread.join(self.join_timeout)
        stopped = not self._thread.is_alive()
        state = self.state
        failed = bool(self._error) or state.get('status') in (
            'starting', 'error', 'recovery_pending', 'restoring', 'applying', 'backing_up', 'validating')
        return dict(stopped=stopped,
                    restored=stopped and not state.get('pending_originals') and not failed,
                    pending_recovery=not stopped or bool(state.get('pending_originals')) or failed,
                    error=self._error or state.get('error'))
