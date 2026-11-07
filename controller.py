#!/usr/bin/env python3
"""Fail-safe BetterDisplay controller. No third party Python dependencies.

Adapter contract: inventory() returns [{identity, kind, name, connected, main,
resolution, hiDPI, rotation, placement, refreshRate, ...}]; set(display, **fields)
uses only the supplied exact device. Engine.tick takes the detector JSON and an
optional {id, action} command. The durable journal always precedes mutations.
"""
import argparse
import errno
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
import re
import selectors
import stat
from pathlib import Path
import signal as signals
import subprocess
import sys
import traceback
import time
import uuid

from storage_recovery import StorageRecovery, is_storage_error

DEFAULT_DIRECTORY = Path.home() / 'Library/Application Support/UUDisplayAutomation'
BINARY = next((str(path) for path in (DEFAULT_DIRECTORY / 'betterdisplaycli',
                                    Path(__file__).resolve().parent / 'dist/betterdisplaycli',
                                    Path('/opt/homebrew/bin/betterdisplaycli'),
                                    Path('/usr/local/bin/betterdisplaycli')) if path.is_file()),
              '/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay')
_runtime_directory = DEFAULT_DIRECTORY
_runtime_logging = False
TARGET_NAME = 'iPad Pro 13'
TARGET_UUID = '00000000-0000-4000-8000-000000000001'
TARGET_ID = 'virtual:2198:55152:1000000001'
MODE = {'resolution': '1376x1032', 'hiDPI': 'on', 'refreshRate': '60'}
DISCONNECT_GRACE = 3
FIELDS = ('resolution', 'hiDPI', 'rotation', 'placement', 'refreshRate')
RUNTIME_LOG_BYTES = 2 * 1024 * 1024
RUNTIME_EVENT_BYTES = 16 * 1024


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_event(directory, event, **fields):
    """Keep two bounded diagnostic files without masking an original failure.

    Rotation happens only when appending an event. Recovery records, backups and
    evidence directories are never touched; a busy diagnostic writer is skipped.
    """
    lock_fd = fd = None
    try:
        path = Path(directory) / 'runtime-events.jsonl'
        record = {'timestamp': time.time(), 'event': event, 'pid': os.getpid(), **fields}
        payload = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        if len(payload) > RUNTIME_EVENT_BYTES:
            record = {key: record[key] for key in ('timestamp', 'event', 'pid')}
            record.update(fields_omitted=True, original_bytes=len(payload))
            payload = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        if len(payload) > min(RUNTIME_EVENT_BYTES, RUNTIME_LOG_BYTES):
            return False

        def owned_regular(info):
            return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and info.st_nlink == 1)

        flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK
        lock_fd = os.open(str(path.with_name('.runtime-events.lock')), flags, 0o600)
        if not owned_regular(os.fstat(lock_fd)):
            return False
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = os.open(str(path), flags | os.O_APPEND, 0o600)
        info = os.fstat(fd)
        if not owned_regular(info):
            return False
        if info.st_size + len(payload) > RUNTIME_LOG_BYTES:
            previous = path.with_name('runtime-events.jsonl.1')
            try:
                previous_info = os.lstat(previous)
            except FileNotFoundError:
                previous_info = None
            if previous_info is not None and not owned_regular(previous_info):
                return False
            os.close(fd)
            fd = None
            os.replace(path, previous)
            fd = os.open(str(path), flags | os.O_EXCL | os.O_APPEND, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError('Unable to append runtime event')
            offset += written
        os.fsync(fd)
        return True
    except Exception:
        return False
    finally:
        for descriptor in (fd, lock_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def truth(value):
    return str(value).strip().lower() in ('on', 'true', '1', 'yes')


def parse_bool(output):
    values = [value.strip().lower() for value in output.split(',')]
    converted = []
    for value in values:
        if value in ('on', 'true', '1', 'yes'):
            converted.append(True)
        elif value in ('off', 'false', '0', 'no'):
            converted.append(False)
        else:
            raise RuntimeError('Invalid BetterDisplay boolean: ' + output)
    if len(set(converted)) != 1:
        raise RuntimeError('Conflicting BetterDisplay boolean: ' + output)
    return converted[0]


def parse_identifiers(output):
    output = output.strip()
    if not output:
        raise RuntimeError('BetterDisplay returned an empty inventory')
    value = json.loads(output if output.startswith('[') else '[' + output + ']')
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise RuntimeError('Invalid BetterDisplay inventory')
    return value


class BetterDisplay:
    def __init__(self, binary=BINARY, dry_run=False, native_helper=None):
        self.native_helper = native_helper
        self.binary = binary
        self.dry_run = dry_run
        self.operations = []
        self.metrics = {"inventory_calls": 0, "CLI_calls": 0}
        self._identifier_cache = None
        self._topology_key = None

    def _run(self, *args):
        self.metrics["CLI_calls"] += 1
        started = time.monotonic()
        failure = None
        try:
            result = subprocess.run([self.binary, *args], capture_output=True, text=True,
                                    timeout=5, check=True)
            text = result.stdout.strip()
            if text.lower().startswith(('error', 'failed')):
                raise RuntimeError(text)
            return text
        except BaseException as exc:
            failure = str(exc)
            raise
        finally:
            if _runtime_logging:
                runtime_event(_runtime_directory, 'CLI_complete', arguments=list(args),
                              duration_seconds=time.monotonic()-started,
                              error=failure)

    def protections(self):
        groups = []
        for item in parse_identifiers(self._run('get', '-identifiers')):
            if item.get('deviceType') != 'DisplayGroup':
                continue
            tag = item.get('tagID')
            if not tag:
                raise RuntimeError('Display group without exact selector')
            enabled = parse_bool(self._run('get', '-type=DisplayGroup',
                                          '-tagID=' + str(tag), '-layoutProtection'))
            groups.append({'tag': str(tag), 'name': item.get('name'), 'enabled': enabled})
        return groups

    def set_protection(self, group, enabled):
        if not group.get('tag'):
            raise RuntimeError('Refusing unscoped layout protection change')
        if self.dry_run:
            raise RuntimeError('Dry run refuses protection mutations')
        self._run('set', '-type=DisplayGroup', '-tagID=' + str(group['tag']),
                  '-layoutProtection=' + ('on' if enabled else 'off'))

    @staticmethod
    def mode_writer_from_log(output, display_id, mode_id):
        writer = None
        for line in output.splitlines():
            match = re.search(r'Display 0x([0-9a-fA-F]+): kCGSDisplayConfigMode ([0-9]+)', line)
            if not match or int(match.group(1), 16) != display_id or int(match.group(2)) != mode_id:
                continue
            writer = ('UURemoteServer' if 'UURemoteServer[' in line else
                      'BetterDisplay' if 'BetterDisplay[' in line else 'other')
        return writer

    def latest_mode_writer(self, target):
        display_id, mode_id = target.get('native_display_id'), target.get('mode_identifier')
        if not isinstance(display_id, int) or not isinstance(mode_id, int):
            return None
        predicate = ('eventMessage CONTAINS "kCGSDisplayConfigMode" AND '
                     f'eventMessage CONTAINS "Display 0x{display_id:08x}:"')
        try:
            result = subprocess.run(['/usr/bin/log', 'show', '--last', '10s', '--style', 'compact',
                                     '--info', '--predicate', predicate], capture_output=True, text=True,
                                    timeout=3, check=True)
            return self.mode_writer_from_log(result.stdout, display_id, mode_id)
        except (OSError, subprocess.SubprocessError):
            return None

    def _system_inventory(self):
        result = subprocess.run(['/usr/sbin/system_profiler', 'SPDisplaysDataType', '-json'],
                                capture_output=True, text=True, check=True, timeout=8)
        report = json.loads(result.stdout)
        displays = {}
        for gpu in report.get('SPDisplaysDataType', []):
            for device in gpu.get('spdisplays_ndrvs', []):
                identifier = str(device.get('_spdisplays_displayID', ''))
                if identifier:
                    displays[str(int(identifier, 16))] = device
        if not displays:
            raise RuntimeError('System display inventory unavailable')
        return displays

    def _native_topology(self):
        candidates = ([Path(self.native_helper)] if self.native_helper else
                      [DEFAULT_DIRECTORY / 'native-topology',
                       Path(__file__).resolve().parent / 'dist/native-topology'])
        helper = next((path for path in candidates if path.is_file()), None)
        if helper is None:
            return None  # Legacy/test-only fallback when helper has not been installed.
        result = subprocess.run([str(helper)], capture_output=True, text=True,
                                check=True, timeout=3)
        values = json.loads(result.stdout)
        if not isinstance(values, list):
            raise RuntimeError('Invalid native topology output')
        return values

    def _native_origins(self):
        values = self._native_topology()
        if values is None:
            return None
        return {str(int(v['displayID'])): str(int(v['originX'])) + 'x' + str(int(v['originY']))
                for v in values}

    def _virtual_mode_number(self, device, fields):
        # Profile hot-switch preflight uses this virtual-only interface.
        if device.get('kind') == 'VirtualScreen':
            return self._mode_number(device, fields)
        return None

    def _mode_number(self, device, fields):
        virtual = device.get('kind') == 'VirtualScreen'
        if (device.get('kind') not in ('Display', 'VirtualScreen') or
                not any(key in fields for key in (('resolution', 'hiDPI') if virtual else
                                                 ('resolution', 'hiDPI', 'refreshRate'))) or
                not isinstance(device.get('native_display_id'), int)):
            return None
        if not virtual and (str(device.get('rotation')) != '0' or
                            str(fields.get('rotation', '0')) != '0'):
            return None  # Rotation still uses BetterDisplay's ordinary selectors.
        candidates = ([Path(self.native_helper)] if self.native_helper else
                      [DEFAULT_DIRECTORY / 'native-topology', Path(__file__).resolve().parent / 'dist/native-topology'])
        helper = next((path for path in candidates if path.is_file()), None)
        if helper is None:
            return None
        result = subprocess.run([str(helper), '--modes', str(device['native_display_id'])],
                                capture_output=True, text=True, check=True, timeout=3)
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('displayID') != device['native_display_id']:
            raise RuntimeError('Native mode enumeration did not identify the requested output')
        fingerprint = 'virtual:' + ':'.join(str(value.get(k, '')) for k in ('vendor', 'model', 'serial'))
        identities = (fingerprint, value.get('uuid')) if virtual else (value.get('uuid'),)
        if device['identity'] not in identities:
            raise RuntimeError('Native mode enumeration identity changed')
        resolution = fields.get('resolution', device.get('resolution', ''))
        if not Engine._valid_resolution(resolution):
            raise RuntimeError('Invalid requested display resolution')
        width, height = map(int, resolution.split('x'))
        scale = 2 if truth(fields.get('hiDPI', device.get('hiDPI'))) else 1
        rate = float(str(fields.get('refreshRate') or device.get('refreshRate') or 60).removesuffix('Hz'))
        modes = [mode for mode in value.get('modes', []) if
                 mode.get('width') == width and mode.get('height') == height and
                 mode.get('pixelWidth') == width * scale and mode.get('pixelHeight') == height * scale and
                 isinstance(mode.get('refreshRate'), (int, float)) and
                 ((mode['refreshRate'] == 0 or abs(mode['refreshRate'] - rate) < .1) if virtual else
                  abs(mode['refreshRate'] - rate) < .001) and
                 type(mode.get('modeID')) is int]
        if not modes:
            if not virtual:
                return None
            raise RuntimeError('Requested HiDPI mode is not available on the exact virtual output')
        return min(mode['modeID'] for mode in modes)

    def _native_inventory(self, values):
        key = tuple(sorted((str(v['displayID']), str(v.get('uuid'))) for v in values))
        if self._identifier_cache is None or key != self._topology_key:
            self._identifier_cache = parse_identifiers(self._run('get', '-identifiers'))
            self._topology_key = key
        # BetterDisplay can briefly retain displayID=0 or the previous ID after
        # a connection change. Match live outputs by durable identity instead.
        actuals = {}
        virtual_actuals = {}
        for value in values:
            native_uuid = str(value.get('uuid', '')).upper()
            if native_uuid in actuals:
                raise RuntimeError('Ambiguous native display UUID')
            actuals[native_uuid] = value
            if all(k in value for k in ('vendor', 'model', 'serial')):
                fingerprint = 'virtual:' + ':'.join(str(value[k]) for k in ('vendor', 'model', 'serial'))
                virtual_actuals.setdefault(fingerprint, []).append(value)
        matched = set()
        devices = []
        for item in self._identifier_cache:
            kind = item.get('deviceType')
            if kind not in ('Display', 'VirtualScreen'):
                continue
            if not item.get('tagID'):
                raise RuntimeError('Display without an exact selector')
            identity = item.get('UUID')
            if kind == 'VirtualScreen':
                identity = 'virtual:' + ':'.join(str(item.get(k, '')) for k in ('vendor', 'model', 'serial'))
            elif not identity or identity == 'UNKNOWN':
                raise RuntimeError('Physical display without durable UUID')
            if kind == 'VirtualScreen':
                candidates = virtual_actuals.get(identity, [])
                if len(candidates) > 1:
                    raise RuntimeError('Ambiguous native virtual display fingerprint')
                actual = candidates[0] if candidates else None
            else:
                actual = actuals.get(str(identity).upper())
            if actual and kind == 'VirtualScreen' and (
                    not item.get('tagID (Display)') or
                    str(item['tagID (Display)']) == str(item['tagID'])):
                # WindowServer can publish the output before BetterDisplay
                # publishes its associated Display selector. Refresh even if
                # the native topology no longer changes; VirtualScreen cannot
                # receive Display-only mode/main operations.
                self._identifier_cache = None
                raise RuntimeError('Virtual display linked selector is not ready')
            if actual:
                if str(actual['displayID']) in matched:
                    raise RuntimeError('Multiple BetterDisplay identities map to one native output')
                matched.add(str(actual['displayID']))
            device = dict(identity=identity, kind=kind, name=item.get('name'), tag=str(item['tagID']),
                          mode_tag=str(item.get('tagID (Display)', item['tagID'])), connected=bool(actual),
                          main=False, mirrored=False, mode_source='native-topology',
                          **{field: None for field in FIELDS})
            if actual:
                width, height = int(actual['width']), int(actual['height'])
                pw, ph = int(actual['pixelWidth']), int(actual['pixelHeight'])
                if min(width, height, pw, ph) <= 0 or abs(pw / width - ph / height) > .05:
                    raise RuntimeError('Invalid native display dimensions/scaling')
                rate = float(actual.get('refreshRate', 0))
                device.update(resolution=f'{width}x{height}', hiDPI='on' if pw / width > 1.05 else 'off',
                              native_display_id=int(actual['displayID']),
                              rotation=format(float(actual['rotation']), '.6g'),
                              placement=f"{int(actual['originX'])}x{int(actual['originY'])}",
                              placement_source='native-topology', main=bool(actual['main']),
                              mirrored=bool(actual.get('mirrored', False)),
                              refreshRate=format(rate, '.6g') if rate > 0 else None,
                              refreshRate_unreported_or_adaptive=rate <= 0,
                              mode_identifier=actual.get('mode_identifier'))
            devices.append(device)
        if matched != {str(v['displayID']) for v in values}:
            self._identifier_cache = None
            raise RuntimeError('Native/BetterDisplay UUID mismatch or unrecognized online display; refreshing selectors')
        return devices

    def inventory(self):
        self.metrics['inventory_calls'] += 1
        native = self._native_topology()
        if native is not None:
            return self._native_inventory(native)
        items = parse_identifiers(self._run('get', '-identifiers'))
        system = self._system_inventory()
        native_origins = self._native_origins()
        devices = []
        for item in items:
            kind = item.get('deviceType')
            if kind not in ('Display', 'VirtualScreen'):
                continue
            tag = item.get('tagID')
            if not tag:
                raise RuntimeError('Display without an exact selector')
            mode_tag = item.get('tagID (Display)', tag)
            identity = item.get('UUID')
            if kind == 'VirtualScreen':
                identity = 'virtual:' + ':'.join(str(item.get(k, '')) for k in
                                               ('vendor', 'model', 'serial'))
            elif not identity or identity == 'UNKNOWN':
                raise RuntimeError('Physical display without durable UUID')
            actual = system.get(str(item.get('displayID', '')))
            online = bool(actual and actual.get('spdisplays_online') == 'spdisplays_yes')
            device = {'identity': identity, 'kind': kind, 'name': item.get('name'),
                      'tag': str(tag), 'mode_tag': str(mode_tag), 'connected': online,
                      'main': False, 'mirrored': False, 'mode_source': 'system_profiler',
                      **{field: None for field in FIELDS}}
            if online:
                mode = re.search(r'(\d+)\s*x\s*(\d+)', actual.get('spdisplays_resolution', ''))
                rate = re.search(r'@\s*([0-9.]+)\s*Hz', actual.get('spdisplays_resolution', ''))
                refresh = float(rate.group(1)) if rate else 0.0
                pixels = re.search(r'(\d+)\s*x\s*(\d+)', actual.get('_spdisplays_pixels', ''))
                if not mode or not pixels:
                    raise RuntimeError('Actual system display mode unavailable: ' + str(device['name']))
                width, height = int(mode.group(1)), int(mode.group(2))
                pixel_width, pixel_height = int(pixels.group(1)), int(pixels.group(2))
                if min(width, height, pixel_width, pixel_height) <= 0:
                    raise RuntimeError('Invalid system display dimensions')
                scale_x, scale_y = pixel_width / width, pixel_height / height
                if abs(scale_x - scale_y) > 0.05:
                    raise RuntimeError('Inconsistent system display scaling')
                rotation = actual.get('spdisplays_rotation', 'spdisplays_supported')
                rotation = str(rotation) if str(rotation) in ('0', '90', '180', '270') else '0'
                if native_origins is None:
                    placement = self._run('get', '-tagID=' + str(mode_tag), '-placement')
                    device['placement_source'] = 'BetterDisplay (legacy helper unavailable)'
                else:
                    placement = native_origins.get(str(item.get('displayID', '')))
                    if placement is None:
                        raise RuntimeError('Native display origin not yet available: ' + str(device['name']))
                    device['placement_source'] = 'native-topology'
                if not re.fullmatch(r'-?\d+x-?\d+', placement):
                    raise RuntimeError('Invalid display placement: ' + placement)
                device.update(resolution=str(width) + 'x' + str(height),
                              hiDPI='on' if scale_x > 1.05 else 'off', rotation=rotation,
                              placement=placement, refreshRate=format(refresh, '.6g') if refresh > 0 else None,
                              refreshRate_unreported_or_adaptive=refresh <= 0,
                              main=actual.get('spdisplays_main') == 'spdisplays_yes',
                              mirrored=actual.get('spdisplays_mirror') == 'spdisplays_on')
            devices.append(device)
        return devices

    def set(self, device, **fields):
        if not device.get('tag') or not device.get('identity'):
            raise RuntimeError('Refusing an unscoped display mutation')
        self.operations.append((device['identity'], dict(fields)))
        if 'connected' in fields:
            self._identifier_cache = None
        if self.dry_run:
            raise RuntimeError('Dry run refuses display mutations')
        # Connected uses the virtual controller; modes use its linked Display.
        groups = []
        if 'connected' in fields:
            groups.append((device['tag'], {'connected': fields['connected']}))
        remaining = {k: v for k, v in fields.items() if k != 'connected'}
        if remaining:
            if device.get('kind') == 'VirtualScreen' and (
                    not device.get('mode_tag') or str(device['mode_tag']) == str(device['tag'])):
                raise RuntimeError('Refusing Display operation without linked virtual selector')
            mode_number = self._mode_number(device, remaining)
            if mode_number is not None:
                # Select an identity-checked current mode, including ProMotion
                # modes whose nominal 120Hz is not a fixed-rate CLI option.
                remaining = {k: v for k, v in remaining.items() if k not in ('resolution', 'hiDPI', 'refreshRate')}
                remaining['displayModeNumber'] = mode_number
            groups.append((device.get('mode_tag', device['tag']), remaining))
        for tag, values in groups:
            args = ['set', '-tagID=' + str(tag)]
            for key, value in values.items():
                if key not in (*FIELDS, 'connected', 'main', 'displayModeNumber'):
                    raise ValueError('Unsupported display field')
                args.append('-' + key + '=' + ('on' if value is True else 'off' if value is False else str(value)))
            self._run(*args)

    def set_connections(self, devices, connected):
        # Independent exact physical selectors can be submitted together so
        # BetterDisplay can coalesce configuration passes. No global selector.
        if (not isinstance(connected, bool) or
                any(d.get('kind') != 'Display' or not d.get('tag') or not d.get('identity') for d in devices) or
                len({d['identity'] for d in devices}) != len(devices) or
                len({d['tag'] for d in devices}) != len(devices)):
            raise RuntimeError('Invalid exact physical connection batch')
        if not devices:
            return
        with ThreadPoolExecutor(max_workers=min(4, len(devices))) as pool:
            list(pool.map(lambda d: self.set(d, connected=connected), devices))

    def discard_virtual(self, original):
        """Delete only an exact, offline temporary definition, with output safe."""
        if original.get('kind') != 'VirtualScreen':
            raise RuntimeError('Temporary cleanup requires an owned virtual identity')
        devices = self.inventory()
        matches = [d for d in devices if d['identity'] == original['identity']]
        if not matches:
            return
        if (len(matches) != 1 or matches[0]['kind'] != 'VirtualScreen'
                or matches[0]['name'] != original['name'] or matches[0]['connected']):
            raise RuntimeError('Temporary virtual identity changed, ambiguous, or still online')
        if not any(d['connected'] and not d.get('mirrored') for d in devices):
            raise RuntimeError('No safe output before temporary virtual cleanup')
        if self.dry_run:
            raise RuntimeError('Dry run refuses virtual deletion')
        self.operations.append((original['identity'], {'discard': True}))
        try:
            self._run('discard', '-type=VirtualScreen', '-tagID=' + matches[0]['tag'])
        finally:
            # Deletion can succeed before its response is lost. A retry must
            # not keep addressing a cached selector for an absent definition.
            self._identifier_cache = None


class TransitionInterrupted(RuntimeError):
    def __init__(self, reason, destination='ARMED', category='detector_unavailable'):
        super().__init__(reason)
        self.destination, self.category = destination, category


class Engine:
    def __init__(self, bd, directory=DEFAULT_DIRECTORY, clock=time.time, sleeper=time.sleep, monotonic=time.monotonic, signal_max_age=5, event_driven=False, persist_initial=True):
        self.sleeper, self.monotonic = sleeper, monotonic
        self.signal_max_age, self.event_driven = signal_max_age, event_driven
        self.last_event_id = None
        self.last_detector_state = None
        self.metrics = {"inventory_calls": 0, "transitions": []}
        self.waiting_for = None
        self.entering = False
        self.storage_recovery = None
        self.pending_resolution = None
        self.initial_mode_guard_until = 0
        self.initial_uu_mode_corrected = False
        self.bd, self.directory, self.clock = bd, Path(directory), clock
        self.directory.mkdir(parents=True, exist_ok=True)
        saved = read_json(self.directory / 'state.json', {})
        if not isinstance(saved, dict):
            raise RuntimeError('Invalid state.json; refusing to overwrite recovery evidence')
        self.state = {'version': 1, 'mode': 'DISABLED', 'journal': None,
                      'last_command': None, 'last_error': None, 'detached': {},
                      'desired_enabled': False, 'restore_destination': 'PAUSED',
                      'interrupted_restarts': [], **saved}
        # A completion state can land before its status copy. Do not interpret
        # that split write as permission to enter another remote session.
        try:
            exit_command = read_json(self.directory / 'command.json', {})
        except (ValueError, OSError):
            exit_command = {}
        try:
            published = read_json(self.directory / 'status.json', {})
        except (ValueError, OSError):
            published = {}
        if (self._valid_command(exit_command) and exit_command.get('exit_after_restore') is True
                and exit_command['action'] in ('pause', 'recover')
                and exit_command['id'] == self.state['last_command']
                and self.state.get('settled_exit_command') != exit_command['id']
                and not self.state.get('exit_after_restore')):
            completed = self.state.get('exit_completed')
            paired = (isinstance(completed, dict) and isinstance(published, dict)
                      and completed.get('id') == exit_command['id']
                      and published.get('exit_completed') == completed
                      and published.get('controller_pid') == completed.get('controller_pid')
                      and published.get('last_command') == exit_command['id']
                      and published.get('mode') == self.state['mode']
                      and published.get('desired_enabled') == self.state['desired_enabled']
                      and not self.state.get('journal') and not published.get('journal'))
            if paired:
                self.state['settled_exit_command'] = exit_command['id']
            else:
                resume = (isinstance(completed, dict) and completed.get('id') == exit_command['id']
                          and completed.get('resume_enabled') is True)
                self.state.update(exit_after_restore=exit_command['id'], exit_resume_enabled=resume,
                                  desired_enabled=False, restore_destination='PAUSED')
                if not self.state.get('journal'):
                    self.state['mode'] = 'PAUSED'
        previous_metrics = saved.get('metrics', {})
        self.metrics.update(previous_metrics)
        if hasattr(self.bd, 'metrics'):
            self.bd.metrics.update({k: previous_metrics.get(k, 0) for k in self.bd.metrics})
        self.last_good = self.clock()
        self.last_tick = None
        self.disconnected_since = None
        self.retry_at = 0
        self.restore_attempts = 0
        self.after_restore = 'PAUSED'
        failure_already_counted = self.state.pop('failure_recorded', False) is True
        if self.state.get('journal'):
            now = self.clock()
            history = [stamp for stamp in self.state.get('interrupted_restarts', [])
                       if isinstance(stamp, (int, float)) and 0 <= now - stamp <= 300]
            if not failure_already_counted:
                history.append(now)
            self.state['interrupted_restarts'] = history
            if len(history) >= 3:
                self.state['desired_enabled'] = False
                self.state['exit_resume_enabled'] = False
                self.state['restore_destination'] = 'PAUSED'
                reason = 'Interrupted restart budget reached; restoring physical displays then pausing'
                self.state['recovery_context'] = dict(category='restart_budget', reason=reason,
                                                      automatic_resume=False, timestamp=now)
            else:
                reason = 'Interrupted session: recovering physical displays before activation'
            if self.state['desired_enabled'] and self.state['restore_destination'] == 'ARMED':
                self.after_restore = 'ARMED'
            self.state['journal']['restore_destination'] = self.after_restore
            self.state['mode'] = 'RESTORING'
            self.state['last_error'] = reason
            runtime_event(self.directory, 'interrupted_recovery', destination=self.after_restore,
                          restart_count=len(history), reason=reason)
        if persist_initial:
            self._save()

    def _save(self):
        self.state['metrics'] = {**self.metrics, **getattr(self.bd, 'metrics', {})}
        atomic_json(self.directory / 'state.json', self.state)
        atomic_json(self.directory / 'status.json', self.status())

    def has_storage_notice(self):
        context = self.state.get('recovery_context') or {}
        return (context.get('category') == 'storage_pressure'
                and not self.state.get('journal')
                and self.state['mode'] in ('ARMED', 'PAUSED', 'DISABLED')
                and self.state.get('last_error') in (None, '', context.get('reason')))

    def clear_storage_notice(self):
        """Called only after the space threshold and durable write probe pass.

        Do not erase a separate restoration failure or change enabled intent.
        The caller must persist both status copies before announcing healthy.
        """
        if not self.has_storage_notice():
            return False
        self.state['last_error'] = None
        self.state.pop('recovery_context', None)
        return True

    def effective_waiting_for(self):
        context = self.state.get('recovery_context', {})
        waiting = self.waiting_for
        if (waiting is None and self.state['mode'] == 'ARMED' and
                context.get('category') == 'detector_unavailable'):
            waiting = 'trusted UU connection'
        return waiting

    def status(self):
        now = self.clock()
        waiting = self.effective_waiting_for()
        return {**copy.deepcopy(self.state), 'timestamp': now, 'updated_at': now, 'controller_pid': os.getpid(),
                'state': self.state['mode'],
                'enabled': self.state['mode'] in ('ARMED', 'ACTIVE'), 'waiting_for': waiting,
                'automatic_resume_allowed': bool(self.state['desired_enabled']),
                'recovery_pending': bool(self.state.get('journal')),
                'next_retry': self.retry_at,
                'metrics': {**self.metrics, **getattr(self.bd, 'metrics', {})}}

    def _reject_command(self, command):
        atomic_json(self.directory / 'command-result.json', {
            'id': command.get('id') if isinstance(command, dict) else None,
            'status': 'error', 'message': 'Invalid command'})

    @staticmethod
    def _valid_command(command):
        return (isinstance(command, dict) and isinstance(command.get('id'), str)
                and bool(command['id']) and command.get('action') in ('enable', 'pause', 'recover'))

    def _manual_exit_intent(self, command):
        if command.get('exit_after_restore') is True:
            # Recovery temporarily clears desired_enabled. A second system exit
            # inherits only the live pending exit intent, never historical state.
            enabled = (self.state['desired_enabled'] or
                       (self.state.get('exit_after_restore') and
                        self.state.get('exit_resume_enabled') is True))
            self.state['exit_after_restore'] = command['id']
            self.state['exit_previous_command_id'] = command.get('previous_command_id')
            self.state['exit_resume_enabled'] = bool(
                command.get('preserve_enabled_after_exit') is True and enabled)
        else:
            self.state.pop('exit_after_restore', None)
            self.state.pop('exit_resume_enabled', None)
            self.state.pop('exit_previous_command_id', None)
        self.state.pop('exit_recovery_pending', None)

    def _entry_checkpoint(self):
        if not self.entering:
            return
        if self.storage_recovery is not None and self.storage_recovery.low_space():
            raise OSError(errno.ENOSPC, 'Disk space below the safe display-transaction threshold')
        try:
            command = read_json(self.directory / 'command.json')
        except (ValueError, OSError):
            command = None
        if self._valid_command(command):
            if command['id'] != self.state['last_command'] and command['action'] in ('pause', 'recover'):
                self.state['last_command'] = command['id']
                self._manual_exit_intent(command)
                raise TransitionInterrupted('Manual ' + command['action'], 'PAUSED', 'manual')
        elif command is not None and (not isinstance(command, dict) or command.get('id') != self.state['last_command']):
            self._reject_command(command)
        # Isolated Engine callers need no detector file. A real daemon always
        # has one; check its live evidence throughout the display transaction.
        path = self.directory / 'signal.json'
        if not path.exists():
            return
        try:
            detector = read_json(path)
        except (ValueError, OSError):
            detector = None
        if not isinstance(detector, dict):
            raise TransitionInterrupted('Detector signal unavailable during entry')
        stamp = detector.get('native_timestamp', detector.get('timestamp'))
        if (not isinstance(stamp, (int, float)) or not 0 <= self.clock() - stamp < self.signal_max_age):
            raise TransitionInterrupted('Detector stale during entry')
        if detector.get('sleeping') is True:
            raise TransitionInterrupted('System sleep', category='sleep')
        if detector.get('trusted') is not True:
            raise TransitionInterrupted('Detector permission unavailable during entry')
        if detector.get('session_health_fault'):
            raise TransitionInterrupted('Detector health fault during entry')
        expected = self.state.get('entry_controller_name')
        if expected and detector.get('state') == 'connected' and not detector.get('session_held'):
            from device_profiles import normalize_name
            if detector.get('activeControllerCount', 1) != 1:
                raise TransitionInterrupted('Controller count changed during entry', category='device_change')
            names = detector.get('controllers')
            if isinstance(names, list) and names:
                try:
                    changed = (len(names) != 1 or normalize_name(names[0]) != expected
                               or detector.get('activeControllerCount', 1) != 1)
                except ValueError:
                    changed = True
                if changed:
                    raise TransitionInterrupted('Remote device changed during entry', category='device_change')
        if (detector.get('state') == 'disconnected' and
                (detector.get('session_negative_verified') is True or
                 detector.get('reason') == 'uu_not_running' or
                 detector.get('session_source') == 'verified_negative')):
            raise TransitionInterrupted('Remote disconnected during entry', category='remote_disconnected')

    def _wait_inventory(self, predicate, description, timeout=15):
        phase_started = self.monotonic()
        deadline = phase_started + timeout
        last_error = 'condition not met'
        self.waiting_for = description
        try:
            while True:
                self._entry_checkpoint()
                # Refresh the watchdog/UI heartbeat throughout planned settling.
                atomic_json(self.directory / 'status.json', self.status())
                try:
                    self.metrics['inventory_calls'] += 1
                    devices = self.bd.inventory()
                    self._entry_checkpoint()
                    if predicate(devices):
                        return devices
                except TransitionInterrupted:
                    raise
                except Exception as exc:
                    last_error = str(exc)
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise RuntimeError('Timed out waiting for ' + description + ': ' + last_error)
                self.sleeper(min(0.5, remaining))
        finally:
            phases = self.metrics.setdefault('phases', [])
            phases.append({'phase': description, 'timestamp': self.clock(),
                           'duration_seconds': self.monotonic() - phase_started})
            self.metrics['phases'] = phases[-40:]
            self.waiting_for = None
            atomic_json(self.directory / 'status.json', self.status())

    def _inventory(self):
        return self._wait_inventory(lambda devices: True, 'system display inventory')

    def _make_main(self, select, description):
        # BetterDisplay can acknowledge a main-display request while a preceding
        # topology change is still settling. Re-select the exact live output and
        # retry only inside this bounded transition, never on an idle timer.
        for attempt in range(3):
            devices = self._inventory()
            target = select(devices)
            if target['main']:
                return devices
            self._entry_checkpoint()
            try:
                self.bd.set(target, main=True)
            except (RuntimeError, subprocess.SubprocessError):
                if attempt == 2:
                    raise
            try:
                return self._wait_inventory(lambda ds: select(ds)['main'], description,
                                            timeout=2 if attempt < 2 else 10)
            except TransitionInterrupted:
                raise
            except RuntimeError:
                if attempt == 2:
                    raise

    @staticmethod
    def _same_field(field, actual, expected):
        if field == 'refreshRate':
            try:
                return abs(float(str(actual).removesuffix('Hz')) -
                           float(str(expected).removesuffix('Hz'))) < 0.001
            except (ValueError, TypeError):
                pass
        return str(actual) == str(expected)

    @staticmethod
    def _mode_changes(device, desired):
        changes = {}
        for field in ('resolution', 'hiDPI', 'rotation', 'refreshRate'):
            expected = desired.get(field)
            if expected is None:
                continue
            actual = device.get(field)
            if not Engine._same_field(field, actual, expected):
                changes[field] = expected
        return changes

    @staticmethod
    def _matches(devices, snapshots, require_main=True, placement=True):
        current = {d['identity']: d for d in devices}
        for identity, original in snapshots.items():
            device = current.get(identity)
            if not device or not device['connected']:
                return False
            if any(not Engine._same_field(k, device[k], original[k]) for k in FIELDS
                   if original.get(k) is not None and (placement or k != 'placement')):
                return False
            if require_main and original['main'] and not device['main']:
                return False
        return True

    @staticmethod
    def _recovery_layout(snapshots, current):
        """Project mixed remote/physical coordinates to a usable physical plan.

        A display first seen beside a virtual main can overlap the wider saved
        physical main. Keep the durable snapshots intact, anchor the effective
        main at zero, and move only overlapping rectangles to their nearest free
        position. Inventory resolution already includes rotation.
        """
        plan = copy.deepcopy(snapshots)

        def rectangle(device):
            size = re.fullmatch(r'([1-9]\d*)x([1-9]\d*)', str(device.get('resolution', '')))
            position = re.fullmatch(r'(-?\d+)x(-?\d+)', str(device.get('placement', '')))
            if size and position:
                return (*map(int, position.groups()), *map(int, size.groups()))

        def overlaps(a, b):
            return (max(a[0], b[0]) < min(a[0] + a[2], b[0] + b[2]) and
                    max(a[1], b[1]) < min(a[1] + a[3], b[1] + b[3]))

        mains = [key for key, value in plan.items() if value.get('main')]
        if len(mains) > 1:
            raise RuntimeError('Physical recovery main output ambiguous')
        rectangles = {key: rectangle(value) for key, value in plan.items()}
        fixed = [rectangle(value) for key, value in current.items()
                 if key not in plan and value['kind'] == 'Display' and value['connected']
                 and not value.get('mirrored')]
        # Incomplete geometry is not permission to relax the original checks.
        if any(value is None for value in [*rectangles.values(), *fixed]):
            return plan
        if mains:
            dx, dy = rectangles[mains[0]][:2]
            for key, (x, y, width, height) in rectangles.items():
                plan[key]['placement'] = f'{x - dx}x{y - dy}'
            rectangles = {key: rectangle(value) for key, value in plan.items()}
        # Reserve all originally conflict-free screens before placing a moved
        # one, so solving an overlap cannot displace an otherwise valid screen.
        reserved = set(mains)
        for key, rect in rectangles.items():
            if (not any(overlaps(rect, other) for other in fixed) and
                    not any(overlaps(rect, other) for other_key, other in rectangles.items()
                            if other_key != key)):
                reserved.add(key)
        if mains and any(overlaps(rectangles[mains[0]], other) for other in fixed):
            raise RuntimeError('Physical recovery main overlaps an unmanaged physical output')
        placed = [*fixed, *(rectangles[key] for key in reserved)]
        order = sorted(plan, key=lambda key: (
            not plan[key].get('main'), bool(plan[key].get('captured_during_remote')),
            abs(rectangles[key][0]) + abs(rectangles[key][1]), key))
        for key in order:
            if key in reserved:
                continue
            rect = rectangles[key]
            if any(overlaps(rect, other) for other in placed):
                x, y, width, height = rect
                xs = {x, *(edge for other in placed for edge in (other[0] - width, other[0] + other[2]))}
                ys = {y, *(edge for other in placed for edge in (other[1] - height, other[1] + other[3]))}
                candidates = ((nx, ny, width, height) for nx in xs for ny in ys)
                rect = min((candidate for candidate in candidates
                            if not any(overlaps(candidate, other) for other in placed)),
                           key=lambda candidate: ((candidate[0] - x) ** 2 + (candidate[1] - y) ** 2,
                                                  abs(candidate[1] - y), candidate[0], candidate[1]))
                plan[key]['placement'] = f'{rect[0]}x{rect[1]}'
            placed.append(rect)
        return plan

    def _physical_recovery_plan(self, snapshots, current, phase):
        plan = self._recovery_layout(snapshots, current)
        changed = {key: {'saved': original.get('placement'), 'effective': plan[key].get('placement')}
                   for key, original in snapshots.items()
                   if original.get('placement') != plan[key].get('placement')}
        if changed:
            runtime_event(self.directory, 'physical_layout_normalized', phase=phase, placements=changed)
        return plan

    def _settle_physical_recovery(self, originals):
        deadline = self.monotonic() + 5
        stable_since = None
        repairs = 0
        while self.monotonic() < deadline:
            devices = self._inventory()
            current = {device['identity']: device for device in devices}
            for identity, original in originals.items():
                device = current.get(identity)
                if not device or not device['connected'] or device.get('mirrored'):
                    raise RuntimeError('Physical output unavailable before protection restoration: ' + identity)
                if original['main'] and not device['main']:
                    raise RuntimeError('Physical main changed after virtual disconnect: ' + identity)
            if self._matches(devices, originals):
                if stable_since is None:
                    stable_since = self.monotonic()
                if self.monotonic() - stable_since >= 1:
                    return
            else:
                stable_since = None
                if repairs >= 2:
                    raise RuntimeError('Physical layout keeps changing after virtual disconnect')
                for identity, original in originals.items():
                    device = current[identity]
                    changes = self._mode_changes(device, original)
                    if changes:
                        self.bd.set(device, **changes)
                current = {device['identity']: device for device in self._wait_inventory(
                    lambda ds: self._matches(ds, originals, placement=False),
                    'physical modes after virtual disconnect')}
                for identity, original in originals.items():
                    if current[identity]['placement'] != original['placement']:
                        self.bd.set(current[identity], placement=original['placement'])
                repairs += 1
            self.sleeper(.15)
        raise RuntimeError('Physical layout did not stabilize after virtual disconnect')

    def _stable_physical_match(self, originals):
        stable_since = None
        def matches(devices):
            nonlocal stable_since
            if not self._matches(devices, originals):
                stable_since = None
                return False
            if stable_since is None:
                stable_since = self.monotonic()
            return self.monotonic() - stable_since >= 1
        return matches

    @staticmethod
    def target(devices, spec=None):
        spec = spec or {'name': TARGET_NAME, 'identities': [TARGET_ID, TARGET_UUID]}
        candidates = [d for d in devices if d['kind'] == 'VirtualScreen' and
                      d['name'] == spec['name']]
        if len(candidates) != 1 or candidates[0]['identity'] not in spec['identities']:
            raise RuntimeError('Target virtual identity missing, changed, or ambiguous')
        if sum(d['identity'] == candidates[0]['identity'] for d in devices) != 1:
            raise RuntimeError('Target virtual identity is duplicated')
        return candidates[0]

    def _target(self, devices):
        journal = self.state.get('journal')
        # Recovery always addresses the exact target captured before mutation,
        # regardless of a newly selected device or a partially saved profile.
        if journal and journal.get('target'):
            old = journal['target']
            spec = {'name': old['name'], 'identities': [old['identity']]}
        else:
            spec = self.state.get('target_spec')
        return self.target(devices, spec)

    @staticmethod
    def _valid_resolution(value):
        if not isinstance(value, str):
            return False
        match = re.fullmatch(r'([1-9][0-9]{0,4})x([1-9][0-9]{0,4})', value)
        return bool(match and all(16 <= int(n) <= 16384 for n in match.groups()))

    def _preferred_mode(self):
        saved = self.state.get('preferred_virtual_mode')
        if isinstance(saved, dict) and self._valid_resolution(saved.get('resolution')):
            return {**MODE, 'resolution': saved['resolution']}
        return dict(MODE)

    def _remember_resolution(self, target):
        resolution = target.get('resolution')
        if not self._valid_resolution(resolution):
            return
        preferred = {**MODE, 'resolution': resolution}
        if preferred != self._preferred_mode():
            self.state['preferred_virtual_mode'] = preferred
            self._save()
            runtime_event(self.directory, 'virtual_resolution_adopted', preferred=preferred)

    def _stage_resolution(self, target):
        resolution = target.get('resolution')
        if not self._valid_resolution(resolution):
            return
        if resolution == self._preferred_mode()['resolution']:
            self.pending_resolution = None
        elif not self.pending_resolution or self.pending_resolution['resolution'] != resolution:
            self.pending_resolution = {'resolution': resolution, 'observed_after': self.clock(),
                                       'refresh_attempts': 0, 'refresh_at': self.clock()}

    def _commit_resolution(self, detector):
        candidate = self.pending_resolution
        if not candidate or detector.get('session_held') or detector.get('raw_state', detector.get('state')) != 'connected':
            return
        observed = detector.get('session_observed_at')
        if (not isinstance(observed, (int, float)) or isinstance(observed, bool)
                or not math.isfinite(observed) or not candidate['observed_after'] <= observed <= self.clock()):
            return
        self._remember_resolution(candidate)
        self.pending_resolution = None

    def needs_resolution_refresh(self):
        if (self.state['mode'] != 'ACTIVE' or not self.pending_resolution or
                self.pending_resolution['refresh_attempts'] >= 2 or
                self.clock() < self.pending_resolution['refresh_at']):
            return False
        self.pending_resolution['refresh_attempts'] += 1
        self.pending_resolution['refresh_at'] = self.clock() + 1.25
        return True

    def _verified_target(self, devices=None, exact_resolution=False, *, spec=None, preferred=None):
        devices = self._inventory() if devices is None else devices
        target = self.target(devices, spec) if spec is not None else self._target(devices)
        if not target['connected']:
            raise RuntimeError('Target virtual display is not online')
        if target.get('mirrored'):
            raise RuntimeError('Target virtual display is mirrored')
        if (not self._valid_resolution(target['resolution']) or not truth(target['hiDPI']) or
                (exact_resolution and target['resolution'] != (preferred or self._preferred_mode())['resolution'])):
            raise RuntimeError('Target mode verification failed: resolution=' + str(target['resolution']) +
                               ', hiDPI=' + str(target['hiDPI']) + ', refreshRate=' + str(target['refreshRate']))
        try:
            rate = float(str(target['refreshRate']).removesuffix('Hz').strip())
        except (ValueError, TypeError):
            rate = float('nan')
        if not math.isfinite(rate) or abs(rate - 60) > 0.1:
            raise RuntimeError('Target refresh rate verification failed')
        return target

    def _capture(self, devices, initial=False):
        if any(d['kind'] == 'Display' and d['connected'] and d.get('mirrored') for d in devices):
            raise RuntimeError('Mirrored display layout not supported; screens unchanged')
        journal = self.state.get('journal')
        if initial:
            target = self._target(devices)
            # Starting a new session adopts the user's current desktop. Older
            # pending coordinates/main roles must never supersede that baseline.
            present = {device['identity'] for device in devices
                       if device['kind'] == 'Display' and device['connected']}
            pending = copy.deepcopy(self.state.get('detached', {}))
            for identity in list(pending):
                if identity in present:
                    del pending[identity]
                else:
                    pending[identity]['mode_only'] = True
            physical = [d for d in devices if d['kind'] == 'Display' and d['connected']]
            baseline = next((d['identity'] for d in physical if d['main']),
                            physical[0]['identity'] if physical else None)
            journal = {'physical': {}, 'target': copy.deepcopy(target),
                       'baseline_main': baseline, 'created': self.clock(),
                       'protections': copy.deepcopy(self.bd.protections())}
            self.state['restore_destination'] = ('ARMED' if self.state['desired_enabled'] else 'PAUSED')
            journal['restore_destination'] = self.state['restore_destination']
            self.state['journal'] = journal
            self.state['detached'] = pending
        changed = initial
        for device in devices:
            if device['kind'] == 'Display' and device['connected']:
                if device['identity'] not in journal['physical']:
                    if not journal.get('baseline_main'):
                        journal['baseline_main'] = device['identity']
                    captured = copy.deepcopy(device)
                    captured['main'] = device['identity'] == journal['baseline_main']
                    if not initial:
                        captured['captured_during_remote'] = True
                    journal['physical'][device['identity']] = captured
                    changed = True
        if changed:
            self._save()  # Must complete before any set operation.

    def _verified_active_target(self):
        # Display callbacks can arrive between WindowServer's bounds and mode
        # updates. Allow a short settling interval for mode mismatch only.
        devices = self._inventory()
        deadline = self.monotonic() + 3
        first = True
        resolution_candidate = None
        checked_writers = set()
        while True:
            try:
                target = self._target(devices)
                if not target['connected'] or target.get('mirrored'):
                    return self._verified_target(devices)  # Preserve immediate safety faults.
                resolution = target.get('resolution')
                if (self._valid_resolution(resolution) and resolution != self._preferred_mode()['resolution']
                        and self.clock() < self.initial_mode_guard_until and not self.initial_uu_mode_corrected
                        and hasattr(self.bd, 'latest_mode_writer')
                        and (resolution, target.get('mode_identifier')) not in checked_writers):
                    checked_writers.add((resolution, target.get('mode_identifier')))
                    writer = self.bd.latest_mode_writer(target)
                    if writer == 'UURemoteServer':
                        latest_devices = self._inventory()
                        latest = self._target(latest_devices)
                        if (latest.get('resolution'), latest.get('mode_identifier')) != (resolution, target.get('mode_identifier')):
                            devices = latest_devices
                            continue  # A newer user choice supersedes the observed UU reset.
                        self.initial_uu_mode_corrected = True
                        self.pending_resolution = None
                        runtime_event(self.directory, 'uu_initial_mode_reset', current=resolution,
                                      preferred=self._preferred_mode())
                        self.bd.set(target, **self._mode_changes(target, self._preferred_mode()))
                        fixed = self._wait_inventory(lambda ds: bool(self._verified_target(ds, exact_resolution=True)),
                                                     'initial UU mode reset correction', timeout=5)
                        return self._verified_target(fixed, exact_resolution=True)
                if self.pending_resolution and resolution != self.pending_resolution['resolution']:
                    self.pending_resolution = None
                if resolution == self._preferred_mode()['resolution']:
                    self.pending_resolution = None
                if self._valid_resolution(resolution) and resolution != self._preferred_mode()['resolution']:
                    # Bounds/mode snapshots can straddle a display callback.
                    # Two matching reads accept the user's mode without setting it back.
                    if resolution_candidate == resolution:
                        self._stage_resolution(target)
                    else:
                        resolution_candidate = resolution
                        remaining = deadline - self.monotonic()
                        if remaining > 0:
                            self.sleeper(min(.15, remaining))
                            self.metrics['inventory_calls'] += 1
                            devices = self.bd.inventory()
                            continue
                return self._verified_target(devices)
            except RuntimeError as exc:
                if not str(exc).startswith(('Target mode verification failed', 'Target refresh rate verification failed')):
                    raise  # Missing identity or offline output is immediately unsafe.
                if first or self.monotonic() >= deadline:
                    if _runtime_logging:
                        runtime_event(self.directory, 'target_mode_settling', final=not first,
                                      target=self._target(devices), error=str(exc))
                    first = False
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    break
                self.sleeper(min(.15, remaining))
                self.metrics['inventory_calls'] += 1
                devices = self.bd.inventory()

        # A remote client can select a different display mode after capture
        # starts. Correct the known, still-online virtual output before treating
        # that as a display failure. Never repair identity loss or a missing
        # output, and never keep fighting a client that repeatedly changes it.
        target = self._target(devices)
        if (not target['connected'] or target.get('mirrored') or
                self.state['mode'] != 'ACTIVE' or not self.state.get('journal') or
                not self.state.get('desired_enabled')):
            raise RuntimeError('Target mode repair requires the active journaled virtual display')
        # A user may keep moving the resolution control throughout the entire
        # settling interval. Accept the latest valid mode if its quality is
        # already good; changing desktop size must never spend a repair budget.
        self._stage_resolution(target)
        try:
            return self._verified_target(devices)
        except RuntimeError as exc:
            if not str(exc).startswith(('Target mode verification failed', 'Target refresh rate verification failed')):
                raise
        now = self.clock()
        attempts = [stamp for stamp in self.state.get('mode_repair_attempts', [])
                    if isinstance(stamp, (int, float)) and now - stamp <= 60]
        if len(attempts) >= 2:
            raise RuntimeError('Target display mode keeps changing; mode repair budget reached')
        self.state['mode_repair_attempts'] = [*attempts, now]
        self._save()  # Persist the limit before any mutation, including restart.
        self._stage_resolution(target)
        preferred = ({**MODE, 'resolution': target['resolution']}
                     if self._valid_resolution(target.get('resolution')) else self._preferred_mode())
        runtime_event(self.directory, 'target_mode_repair', before=target,
                      desired=preferred, attempt=len(attempts) + 1)
        self.bd.set(target, **preferred)
        devices = self._wait_inventory(lambda ds: bool(self._verified_target(ds)),
                                       'corrected virtual display mode', timeout=5)
        verified = self._verified_target(devices)
        runtime_event(self.directory, 'target_mode_repaired', target=verified)
        return verified

    def _enter(self):
        self.entering = True
        try:
            self._enter_transition()
        finally:
            self.entering = False

    def _enter_transition(self):
        devices = self._inventory()
        target = self._target(devices)
        self._capture(devices, initial=True)
        preserve_preferred = self.state.pop('entry_preserve_preferred', False)
        if (not preserve_preferred and target['connected'] and truth(target.get('hiDPI'))
                and not target.get('mirrored')):
            self._remember_resolution(target)
        for group in self.state['journal'].get('protections', []):
            if group['enabled']:
                self._entry_checkpoint()
                self.bd.set_protection(group, False)
        self._entry_checkpoint()
        self.bd.set(target, connected=True)
        devices = self._wait_inventory(lambda ds: self._target(ds)['connected'], 'virtual display online')
        changes = self._mode_changes(self._target(devices), self._preferred_mode())
        if changes:
            self._entry_checkpoint()
            self.bd.set(self._target(devices), **changes)
        devices = self._wait_inventory(lambda ds: bool(self._verified_target(ds, exact_resolution=True)), 'virtual display mode')
        self._make_main(lambda ds: self._verified_target(ds, True), 'virtual main display')
        self._hide_physical()
        # BetterDisplay can carry the previous mode into the physical-display
        # disconnect transaction. Finish that topology, then recheck the chosen
        # mode before declaring the remote desktop ready.
        final = self._target(self._inventory())
        settled_since = self.monotonic()
        settle_deadline = settled_since + 3
        signature = tuple(final.get(key) for key in ('resolution', 'hiDPI', 'refreshRate', 'mode_identifier', 'main'))
        for _ in range(16):
            if self.monotonic() - settled_since >= 1 or self.monotonic() >= settle_deadline:
                break
            self.sleeper(.2)
            final = self._target(self._inventory())
            if not final['connected'] or final.get('mirrored'):
                raise RuntimeError('Virtual output changed unsafely during final topology settling')
            current = tuple(final.get(key) for key in ('resolution', 'hiDPI', 'refreshRate', 'mode_identifier', 'main'))
            if current != signature:
                signature = current
                settled_since = self.monotonic()
        changes = self._mode_changes(final, self._preferred_mode())
        if changes:
            self._entry_checkpoint()
            self.bd.set(final, **changes)
        self._wait_inventory(lambda ds: bool(self._verified_target(ds, exact_resolution=True)),
                             'final virtual display mode')
        self.state['mode'] = 'ACTIVE'
        self.initial_mode_guard_until = self.clock() + 10
        self.initial_uu_mode_corrected = False
        self.state['last_error'] = None
        self.retry_at = 0

    def reconfigure_active(self, spec, preferred, profile_id, managed_targets, owner):
        """Change a live mode or hand over virtual outputs without ending a session.

        The physical baseline and layout protections belong to the whole remote
        session. Record both outputs before connecting the candidate, verify it
        before retiring the old output, and retain both for interrupted recovery.
        """
        if (self.state['mode'] != 'ACTIVE' or not self.state.get('journal')
                or not self.state.get('desired_enabled')):
            raise RuntimeError('Live configuration requires an active recovery journal')
        self.entering = True
        self.state['entry_controller_name'] = owner
        try:
            self._entry_checkpoint()
            devices = self._inventory()
            old = self._verified_target(devices)
            candidate = self.target(devices, spec)
            if candidate.get('mirrored'):
                raise RuntimeError('Candidate virtual display is mirrored')
            self._capture(devices)
            journal = self.state['journal']
            targets = journal.setdefault('virtual_targets', {})
            targets.setdefault(old['identity'], copy.deepcopy(old))
            targets.setdefault(candidate['identity'], copy.deepcopy(candidate))
            journal['live_change'] = {'from': old['identity'], 'to': candidate['identity'],
                                      'profile_id': profile_id, 'preferred': preferred,
                                      'phase': 'preparing'}
            self._save()  # Covers a crash after candidate connection, before commit.
            if not candidate['connected']:
                self._entry_checkpoint()
                self.bd.set(candidate, connected=True)
                devices = self._wait_inventory(lambda ds: self.target(ds, spec)['connected'],
                                               'candidate virtual display online')
                candidate = self.target(devices, spec)
            changes = self._mode_changes(candidate, preferred)
            if changes:
                self._entry_checkpoint()
                self.bd.set(candidate, **changes)
            devices = self._wait_inventory(
                lambda ds: bool(self._verified_target(ds, True, spec=spec, preferred=preferred)),
                'candidate virtual display mode')
            candidate = self.target(devices, spec)
            if not candidate['main']:
                devices = self._make_main(
                    lambda ds: self._verified_target(ds, True, spec=spec, preferred=preferred),
                    'candidate virtual main display')
            self._entry_checkpoint()
            # This is the new recovery fallback from this point forward. The
            # original physical snapshot, timestamp and protections stay intact.
            journal['target'] = copy.deepcopy(self.target(devices, spec))
            journal['live_change']['phase'] = 'retiring_previous'
            self.state.update(target_spec=copy.deepcopy(spec), active_profile_id=profile_id,
                              preferred_virtual_mode=copy.deepcopy(preferred),
                              managed_virtual_targets=copy.deepcopy(managed_targets),
                              active_controller_name=owner)
            self.pending_resolution = None
            self._save()
            self._hide_physical()
            self._wait_inventory(lambda ds: bool(self._verified_target(ds, True)),
                                 'final live virtual display mode')
            self._entry_checkpoint()
            journal.pop('live_change', None)
            self.state['last_error'] = None
            self.retry_at = 0
            self.initial_mode_guard_until = self.clock() + 10
            self.initial_uu_mode_corrected = False
            self._save()
            runtime_event(self.directory, 'active_display_reconfigured', profile_id=profile_id,
                          previous=old['identity'], target=journal['target']['identity'], preferred=preferred)
        finally:
            self.entering = False

    def discard_transients(self):
        """Finish durable temporary-screen cleanup, also used after a crash."""
        journal = self.state.get('journal')
        if not journal:
            return
        for identity, original in list(journal.get('transient_virtuals', {}).items()):
            path = self.directory / 'managed-virtuals.json'
            records = read_json(path)
            if records is not None:
                if (not isinstance(records, dict) or records.get('version') != 1
                        or not isinstance(records.get('devices'), dict)):
                    raise RuntimeError('Temporary virtual ownership record is invalid')
                record = records['devices'].get(identity)
                if record:
                    if (record.get('profile_id') != original['profile_id']
                            or record.get('virtual', {}).get('name') != original['name']):
                        raise RuntimeError('Temporary virtual ownership changed')
            if journal['target']['identity'] == identity:
                # Do not delete the journal's only recovery fallback. Recovery
                # can be interrupted again after cleanup, or physical hardware
                # may disappear before the final layout is fully restored.
                devices = self._inventory()
                candidates = [original.get('fallback_target', {})] + list(journal.get('virtual_targets', {}).values())
                fallback = None
                for candidate in candidates:
                    candidate_id = candidate.get('identity')
                    if not candidate_id or candidate_id in journal.get('transient_virtuals', {}):
                        continue
                    try:
                        fallback = self.target(devices, {'name': candidate['name'], 'identities': [candidate_id]})
                        break
                    except RuntimeError:
                        continue
                if fallback is None:
                    raise RuntimeError('No persistent virtual fallback before temporary cleanup')
                journal['target'] = copy.deepcopy(fallback)
                self.state['target_spec'] = {'name': fallback['name'], 'identities': [fallback['identity']]}
                self._save()  # The fallback must be durable before deletion.
            self.bd.discard_virtual(original)
            self._wait_inventory(lambda ds: not any(d['identity'] == identity for d in ds),
                                 'temporary virtual definition removed')
            if records is not None:
                if records['devices'].get(identity):
                    del records['devices'][identity]
                    atomic_json(path, records)
            journal['transient_virtuals'].pop(identity)
            journal.get('virtual_targets', {}).pop(identity, None)
            journal.get('other_managed_virtuals', {}).pop(identity, None)
            self.state['managed_virtual_targets'] = [
                spec for spec in self.state.get('managed_virtual_targets', [])
                if identity not in spec.get('identities', [])]
            self._save()

    def _hide_physical(self):
        devices = self._inventory()
        self._capture(devices)
        verified = self._verified_target(devices)
        owned = self.state.get('managed_virtual_targets', [])
        other_virtuals = [d for d in devices if d['kind'] == 'VirtualScreen' and d['connected']
                          and d['identity'] != verified['identity'] and any(
                              d['identity'] in spec['identities'] and d['name'] == spec['name'] for spec in owned)]
        if other_virtuals:
            self.state['journal'].setdefault('other_managed_virtuals', {}).update(
                {d['identity']: copy.deepcopy(d) for d in other_virtuals})
            self._save()
            for device in other_virtuals:
                self._entry_checkpoint()
                self.bd.set(device, connected=False)
            identities = {d['identity'] for d in other_virtuals}
            devices = self._wait_inventory(lambda ds: not any(
                d['identity'] in identities and d['connected'] for d in ds), 'other managed virtual screens offline')
        physical = [d for d in devices if d['kind'] == 'Display' and d['connected']]
        if physical:
            verified = self._verified_target(devices)
            if not verified['main']:
                self._make_main(lambda ds: self._verified_target(ds), 'virtual main before physical disconnect')
            if hasattr(self.bd, 'set_connections'):
                self._entry_checkpoint()
                self.bd.set_connections(physical, False)
            else:
                for device in physical:
                    self._entry_checkpoint()
                    self.bd.set(device, connected=False)
            identities = {d['identity'] for d in physical}
            self._wait_inventory(lambda ds: not any(d['identity'] in identities and d['connected'] for d in ds),
                                 'physical disconnect rejected or unsettled')

    def _begin_restore(self, reason, afterwards='PAUSED', category=None):
        self.pending_resolution = None
        self.state['last_error'] = reason
        self.after_restore = afterwards
        self.state['restore_destination'] = afterwards
        if afterwards == 'PAUSED':
            self.state['desired_enabled'] = False
        self.state['recovery_context'] = dict(
            category=category or ('automatic_recovery' if afterwards == 'ARMED' else 'safety_fault'),
            reason=reason, automatic_resume=afterwards == 'ARMED', timestamp=self.clock())
        runtime_event(self.directory, 'recovery_begin', destination=afterwards,
                      context=self.state['recovery_context'])
        if self.state.get('journal'):
            self.state['journal']['restore_destination'] = afterwards
        self.state['mode'] = 'RESTORING' if self.state.get('journal') else afterwards
        self.retry_at = 0
        self._save()

    def stop_for_restart(self, preserve_intent=True):
        destination = 'ARMED' if preserve_intent and self.state['desired_enabled'] else 'PAUSED'
        if self.state.get('recovery_context', {}).get('category') == 'restart_budget':
            self._begin_restore(self.state['recovery_context']['reason'], afterwards='PAUSED',
                                category='restart_budget')
            return
        self._begin_restore('Controller stopped', afterwards=destination,
                            category='process_restart' if destination == 'ARMED' else 'manual_stop')

    def record_process_failure(self):
        """A cleanly drained crash still consumes the interrupted-run budget."""
        now = self.clock()
        history = [stamp for stamp in self.state.get('interrupted_restarts', [])
                   if isinstance(stamp, (int, float)) and 0 <= now - stamp <= 300]
        history.append(now)
        self.state.update(interrupted_restarts=history, failure_recorded=True)
        if len(history) >= 3:
            reason = 'Interrupted restart budget reached; restoring physical displays then pausing'
            self.state.update(desired_enabled=False, restore_destination='PAUSED',
                              exit_resume_enabled=False,
                              recovery_context=dict(category='restart_budget', reason=reason,
                                                    automatic_resume=False, timestamp=now))
            self.after_restore = 'PAUSED'
        self._save()

    def _restore(self):
        journal = self.state.get('journal')
        if not journal:
            self.state['mode'] = self.after_restore
            return
        # Each retry can follow a crash after protections were re-enabled.
        for group in journal.get('protections', []):
            if group['enabled']:
                self.bd.set_protection(group, False)
        devices = self._inventory()
        current = {d['identity']: d for d in devices}
        errors = []
        for identity, original in journal['physical'].items():
            device = current.get(identity)
            if not device:
                continue  # Detached hardware is not fabricated or re-created.
            try:
                if not device['connected']:
                    self.bd.set(device, connected=True)
            except Exception as exc:
                errors.append(str(exc))
        expected = set(journal['physical']).intersection(current)
        if expected:
            settled = self._wait_inventory(
                lambda ds: all(any(d['identity'] == identity and d['connected'] for d in ds)
                               for identity in expected), 'physical displays online')
            current = {d['identity']: d for d in settled}
        online = [d for d in current.values() if d['kind'] == 'Display' and d['connected']]
        if not online:
            fallback = self._target(list(current.values()))
            if not fallback['connected']:
                self.bd.set(fallback, connected=True)
                settled = self._wait_inventory(lambda ds: self._target(ds)['connected'], 'fallback virtual online')
            else:
                settled = list(current.values())
            try:
                # A definition may have changed orientation before a crash. A
                # good existing HiDPI mode is a usable fallback even if the old
                # saved resolution has the opposite orientation.
                self._verified_target(settled)
            except RuntimeError:
                target = self._target(settled)
                preferred = self._preferred_mode()
                if self._valid_resolution(target.get('resolution')):
                    width, height = map(int, target['resolution'].split('x'))
                    wanted_width, wanted_height = map(int, preferred['resolution'].split('x'))
                    if (width < height) != (wanted_width < wanted_height):
                        preferred['resolution'] = f'{wanted_height}x{wanted_width}'
                changes = self._mode_changes(target, preferred)
                if changes:
                    self.bd.set(target, **changes)
                self._wait_inventory(lambda ds: bool(self._verified_target(ds)), 'fallback virtual mode')
            raise RuntimeError('No physical output online; keeping virtual safety fallback')
        for identity, original in journal['physical'].items():
            device = current.get(identity)
            if not device:
                continue
            if not device['connected']:
                errors.append('Physical output still offline: ' + identity)
                continue
            try:
                changes = self._mode_changes(device, original)
                if changes:
                    self.bd.set(device, **changes)
            except Exception as exc:
                errors.append(str(exc))
        # A missing former main display makes the old absolute coordinates
        # impossible: macOS always places the current main at 0x0. Build a
        # temporary translated plan, leaving the durable originals intact for
        # detached-display recovery when the missing device returns.
        originals = {identity: copy.deepcopy(original) for identity, original in journal['physical'].items()
                     if identity in current and current[identity]['connected']}
        if originals and not any(original['main'] for original in originals.values()):
            fallback = next((identity for identity in originals if current[identity]['main']),
                            sorted(originals)[0])
            anchor = re.fullmatch(r'(-?\d+)x(-?\d+)', str(originals[fallback].get('placement', '')))
            for identity, original in originals.items():
                original['main'] = identity == fallback
                position = re.fullmatch(r'(-?\d+)x(-?\d+)', str(original.get('placement', '')))
                if anchor and position:
                    original['placement'] = f'{int(position[1]) - int(anchor[1])}x{int(position[2]) - int(anchor[2])}'
            originals[fallback]['placement'] = '0x0'
        if not errors:
            settled = self._wait_inventory(
                lambda ds: self._matches(ds, originals, require_main=False, placement=False),
                'physical display modes')
            current = {d['identity']: d for d in settled}
        # Setting main can move origins; apply saved placement again afterwards.
        for identity, original in originals.items():
            if (original['main'] and identity in current and current[identity]['connected']
                    and not current[identity]['main']):
                try:
                    def select_physical(ds, identity=identity, name=current[identity]['name']):
                        matches = [d for d in ds if d['identity'] == identity and d['name'] == name
                                   and d['kind'] == 'Display' and d['connected']]
                        if len(matches) != 1:
                            raise RuntimeError('Physical main output missing or ambiguous')
                        return matches[0]
                    self._make_main(select_physical, 'physical main display')
                except Exception as exc:
                    errors.append(str(exc))
        if not errors:
            settled = self._wait_inventory(
                lambda ds: all(any(d['identity'] == identity and d['main'] and d['connected'] for d in ds)
                               for identity, original in originals.items() if original['main']),
                'physical main display')
            current = {d['identity']: d for d in settled}
        originals = self._physical_recovery_plan(originals, current, 'session')
        for identity, original in originals.items():
            if (identity in current and current[identity]['connected']
                    and current[identity]['placement'] != original['placement']):
                try:
                    self.bd.set(current[identity], placement=original['placement'])
                except Exception as exc:
                    errors.append(str(exc))
        if not errors:
            settled = self._wait_inventory(lambda ds: self._matches(ds, originals), 'physical display layout')
        else:
            settled = self._inventory()
        verified = {d['identity']: d for d in settled}
        for identity, original in originals.items():
            device = verified.get(identity)
            if device:
                if not self._matches(settled, {identity: original}, require_main=False):
                    errors.append('Restoration verification failed: ' + identity)
                if original['main'] and not device['main']:
                    errors.append('Main restoration verification failed: ' + identity)
        if errors:
            raise RuntimeError('; '.join(errors))
        # A crash during a handover can leave both virtual outputs online. Every
        # output touched by this session was recorded before connection; close
        # those exact owned identities after the original physical layout is safe.
        targets = {**journal.get('other_managed_virtuals', {}),
                   **journal.get('virtual_targets', {}),
                   journal['target']['identity']: journal['target']}
        for identity, original in targets.items():
            matches = [d for d in settled if d['identity'] == identity]
            if len(matches) > 1 or (matches and (matches[0]['kind'] != 'VirtualScreen'
                                               or matches[0]['name'] != original['name'])):
                raise RuntimeError('Recovery virtual identity changed or ambiguous: ' + identity)
            target = matches[0] if matches else None
            if target and target['connected']:
                if not any(d['kind'] == 'Display' and d['connected'] for d in self._inventory()):
                    raise RuntimeError('Physical outputs disappeared before virtual disconnect')
                self.bd.set(target, connected=False)
                self._wait_inventory(
                    lambda ds: not any(d['identity'] == identity and d['connected'] for d in ds),
                    'target virtual disconnect')
        self.discard_transients()
        # Disconnecting a virtual output can make the remote server initialize
        # the remaining physical screen again. Reconcile the saved fields once
        # before capturing them in the restored layout protection.
        self._settle_physical_recovery(originals)
        # Protection captures the final layout, including the dedicated virtual
        # being offline, so re-enable only after that layout is verified.
        if not errors:
            for group in journal.get('protections', []):
                if group['enabled']:
                    try:
                        self.bd.set_protection(group, True)
                    except Exception as exc:
                        errors.append(str(exc))
            if any(group['enabled'] for group in journal.get('protections', [])):
                actual_groups = {g['tag']: g for g in self.bd.protections()}
                for group in journal['protections']:
                    if group['enabled'] and not actual_groups.get(group['tag'], {}).get('enabled'):
                        errors.append('Layout protection restoration verification failed: ' + group['tag'])
        if errors:
            raise RuntimeError('; '.join(errors))
        final = {d['identity']: d for d in self._wait_inventory(
            self._stable_physical_match(originals), 'final protected physical layout')}
        for identity, original in originals.items():
            device = final.get(identity)
            if device and not self._matches(list(final.values()), {identity: original}):
                raise RuntimeError('Final protected layout verification failed: ' + identity)
        detached = self.state.setdefault('detached', {})
        missing_main = any(original['main'] and identity not in verified
                           for identity, original in journal['physical'].items())
        for identity, original in journal['physical'].items():
            if missing_main or identity not in verified:
                # An older incomplete layout must survive later user baselines.
                detached.setdefault(identity, copy.deepcopy(original))
        self.state['journal'] = None
        self.state['mode'] = self.after_restore
        self.restore_attempts = 0
        self.retry_at = 0
        if (self.after_restore == 'ARMED'
                or self.state.get('recovery_context', {}).get('category') in ('manual', 'manual_stop')
                or str(self.state.get('last_error', '')).startswith('Manual ')):
            self.state['last_error'] = None
        runtime_event(self.directory, 'recovery_complete', destination=self.after_restore)

    def _restore_detached(self, now):
        pending = self.state.get('detached', {})
        if not pending or now < self.retry_at:
            return
        self.retry_at = now + 30
        try:
            current = {d['identity']: d for d in self._inventory()}
            for identity, original in list(pending.items()):
                if not original.get('mode_only') or identity not in current:
                    continue
                device = current[identity]
                self._save()
                if not device['connected']:
                    self.bd.set(device, connected=True)
                    current = {d['identity']: d for d in self._wait_inventory(
                        lambda ds: any(d['identity'] == identity and d['connected'] for d in ds),
                        'returning detached display online')}
                    device = current[identity]
                changes = self._mode_changes(device, original)
                if changes:
                    self.bd.set(device, **changes)
                self._wait_inventory(lambda ds: self._matches(ds, {identity: original},
                    require_main=False, placement=False), 'detached display modes')
                del pending[identity]
                self._save()
            plan = {key: original for key, original in pending.items() if not original.get('mode_only')}
            if not plan or not set(plan).issubset(current):
                return
            # Restore main before *any* origins: WindowServer can translate all
            # other screens when the original main returns.
            for identity, original in plan.items():
                device = current[identity]
                self._save()
                if not device['connected']:
                    self.bd.set(device, connected=True)
            current = {d['identity']: d for d in self._wait_inventory(
                lambda ds: all(any(d['identity'] == key and d['connected'] for d in ds)
                               for key in plan), 'returning detached displays online')}
            for identity, original in plan.items():
                changes = self._mode_changes(current[identity], original)
                if changes:
                    self.bd.set(current[identity], **changes)
                if original['main'] and not current[identity]['main']:
                    self.bd.set(current[identity], main=True)
            current = {d['identity']: d for d in self._wait_inventory(
                lambda ds: all(any(d['identity'] == key and d['main'] for d in ds)
                               for key, original in plan.items() if original['main']),
                'detached main display')}
            plan = self._physical_recovery_plan(plan, current, 'detached')
            for identity, original in plan.items():
                if current[identity]['placement'] != original['placement']:
                    self.bd.set(current[identity], placement=original['placement'])
            self._wait_inventory(lambda ds: self._matches(ds, plan), 'detached display layout')
            for identity in plan:
                del pending[identity]
            self._save()
        except Exception as exc:
            self.state['last_error'] = str(exc)
        self.retry_at = now + 30

    def next_timeout(self, detector=None):
        now = self.clock()
        deadlines = [now + 30]  # status heartbeat, no display inventory
        if self.state['mode'] == 'ACTIVE' and self.pending_resolution and self.pending_resolution['refresh_attempts'] < 2:
            deadlines.append(max(now, self.pending_resolution['refresh_at']))
        signal = detector or {}
        if (self.state['mode'] == 'ARMED' and not signal.get('profile_entry_blocked') and signal.get('state') == 'connected'
                and signal.get('trusted') is True and not signal.get('sleeping')
                and isinstance(signal.get('timestamp'), (int, float))
                and 0 <= now - signal['timestamp'] < self.signal_max_age):
            deadlines.append(now)
        if self.state['mode'] == 'RESTORING':
            deadlines.append(max(now, self.retry_at))
        confirmed_disconnected = (signal.get('state') == 'disconnected' and signal.get('trusted') is True
                                  and isinstance(signal.get('timestamp'), (int, float))
                                  and 0 <= now - signal['timestamp'] < self.signal_max_age)
        if self.disconnected_since is not None and self.state['mode'] == 'ACTIVE' and confirmed_disconnected:
            deadlines.append(self.disconnected_since + DISCONNECT_GRACE)
        if self.state.get('detached') and (self.state['mode'] in ('DISABLED', 'PAUSED') or
                                          (self.state['mode'] == 'ARMED' and confirmed_disconnected)):
            deadlines.append(max(now, self.retry_at))
        if self.state['mode'] in ('ARMED', 'ACTIVE'):
            stamp = (detector or {}).get('timestamp', now)
            # Once safely waiting with physical screens restored, an expired
            # detector lease must not create a zero-timeout busy loop.
            if isinstance(stamp, (int, float)) and stamp + self.signal_max_age > now:
                deadlines.append(stamp + self.signal_max_age)
        return max(0, min(deadlines) - now)

    def tick(self, detector=None, command=None):
        now = self.clock()
        started = self.monotonic()
        initial_mode = self.state['mode']
        healthy = False
        try:
            if command is not None and not self._valid_command(command):
                self._reject_command(command)
                command = None
            if command is not None and command.get('id') != self.state['last_command']:
                action = command.get('action')
                self.state['last_command'] = command['id']
                if action == 'enable':
                    self.state.pop('exit_after_restore', None)
                    self.state.pop('exit_resume_enabled', None)
                    self.state.pop('exit_recovery_pending', None)
                    self.state.pop('exit_previous_command_id', None)
                if (action == 'enable' and self.state['mode'] in ('DISABLED', 'PAUSED', 'ARMED')
                        and not self.state.get('journal')):
                    self.state['mode'] = 'ARMED'
                    self.state['desired_enabled'] = True
                    self.state['restore_destination'] = 'ARMED'
                    self.state['last_error'] = None
                    self.last_good = now
                elif action == 'enable' and self.state['mode'] == 'RESTORING':
                    # Consume the explicit intent without entering over the old
                    # journal. Resume only after that recovery has completed.
                    self.state['desired_enabled'] = True
                    self.state['restore_destination'] = self.after_restore = 'ARMED'
                    if self.state.get('journal'):
                        self.state['journal']['restore_destination'] = 'ARMED'
                elif action in ('pause', 'recover'):
                    self._manual_exit_intent(command)
                    self._begin_restore('Manual ' + action, category='manual')
                self._save()
            if self.state['mode'] in ('DISABLED', 'PAUSED'):
                self._restore_detached(now)
                return self.status()
            if (not self.event_driven and self.state['mode'] in ('ARMED', 'ACTIVE') and self.last_tick is not None
                    and (now - self.last_tick > 15 or now < self.last_tick)):
                self._begin_restore('Controller clock/heartbeat gap', afterwards='ARMED')
            self.last_tick = now
            detector = detector or {}
            if (detector.get('state') != 'connected' or
                    detector.get('raw_reason', detector.get('reason')) == 'ax_home_disconnect_confirmation_pending'):
                self.pending_resolution = None
            stamp = detector.get('timestamp', 0)
            valid_stamp = isinstance(stamp, (int, float)) and 0 <= now - stamp < self.signal_max_age
            healthy = (valid_stamp and detector.get('trusted') is True and
                       detector.get('state') in ('connected', 'disconnected'))
            # A one-shot verification has a fixed evidence time. Reusing that
            # snapshot must not extend the detector's uncertainty grace period.
            confirmation_time = stamp if detector.get('session_verification') else now
            if self.state['mode'] == 'RESTORING':
                pass
            elif detector.get('sleeping') is True:
                healthy = False
                self._begin_restore('System sleep', afterwards='ARMED', category='sleep')
            elif detector.get('session_health_fault') == 'generation_changed':
                healthy = False
                if self.state['mode'] == 'ACTIVE':
                    self._begin_restore('Native session generation changed', afterwards='ARMED',
                                        category='detector_unavailable')
            elif healthy:
                self.last_good = confirmation_time
            elif (self.event_driven and (not isinstance(stamp, (int, float)) or now - stamp >= self.signal_max_age or now - self.last_good >= self.signal_max_age)) or (not self.event_driven and now - self.last_good >= 15):
                if self.state['mode'] != 'ARMED' or self.state.get('journal'):
                    self._begin_restore('Detector stale, untrusted, or unknown', afterwards='ARMED',
                                        category='detector_unavailable')
            if self.state['mode'] == 'RESTORING':
                if now >= self.retry_at:
                    try:
                        self._restore()
                    except Exception as exc:
                        self.restore_attempts += 1
                        self.state['last_error'] = str(exc)
                        delay = 5 if self.after_restore == 'ARMED' else 30
                        self.retry_at = self.clock() + (delay if self.restore_attempts < 5 else 300)
                return self.status()
            if not healthy:
                display_key = ((detector.get('run_id'), detector['display_revision'])
                               if 'display_revision' in detector else None)
                if self.state['mode'] == 'ACTIVE' and (
                        not self.event_driven or
                        (display_key is not None and display_key != getattr(self, 'last_display_key', None)) or
                        (detector.get('event') == 'display' and detector.get('event_id') != self.last_event_id)):
                    # Topology safety is independent of session evidence. Never
                    # repair modes or hide newly attached physical screens here.
                    target = self._target(self._inventory())
                    if not target['connected'] or target.get('mirrored'):
                        raise RuntimeError('Active target offline or mirrored during detector uncertainty')
                    self.last_display_key = display_key
                return self.status()
            if self.state['mode'] == 'ARMED' and detector['state'] == 'disconnected':
                self._restore_detached(now)
            if detector['state'] == 'connected':
                self.disconnected_since = None
                display_key = ((detector.get('run_id'), detector['display_revision'])
                               if 'display_revision' in detector else None)
                if self.state['mode'] == 'ARMED' and not detector.get('profile_entry_blocked'):
                    self._enter()
                elif self.state['mode'] == 'ACTIVE' and (not self.event_driven or
                        (display_key is not None and display_key != getattr(self, 'last_display_key', None)) or
                        (detector.get('event_id') != self.last_event_id and detector.get('event') == 'display') or
                        self.last_detector_state != 'connected'):
                    if self.event_driven:
                        self._verified_active_target()
                    else:
                        self._verified_target()
                    self._hide_physical()
                if self.state['mode'] == 'ACTIVE':
                    self.last_display_key = display_key
                    self._commit_resolution(detector)
            elif self.state['mode'] == 'ACTIVE':
                if self.disconnected_since is None:
                    self.disconnected_since = now
                if now - self.disconnected_since >= DISCONNECT_GRACE:
                    # Run restoration through the retry-preserving branch next tick.
                    self._begin_restore('Remote disconnected', afterwards='ARMED', category='remote_disconnected')
        except TransitionInterrupted as exc:
            self._begin_restore(str(exc), afterwards=exc.destination, category=exc.category)
            if self.state.get('journal'):
                try:
                    self._restore()
                except Exception as restore_exc:
                    self.state['last_error'] = str(exc) + '; recovery: ' + str(restore_exc)
                    self.restore_attempts += 1
                    self.retry_at = self.clock() + 5
        except Exception as exc:
            if is_storage_error(exc):
                raise
            self._begin_restore(str(exc))
            if self.state.get('journal'):
                try:
                    self._restore()
                except Exception as restore_exc:
                    self.state['last_error'] = str(exc) + '; recovery: ' + str(restore_exc)
                    self.restore_attempts += 1
                    self.retry_at = now + 30
        finally:
            # Inventory and display transitions can be slow; only time spent
            # outside this completed tick counts as an event-loop clock gap.
            self.last_tick = self.clock()
            self.last_event_id = (detector or {}).get('event_id')
            self.last_detector_state = (detector or {}).get('state')
            if initial_mode != self.state['mode']:
                self.metrics['transitions'].append({'from': initial_mode, 'to': self.state['mode'],
                    'timestamp': self.clock(), 'duration_seconds': self.monotonic() - started})
                self.metrics['transitions'] = self.metrics['transitions'][-20:]
            if healthy:
                self.last_good = (confirmation_time if detector.get('session_verification')
                                  else self.clock())
            self._save()
        return self.status()


def wake_controller(directory, manual_command=None):
    try:
        if manual_command is not None:
            manual_command = dict(manual_command)
            if 'previous_command_id' not in manual_command:
                previous = read_json(Path(directory) / 'command.json', {})
                manual_command['previous_command_id'] = previous.get('id') if isinstance(previous, dict) else None
        fd = os.open(str(Path(directory) / 'wake.fifo'), os.O_WRONLY | os.O_NONBLOCK)
        try:
            payload = (b'\nUU_DISPLAY_COMMAND ' + json.dumps(manual_command).encode('utf-8') + b'\n'
                       if manual_command is not None else b'w')
            return os.write(fd, payload) == len(payload)
        finally:
            os.close(fd)
    except (FileNotFoundError, OSError):
        return False  # Startup normally reads the durable command.


class WakeFIFO:
    def __init__(self, directory):
        path = Path(directory) / 'wake.fifo'
        try:
            os.mkfifo(path, 0o600)
        except FileExistsError:
            if not stat.S_ISFIFO(path.lstat().st_mode):
                raise RuntimeError('wake.fifo is not a FIFO')
        self.fd = os.open(str(path), os.O_RDWR | os.O_NONBLOCK)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.fd, selectors.EVENT_READ)
        self.manual_commands = []
        self.buffer = b''

    def wait(self, timeout):
        if self.manual_commands or self.selector.select(timeout):
            self.drain()

    def wait_for_retry(self, timeout):
        # Already queued commands cannot progress while an earlier write is
        # blocked. Back off, but let a fresh wakeup (e.g. newer enable) interrupt.
        if self.selector.select(timeout):
            self.drain()

    def drain(self):
        """Collect available fallback commands without waiting or consuming them."""
        while True:
            try:
                data = os.read(self.fd, 65536)
                if not data:
                    break
                self.buffer = (self.buffer + data)[-8192:]
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    if not line.startswith(b'UU_DISPLAY_COMMAND '):
                        continue
                    try:
                        command = json.loads(line[len(b'UU_DISPLAY_COMMAND '):])
                        if (Engine._valid_command(command) and command['action'] in ('pause', 'recover')
                                and len(command['id']) <= 200):
                            self.manual_commands = (self.manual_commands + [command])[-8:]
                    except (ValueError, UnicodeError):
                        pass
            except BlockingIOError:
                break

    def close(self):
        self.selector.close()
        os.close(self.fd)

    def take_manual_command(self):
        return self.manual_commands.pop(0) if self.manual_commands else None


def prepare_session_input(directory, engine, verifier, detector):
    def latest_command():
        try:
            return read_json(Path(directory) / 'command.json')
        except (ValueError, OSError):
            return None
    command = latest_command()
    manual = (isinstance(command, dict) and command.get('id') != engine.state.get('last_command')
              and command.get('action') in ('pause', 'recover'))
    user_change = (isinstance(command, dict) and command.get('id') != engine.state.get('last_command')
                   and command.get('action') in ('profile_bind', 'profile_apply', 'profile_select'))
    if user_change and hasattr(verifier, 'refresh_for_command'):
        detector = verifier.refresh_for_command(detector)
    elif not manual and engine.state.get('desired_enabled'):
        detector = verifier.resolve(detector)
    # A pause arriving during the bounded synchronous read takes priority over
    # its result, before any display transaction can start.
    return detector, latest_command()


def native_storage_report(payload):
    executable = Path.home() / 'Applications/UUDisplayAutomation.app/Contents/MacOS/UUDisplayAutomation'
    subprocess.run([str(executable), '--report-storage-health', json.dumps(payload)],
                   capture_output=True, timeout=3, check=False)


def run_controller(args, *, storage_factory=StorageRecovery, reporter=native_storage_report,
                   sleeper=time.sleep):
    """Keep one lock owner alive while storage is temporarily unavailable."""
    global _runtime_directory, _runtime_logging
    from cursor_profile import CursorProfileWorker
    from office_cursor_relay import OfficeCursorRelay
    from session_recheck import SessionRecheck
    from session_policy import SessionPolicy
    from profile_runtime import DeviceCoordinator

    _runtime_directory, _runtime_logging = args.directory, True
    storage = storage_factory(args.directory, reporter=reporter)
    engine = coordinator = cursor = office = verifier = policy = wake = lock = None
    stopped, stop_signal = False, None
    pending_manual = None
    previous_cursor, cursor_snapshot_valid = None, False
    last_detected = None
    ordinary_completion = False

    def stopping(signum, frame):
        nonlocal stopped, stop_signal
        stopped, stop_signal = True, signum
        if wake:
            wake_controller(args.directory)

    signals.signal(signals.SIGTERM, stopping)
    signals.signal(signals.SIGINT, stopping)
    runtime_event(args.directory, 'process_start', ppid=os.getppid(), python=sys.version,
                  argv=sys.argv, binary=args.binary)

    def state_info():
        return dict(recovery_pending=bool(engine and engine.state.get('journal')),
                    mode=engine.state['mode'] if engine else 'STARTING')

    def disk_command():
        try:
            value = read_json(args.directory / 'command.json')
            return value if isinstance(value, dict) else None
        except (ValueError, OSError):
            return None

    def collect_manual():
        nonlocal pending_manual
        if wake:
            wake.drain()
        # Keep the current fallback until it is committed. An ENOSPC retry
        # must not replace an earlier pause with a later preserve-exit.
        value = wake.take_manual_command() if wake and pending_manual is None else None
        if value:
            original_id = value.pop('previous_command_id', None)
            if value.get('exit_after_restore') is True:
                value['previous_command_id'] = original_id
            current = disk_command() or {}
            current_id = current.get('id')
            # Only an explicit newer enable revokes a manual stop. A newer
            # profile edit may still be saved after pausing; it cannot unpause.
            if current_id in (None, original_id, value['id']) or current.get('action') != 'enable':
                pending_manual = (value, original_id)
        return pending_manual[0] if pending_manual else disk_command()

    def restore_for_storage():
        if engine is None:
            return
        manual = collect_manual()
        if (Engine._valid_command(manual) and manual['action'] in ('pause', 'recover')
                and manual['id'] != engine.state.get('last_command')):
            engine.state['last_command'] = manual['id']
            engine._manual_exit_intent(manual)
            engine._begin_restore('Manual ' + manual['action'], afterwards='PAUSED', category='manual')
        elif engine.state['mode'] != 'RESTORING':
            destination = 'ARMED' if engine.state['desired_enabled'] else 'PAUSED'
            engine._begin_restore('磁盘空间不足，暂停新切屏并保留恢复记录',
                                  afterwards=destination, category='storage_pressure')
        # All display recovery still uses the ordinary durable journal and its
        # write-before-mutation checks. An unwritable journal is never bypassed.
        engine._save()
        engine.tick({'profile_entry_blocked': True})

    def wait(timeout):
        if not stopped:
            wake.wait(timeout) if wake else sleeper(timeout)

    def initialize():
        nonlocal engine, coordinator, cursor, office, verifier, policy
        if engine is None:
            storage.ensure_reserve()
            engine = Engine(BetterDisplay(args.binary), args.directory, event_driven=args.event_driven,
                            signal_max_age=90 if args.event_driven else 5, persist_initial=False)
            engine.storage_recovery = storage
            engine._save()
        if policy is None:
            atomic_json(args.directory / 'session-policy.json', {'version': 1})
            policy = SessionPolicy()
        if coordinator is None:
            coordinator = DeviceCoordinator(engine)
            if coordinator.error and is_storage_error(coordinator.error):
                raise RuntimeError(coordinator.error)
        if cursor is None:
            cursor = CursorProfileWorker(args.directory, helper=args.directory / 'cursor-image-probe')
        if office is None:
            office = OfficeCursorRelay(args.directory)
        if verifier is None:
            verifier = SessionRecheck(args.directory,
                Path.home() / 'Applications/UUDisplayAutomation.app/Contents/MacOS/UUDisplayAutomation')

    def finish_shutdown(preserve_intent, command_id=None, cancelable=False):
        """Keep the lock and sole recovery owner until every restorer stops.

        A stopped worker can leave durable originals after a failed batch. A
        replacement may retry them, but only after the former worker is dead.
        This path never performs a new remote entry or reads the UU interface.
        """
        nonlocal cursor
        begin_pending = bool(engine and (command_id is None or engine.state['desired_enabled'] or
                                         engine.state['mode'] not in ('RESTORING', 'PAUSED')))
        cursor_restored = False
        worker_stopped = cursor is None
        attempts = 0
        cancel_pending = False

        def publish_shutdown_status():
            if coordinator is not None:
                try:
                    coordinator.publish(read_json(args.directory / 'signal.json', {}))
                except Exception as error:
                    runtime_event(args.directory, 'shutdown_ui_publish_pending', error=str(error))

        def refresh_shutdown_command():
            nonlocal command_id, preserve_intent, pending_manual, begin_pending, cancel_pending
            if engine is None:
                return False
            cancel = cancel_pending
            while True:
                latest = collect_manual()
                commands = [latest]
                if pending_manual:
                    manual, original_id = pending_manual
                    durable = disk_command() or {}
                    if durable.get('id') in (None, original_id, manual['id'], engine.state.get('last_command')):
                        # A fallback must become durable before its owner exits.
                        # On ENOSPC retain pending_manual and retry under this lock.
                        atomic_json(args.directory / 'command.json', manual)
                    elif durable.get('action') == 'enable':
                        commands = [durable]
                    else:
                        # A durable command newer than the FIFO's baseline follows
                        # the fallback, so a later preserve-exit cannot undo pause.
                        commands.append(durable)
                    pending_manual = None
                for latest in commands:
                    if not Engine._valid_command(latest):
                        continue
                    if latest['action'] == 'enable':
                        cancel = bool(cancelable and (command_id or cancel_pending) and latest['id'] != command_id and
                                      latest['id'] != engine.state.get('exit_previous_command_id'))
                        continue
                    if latest['id'] == engine.state.get('last_command'):
                        continue
                    if latest['id'] == engine.state.get('exit_previous_command_id'):
                        continue
                    begin_pending = False
                    if latest.get('exit_after_restore') is True:
                        command_id = latest['id']
                        preserve_intent = False
                        cancel = False
                    else:
                        command_id = None
                        preserve_intent = False
                        cancel = cancelable
                    # tick may mutate RAM before its durable save fails. Keep the
                    # new handshake identity/cancellation through the retry too.
                    cancel_pending = cancel
                    engine.tick({'profile_entry_blocked': True}, latest)
                cancel_pending = cancel
                if wake is None or not wake.manual_commands:
                    return cancel

        while True:
            cancel_requested = False
            recovery_error = None
            try:
                cancel_requested = refresh_shutdown_command()
                if engine:
                    if begin_pending:
                        engine.stop_for_restart(preserve_intent=preserve_intent)
                        begin_pending = False
                    # A previous attempt may have failed between RAM mutation
                    # and its write. Never bypass the complete journal barrier.
                    engine._save()
                    engine.tick({'profile_entry_blocked': True})
            except Exception as error:
                recovery_error = error
            try:
                if office:
                    office.close()
                if not cursor_restored:
                    if cursor is None:
                        cursor = CursorProfileWorker(args.directory, helper=args.directory / 'cursor-image-probe')
                    outcome = cursor.close()
                    worker_stopped = outcome.get('stopped', True)
                    if outcome.get('stopped', True):
                        cursor = None
                        cursor_restored = (outcome.get('restored', True) and
                                           not outcome.get('pending_recovery', False))
                if recovery_error is None:
                    cancel_requested = refresh_shutdown_command()
                displays_restored = engine is None or not engine.state.get('journal')
                office_stopped = office is None or getattr(office, 'process', None) is None
                if cancel_requested and recovery_error is None and worker_stopped and office_stopped:
                    # A closing worker refuses new sync requests. Its successor
                    # may be created only after it has stopped under this lock.
                    return False
                if (not cancel_pending and pending_manual is None and
                        displays_restored and cursor_restored and office_stopped and
                        (recovery_error is None or command_id is None)):
                    if engine and command_id:
                        before_completion = copy.deepcopy(engine.state)
                        engine.state.pop('exit_after_restore', None)
                        engine.state.pop('exit_recovery_pending', None)
                        engine.state.pop('exit_previous_command_id', None)
                        resume = engine.state.pop('exit_resume_enabled', False)
                        if resume:
                            engine.state.update(mode='ARMED', desired_enabled=True,
                                                restore_destination='ARMED')
                            engine.after_restore = 'ARMED'
                        engine.state['exit_completed'] = dict(
                            id=command_id, timestamp=time.time(), controller_pid=os.getpid(),
                            resume_enabled=resume)
                        try:
                            engine._save()
                        except Exception:
                            engine.state = before_completion
                            engine.after_restore = 'PAUSED'
                            # state may have landed before status failed. Put
                            # the durable request back before waiting/retrying.
                            try:
                                engine._save()
                            except Exception as rollback_error:
                                runtime_event(args.directory, 'shutdown_receipt_rollback_pending',
                                              error=str(rollback_error), command_id=command_id)
                            raise
                    publish_shutdown_status()
                    runtime_event(args.directory, 'shutdown_recovery_complete',
                                  command_id=command_id, preserve_intent=preserve_intent)
                    return True
                if (command_id is None and not cancel_pending and pending_manual is None and
                        attempts >= 1 and worker_stopped and office_stopped):
                    # SIGTERM and unexpected failures have a supervisor to
                    # restart them. Hand back only stopped restorers and keep
                    # their durable journals; an explicit Quit handshake waits
                    # for actual recovery instead of taking this bounded exit.
                    runtime_event(args.directory, 'shutdown_deferred_to_restart',
                                  display_pending=not displays_restored,
                                  cursor_pending=not cursor_restored)
                    return True
                runtime_event(args.directory, 'shutdown_recovery_pending',
                              display_pending=not displays_restored,
                              cursor_pending=not cursor_restored, office_pending=not office_stopped)
                if engine and command_id:
                    engine.state['exit_recovery_pending'] = dict(
                        displays=not displays_restored, cursor=not cursor_restored, office=not office_stopped)
                    engine._save()
                    publish_shutdown_status()
            except Exception as error:
                recovery_error = error
            if recovery_error is not None:
                if is_storage_error(recovery_error):
                    storage.activate(recovery_error, **state_info())
                runtime_event(args.directory, 'shutdown_recovery_pending', error=str(recovery_error))
            storage.heartbeat(**state_info())
            delay = (5, 10, 20, 30)[min(attempts, 3)]
            attempts += 1
            # Signal shutdown must also wait; wait() intentionally skips waits
            # after a signal and would turn this recovery loop into a busy loop.
            wake.wait_for_retry(delay) if wake else sleeper(delay)

    try:
        while not stopped:
            try:
                # Acquire existing authority even at low free space. Do not
                # advertise a waiting controller while another process owns it.
                if lock is None and (not storage.active or (args.directory / 'controller.lock').exists()):
                    args.directory.mkdir(parents=True, exist_ok=True)
                    candidate_lock = (args.directory / 'controller.lock').open('a')
                    try:
                        fcntl.flock(candidate_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        candidate_lock.close()
                        raise SystemExit('Controller already running')
                    lock = candidate_lock
                if wake is None and args.event_driven and lock and (args.directory / 'wake.fifo').exists():
                    wake = WakeFIFO(args.directory)
                if storage.active:
                    manual = collect_manual()
                    if (engine and Engine._valid_command(manual) and manual['action'] in ('pause', 'recover')
                            and manual['id'] != engine.state.get('last_command')):
                        try:
                            restore_for_storage()
                        except Exception as error:
                            runtime_event(args.directory, 'storage_manual_recovery_pending', error=str(error))
                    storage.heartbeat(**state_info())
                    if not storage.ready():
                        wait(min(30, storage.next_timeout()))
                        continue
                    restore_for_storage()
                    if cursor_snapshot_valid:
                        path = args.directory / 'cursor-profile-config.json'
                        if previous_cursor is None:
                            path.unlink(missing_ok=True)
                        else:
                            atomic_json(path, previous_cursor)
                        cursor_snapshot_valid = False
                elif storage.low_space():
                    raise OSError(errno.ENOSPC, 'Available space below the safe display-transaction threshold')
                if lock is None:
                    args.directory.mkdir(parents=True, exist_ok=True)
                    lock = (args.directory / 'controller.lock').open('a')
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise SystemExit('Controller already running')
                if wake is None and args.event_driven:
                    wake = WakeFIFO(args.directory)
                initialize()
                if not storage.active and engine.has_storage_notice():
                    # A previous version/process can leave an otherwise idle
                    # storage warning behind. Reuse the ordinary hysteresis and
                    # fsync probe instead of merely hiding the saved warning.
                    storage.activate(OSError(errno.ENOSPC, 'Rechecking saved storage recovery'),
                                     **state_info())
                    continue
                if pending_manual:
                    manual, original_id = pending_manual
                    current_id = (disk_command() or {}).get('id')
                    if current_id in (None, original_id, manual['id']):
                        atomic_json(args.directory / 'command.json', manual)
                    # A newer durable command takes precedence over the old
                    # fallback. Otherwise persist the manual command so an old
                    # durable enable/profile command cannot replay after it.
                    pending_manual = None
                if storage.active:
                    engine.clear_storage_notice()
                    engine._save()
                    coordinator.publish(read_json(args.directory / 'signal.json', {}))
                    storage.clear()
                    storage.ensure_reserve()
                    runtime_event(args.directory, 'storage_recovered', pid_unchanged=True)
                try:
                    detector = read_json(args.directory / 'signal.json', {})
                except (ValueError, OSError):
                    detector = {}
                detected = tuple(detector.get(k) for k in ('run_id', 'state', 'trusted', 'reason', 'sleeping'))
                if detected != last_detected:
                    runtime_event(args.directory, 'detector_state', received_at=time.time(),
                                  observed_at=detector.get('timestamp'),
                                  **{k: detector.get(k) for k in ('state', 'trusted', 'reason', 'sleeping',
                                                                 'event_id', 'ax_reads', 'observer_supported')})
                    last_detected = detected
                previous_cursor = read_json(args.directory / 'cursor-profile-config.json')
                cursor_snapshot_valid = True
                detector, command = prepare_session_input(args.directory, engine, verifier, detector)
                detector = policy.evaluate(detector, engine.state['mode'])
                detector['policy_controller_pid'] = os.getpid()
                atomic_json(args.directory / 'effective-session.json', detector)
                detector = coordinator.tick(detector, command)
                if engine.needs_resolution_refresh() or policy.needs_refresh(detector, engine.state['mode'],
                        engine.state.get('desired_enabled'), engine.state.get('last_command')):
                    try:
                        result = subprocess.run([verifier.executable, '--request-session-refresh'],
                                                timeout=3, check=False, capture_output=True)
                        runtime_event(args.directory, 'session_refresh_requested', succeeded=result.returncode == 0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                office.sync(engine.state['mode'] == 'ACTIVE')
                cursor.sync(engine.state['mode'] == 'ACTIVE')
                exit_command = engine.state.get('exit_after_restore')
                if exit_command:
                    if finish_shutdown(False, exit_command, cancelable=True):
                        ordinary_completion = True
                        break
                    continue
                if args.once:
                    ordinary_completion = True
                    break
                timeout = engine.next_timeout(detector) if wake else 1
                verification_timeout = verifier.next_timeout()
                if engine.state['desired_enabled'] and verification_timeout is not None:
                    timeout = min(timeout, verification_timeout)
                wait(timeout)
                collect_manual()
            except Exception as error:
                if not is_storage_error(error):
                    raise
                storage.activate(error, **state_info())
                runtime_event(args.directory, 'storage_pressure', error=str(error), **state_info())
                if office:
                    office.close()
                if cursor:
                    cursor.sync(False)
                try:
                    # Loading an old journal needs no new transaction. The
                    # freed reserve can restore physical output immediately;
                    # 512 MiB is required only before resuming new operations.
                    if engine is None and lock is not None:
                        engine = Engine(BetterDisplay(args.binary), args.directory,
                                        event_driven=args.event_driven,
                                        signal_max_age=90 if args.event_driven else 5, persist_initial=False)
                        engine.storage_recovery = storage
                    if wake is None and args.event_driven and lock:
                        wake = WakeFIFO(args.directory)
                    restore_for_storage()
                    if cursor_snapshot_valid and coordinator:
                        coordinator._restore_cursor_projection(previous_cursor)
                except Exception as recovery_error:
                    runtime_event(args.directory, 'storage_recovery_pending', error=str(recovery_error))
                coordinator = verifier = policy = None
                storage.heartbeat(force=True, **state_info())
                wait(min(30, storage.next_timeout()))
    finally:
        if not ordinary_completion and engine:
            if not stopped:
                try:
                    engine.record_process_failure()
                except Exception as failure_error:
                    runtime_event(args.directory, 'process_failure_record_pending', error=str(failure_error))
            exit_command = engine.state.get('exit_after_restore')
            finish_shutdown(preserve_intent=not exit_command and (not stopped or stop_signal == signals.SIGTERM),
                            command_id=exit_command)
        else:
            if office:
                office.close()
            if cursor:
                outcome = cursor.close()
                runtime_event(args.directory, 'cursor_worker_shutdown', **outcome)
                # Even --once may not hand a live helper to another lock owner.
                while not outcome.get('stopped', True):
                    wake.wait(1) if wake else sleeper(1)
                    outcome = cursor.close()
        if wake:
            wake.close()
        if lock:
            lock.close()


def main():
    global _runtime_directory, _runtime_logging
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument('--binary', default=BINARY)
    parser.add_argument('--event-driven', action='store_true')
    for flag in ('once', 'status', 'enable', 'pause', 'recover', 'dry-run'):
        parser.add_argument('--' + flag, action='store_true')
    args = parser.parse_args()
    _runtime_directory = args.directory
    actions = [x for x in ('enable', 'pause', 'recover') if getattr(args, x)]
    if len(actions) > 1:
        parser.error('Choose one command')
    if args.dry_run:
        # Read-only topology inspection: no state/journal/command writes.
        print(json.dumps({'dry_run': True, 'inventory': BetterDisplay(args.binary).inventory(),
                          'protections': BetterDisplay(args.binary).protections()}, indent=2))
        return
    if actions:
        command = {'id': uuid.uuid4().hex, 'action': actions[0]}
        try:
            previous = read_json(args.directory / 'command.json', {})
        except (ValueError, OSError):
            previous = {}
        previous_id = previous.get('id') if isinstance(previous, dict) else None
        try:
            atomic_json(args.directory / 'command.json', command)
        except OSError as error:
            if not (is_storage_error(error) and actions[0] in ('pause', 'recover')
                    and wake_controller(args.directory, {**command, 'previous_command_id': previous_id})):
                raise
        wake_controller(args.directory)
        if not args.once:
            return
    if args.status:
        print(json.dumps(read_json(args.directory / 'status.json', {'mode': 'DISABLED'}), indent=2))
        return
    run_controller(args)


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        if _runtime_logging:
            runtime_event(_runtime_directory, 'process_exit', reason='exception',
                          exception=repr(exc), traceback=traceback.format_exc())
        raise
    else:
        if _runtime_logging:
            runtime_event(_runtime_directory, 'process_exit', reason='normal')
