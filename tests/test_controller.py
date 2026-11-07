import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[1] / 'controller.py'
spec = importlib.util.spec_from_file_location('controller', MODULE)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def display(identity='physical1', connected=True, main=True, kind='Display', **extra):
    return dict(identity=identity, kind=kind, name=identity, tag=identity,
                mode_tag=identity, connected=connected, main=main,
                resolution='1920x1080', hiDPI='off', rotation='0',
                placement='0x0', refreshRate='60', **extra)


def target(connected=False):
    value = display(c.TARGET_UUID, connected, False, 'VirtualScreen')
    value['name'] = c.TARGET_NAME
    return value


class FakeBD:
    def __init__(self, devices):
        self.devices = copy.deepcopy(devices)
        self.calls = []
        self.fail = None
        self.ignore_disconnect = False
        self.directory = None
        self.groups = []
        self.group_calls = []
        self.protection_fail = None

    def protections(self):
        return copy.deepcopy(self.groups)

    def set_protection(self, group, enabled):
        saved = json.loads((self.directory / 'state.json').read_text())
        assert saved['journal']['protections'], 'Protection mutation without durable journal'
        if enabled:
            assert not any(d['identity'] in (c.TARGET_ID, c.TARGET_UUID) and d['connected']
                           for d in self.devices), 'Protection restored before virtual off'
        self.group_calls.append((group['tag'], enabled))
        if self.protection_fail and self.protection_fail(group, enabled):
            raise RuntimeError('Injected protection failure')
        next(g for g in self.groups if g['tag'] == group['tag'])['enabled'] = enabled

    def inventory(self):
        return copy.deepcopy(self.devices)

    def discard_virtual(self, original):
        matches = [d for d in self.devices if d['identity'] == original['identity']]
        if not matches:
            return
        assert len(matches) == 1 and matches[0]['kind'] == 'VirtualScreen'
        assert matches[0]['name'] == original['name'] and not matches[0]['connected']
        assert any(d['connected'] for d in self.devices), 'No safe output during virtual cleanup'
        if self.directory:
            saved = json.loads((self.directory / 'state.json').read_text())
            assert original['identity'] in saved['journal']['transient_virtuals'], 'Deletion before durable ownership'
        fields = {'discard': True}
        self.calls.append((original['identity'], fields))
        if self.fail and self.fail(matches[0], fields):
            raise RuntimeError('Injected temporary deletion failure')
        self.devices.remove(matches[0])

    def set(self, device, **fields):
        if self.directory:
            saved = json.loads((self.directory / 'state.json').read_text())
            assert saved['journal'] or saved.get('detached'), 'Mutation happened before durable journal'
        assert not any(group['enabled'] for group in self.groups), 'Layout protection not suspended'
        self.calls.append((device['identity'], dict(fields)))
        if self.fail and self.fail(device, fields):
            raise RuntimeError('Injected failure')
        real = next(d for d in self.devices if d['identity'] == device['identity'])
        if fields.get('main'):
            for d in self.devices:
                d['main'] = False
        if self.ignore_disconnect and fields.get('connected') is False and device['kind'] == 'Display':
            return
        real.update(fields)
        assert any(d['connected'] for d in self.devices), 'Blanked every screen'


class ControllerTests(unittest.TestCase):
    def test_missing_original_main_restores_remaining_screen_at_zero_and_preserves_original(self):
        secondary = display('secondary', True, False)
        secondary['placement'] = '-800x0'
        self.bd.devices.append(secondary)
        self.enable()
        saved = copy.deepcopy(self.engine.state['journal']['physical'])
        self.bd.devices = [d for d in self.bd.devices if d['identity'] != 'physical1']
        self.engine.tick(dict(timestamp=self.now, state='disconnected', trusted=True),
                         {'id': 'pause-missing-main', 'action': 'pause'})
        restored = next(d for d in self.bd.devices if d['identity'] == 'secondary')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(restored['connected'])
        self.assertTrue(restored['main'])
        self.assertEqual(restored['placement'], '0x0')
        self.assertFalse(next(d for d in self.bd.devices if d['kind'] == 'VirtualScreen')['connected'])
        self.assertEqual(self.engine.state['detached']['physical1'], saved['physical1'])
        self.assertEqual(saved['secondary']['placement'], '-800x0')

    def test_missing_main_translates_remaining_layout_without_changing_relative_positions(self):
        left, right = display('left', True, False), display('right', True, False)
        left['placement'], right['placement'] = '-800x100', '1920x-300'
        self.bd.devices.extend([left, right])
        self.enable()
        self.bd.devices = [d for d in self.bd.devices if d['identity'] != 'physical1']
        self.engine.tick(dict(timestamp=self.now, state='disconnected', trusted=True),
                         {'id': 'pause-relative-layout', 'action': 'pause'})
        restored = {d['identity']: d for d in self.bd.devices}
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(restored['left']['main'])
        self.assertEqual(restored['left']['placement'], '0x0')
        self.assertEqual(restored['right']['placement'], '2720x-400')
        self.assertFalse(restored['right']['main'])
        self.assertIn('physical1', self.engine.state['detached'])

    def test_unknown_display_event_recovers_offline_target_immediately(self):
        self.event_engine()
        self.enable()
        self.bd.devices[1]['connected'] = False
        self.tick('unknown', trusted=False, display_revision=2, run_id='run')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertIsNone(self.engine.state['journal'])

    def test_unknown_display_event_recovers_missing_target_identity(self):
        self.event_engine()
        self.enable()
        self.bd.devices.pop(1)
        self.tick('unknown', display_revision=2)
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_unknown_display_event_does_not_hide_hotplug_or_repair_mode(self):
        self.event_engine()
        self.enable()
        self.bd.devices.append(display('new', True, False))
        self.bd.devices[1]['hiDPI'] = 'off'
        before = len(self.bd.calls)
        self.tick('unknown', display_revision=2)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(len(self.bd.calls), before)
        self.assertTrue(self.bd.devices[-1]['connected'])

    def test_invalid_commands_preserve_healthy_session(self):
        self.enable()
        journal = copy.deepcopy(self.engine.state['journal'])
        before = len(self.bd.calls)
        for command in ([], ['pause'], 'pause', 123, False, {},
                        {'id': 'bad', 'action': 'destroy'},
                        {'id': [], 'action': 'pause'}):
            with self.subTest(command=command):
                self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True), command)
                self.assertEqual(self.engine.state['mode'], 'ACTIVE')
                self.assertEqual(self.engine.state['last_command'], 'enable1')
                self.assertEqual(self.engine.state['journal'], journal)
                result = json.loads((self.directory / 'command-result.json').read_text())
                self.assertEqual(result['status'], 'error')
        self.assertEqual(len(self.bd.calls), before)

    def test_invalid_command_does_not_mask_real_disconnect(self):
        self.enable()
        self.tick('disconnected')
        self.now += c.DISCONNECT_GRACE
        self.engine.tick(dict(timestamp=self.now, state='disconnected', trusted=True), ['bad'])
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])

    def test_invalid_durable_command_does_not_break_session_preparation(self):
        c.atomic_json(self.directory / 'command.json', ['bad'])
        detector, command = c.prepare_session_input(self.directory, self.engine,
            SimpleNamespace(resolve=lambda d: d), {'state': 'unknown'})
        self.engine.tick(detector, command)
        self.assertEqual(self.engine.state['mode'], 'DISABLED')
        self.assertEqual(json.loads((self.directory / 'command-result.json').read_text())['status'], 'error')

    def interrupt_after_virtual_online(self, change):
        original = self.bd.set
        def setting(device, **fields):
            original(device, **fields)
            if device['kind'] == 'VirtualScreen' and fields.get('connected') is True:
                change()
        self.bd.set = setting
        self.enable()
        self.assertFalse(any(identity == 'physical1' and fields.get('connected') is False
                             for identity, fields in self.bd.calls))
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_pause_during_entry_preserves_manual_destination(self):
        self.interrupt_after_virtual_online(lambda: c.atomic_json(
            self.directory / 'command.json', {'id': 'urgent-pause', 'action': 'pause'}))
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertEqual(self.engine.state['last_command'], 'urgent-pause')
        self.assertEqual(self.engine.state['recovery_context']['reason'], 'Manual pause')

    def test_recover_during_entry_preserves_manual_destination(self):
        self.interrupt_after_virtual_online(lambda: c.atomic_json(
            self.directory / 'command.json', {'id': 'urgent-recover', 'action': 'recover'}))
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.engine.state['last_command'], 'urgent-recover')

    def test_verified_disconnect_during_entry_recovers_to_armed(self):
        self.interrupt_after_virtual_online(lambda: c.atomic_json(self.directory / 'signal.json',
            dict(timestamp=self.now, state='disconnected', trusted=True, session_negative_verified=True)))
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.engine.state['desired_enabled'])
        self.assertEqual(self.engine.state['recovery_context']['category'], 'remote_disconnected')

    def test_entry_health_faults_abort_and_recover_without_recursive_checkpoint(self):
        for fields in ({'sleeping': True}, {'trusted': False}, {'timestamp': 0},
                       {'session_health_fault': 'generation_changed'}):
            with self.subTest(fields=fields):
                self.bd.devices = [display(), target()]
                self.bd.calls = []
                self.engine = self.make_engine()
                (self.directory / 'signal.json').unlink(missing_ok=True)
                original = FakeBD.set.__get__(self.bd, FakeBD)
                self.bd.set = original
                def signal_change(fields=fields):
                    value = dict(timestamp=self.now, state='unknown', trusted=True)
                    value.update(fields)
                    c.atomic_json(self.directory / 'signal.json', value)
                # Use a new command ID for each independent activation.
                self.engine.state['last_command'] = None
                self.interrupt_after_virtual_online(signal_change)
                self.assertEqual(self.engine.state['mode'], 'ARMED')
                self.assertFalse(self.engine.entering)
                self.assertIsNone(self.engine.state['journal'])

    def test_fresh_unknown_signal_during_entry_is_not_a_disconnect(self):
        c.atomic_json(self.directory / 'signal.json',
                      dict(timestamp=self.now, state='unknown', trusted=True))
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_pause_is_checked_in_each_inventory_wait_iteration(self):
        original_inventory = self.bd.inventory
        def inventory():
            devices = original_inventory()
            if self.engine.entering and self.bd.devices[1]['connected']:
                devices[1]['connected'] = False
            return devices
        self.bd.inventory = inventory
        def sleeping(seconds):
            self.advance(seconds)
            c.atomic_json(self.directory / 'command.json', {'id': 'in-wait', 'action': 'pause'})
        self.engine.sleeper = sleeping
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.engine.state['last_command'], 'in-wait')
        self.assertFalse(any(identity == 'physical1' and fields.get('connected') is False
                             for identity, fields in self.bd.calls))
        self.assertIsNone(self.engine.state['journal'])

    def test_pause_before_physical_disconnect_takes_priority_over_negative_signal(self):
        original_capture = self.engine._capture
        def capture(devices, initial=False):
            original_capture(devices, initial)
            if not initial:
                c.atomic_json(self.directory / 'command.json', {'id': 'last-moment', 'action': 'pause'})
                c.atomic_json(self.directory / 'signal.json', dict(timestamp=self.now,
                    state='disconnected', trusted=True, session_negative_verified=True))
        self.engine._capture = capture
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.engine.state['recovery_context']['category'], 'manual')
        self.assertFalse(any(identity == 'physical1' and fields.get('connected') is False
                             for identity, fields in self.bd.calls))

    def test_returning_main_restores_entire_original_coordinate_frame_after_restart(self):
        secondary = display('secondary', True, False)
        secondary['resolution'] = '800x1080'
        secondary['placement'] = '-800x100'
        self.bd.devices.append(secondary)
        self.enable()
        originals = copy.deepcopy(self.engine.state['journal']['physical'])
        self.bd.devices = [d for d in self.bd.devices if d['identity'] != 'physical1']
        self.engine.tick(dict(timestamp=self.now, state='disconnected', trusted=True),
                         {'id': 'pause-layout', 'action': 'pause'})
        self.assertEqual(self.engine.state['detached'], originals)
        self.engine = self.make_engine()
        self.tick('disconnected')
        self.assertEqual(self.engine.state['detached'], originals)
        self.bd.devices.append(display())
        original_set = self.bd.set
        def setting(device, **fields):
            original_set(device, **fields)
            if fields.get('main'):
                # Model WindowServer moving all origins when main changes.
                for d in self.bd.devices:
                    if d['identity'] != device['identity']:
                        d['placement'] = '999x999'
        self.bd.set = setting
        self.now += 31
        self.tick('disconnected')
        current = {d['identity']: d for d in self.bd.devices}
        self.assertTrue(c.Engine._matches(list(current.values()), originals))
        self.assertFalse(self.engine.state['detached'])
        self.assertEqual(current['secondary']['placement'], '-800x100')

    def test_new_session_consumes_returned_pending_layout_as_current_baseline(self):
        old = display('physical1')
        self.engine.state['detached'] = {'physical1': old}
        self.bd.devices[0]['resolution'] = '1600x900'
        self.bd.devices[0]['placement'] = '42x24'
        self.enable()
        self.assertFalse(self.engine.state['detached'])
        captured = self.engine.state['journal']['physical']['physical1']
        self.assertEqual(captured['resolution'], '1600x900')
        self.assertEqual(captured['placement'], '42x24')

    def test_new_session_downgrades_absent_old_layout_to_mode_only(self):
        missing = display('old-main')
        missing['resolution'] = '1600x900'
        missing['placement'] = '-800x100'
        self.engine.state['detached'] = {'old-main': missing, 'physical1': display()}
        self.enable()
        self.assertNotIn('physical1', self.engine.state['detached'])
        self.assertTrue(self.engine.state['detached']['old-main']['mode_only'])
        self.engine.tick(dict(timestamp=self.now, state='disconnected', trusted=True),
                         {'id': 'new-pause', 'action': 'pause'})
        returning = display('old-main', True, False)
        returning['placement'] = '2000x300'
        self.bd.devices.append(returning)
        self.tick('disconnected')
        actual = next(d for d in self.bd.devices if d['identity'] == 'old-main')
        self.assertEqual(actual['resolution'], '1600x900')
        self.assertEqual(actual['placement'], '2000x300')
        self.assertFalse(actual['main'])
        self.assertTrue(self.bd.devices[0]['main'])
        self.assertFalse(self.engine.state['detached'])

    def test_missing_layout_retry_is_bounded_and_preserves_pending(self):
        self.engine.state['mode'] = 'PAUSED'
        self.engine.state['detached'] = {'missing': display('missing')}
        self.tick('disconnected')
        self.assertEqual(self.engine.next_timeout({'state': 'disconnected'}), 30)
        self.assertIn('missing', self.engine.state['detached'])

    def test_unverified_negative_during_entry_does_not_abort(self):
        c.atomic_json(self.directory / 'signal.json', dict(timestamp=self.now,
            state='disconnected', trusted=True, reason='ax_home_disconnect_confirmation_pending'))
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_pause_interruption_keeps_journal_when_recovery_fails(self):
        original_set = self.bd.set
        def setting(device, **fields):
            if (device['kind'] == 'VirtualScreen' and fields.get('connected') is False):
                raise RuntimeError('recovery unavailable')
            original_set(device, **fields)
            if device['kind'] == 'VirtualScreen' and fields.get('connected') is True:
                c.atomic_json(self.directory / 'command.json', {'id': 'durable-pause', 'action': 'pause'})
        self.bd.set = setting
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertEqual(self.engine.state['journal']['restore_destination'], 'PAUSED')
        self.assertEqual(self.engine.state['last_command'], 'durable-pause')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertIn('recovery unavailable', self.engine.state['last_error'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.now = 1000.0
        self.bd = FakeBD([display(), target()])
        self.engine = self.make_engine()

    def tearDown(self):
        self.temp.cleanup()

    def make_engine(self):
        self.bd.directory = self.directory
        return c.Engine(self.bd, self.directory, clock=lambda: self.now,
                        monotonic=lambda: self.now, sleeper=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def tick(self, state='connected', **fields):
        signal = dict(timestamp=self.now, session_observed_at=self.now,
                      state=state, trusted=True, sleeping=False)
        signal.update(fields)
        return self.engine.tick(signal)

    def enable(self):
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True),
                         {'id': 'enable1', 'action': 'enable'})

    def event_engine(self):
        self.engine = c.Engine(self.bd, self.directory, clock=lambda: self.now,
                               monotonic=lambda: self.now, sleeper=self.advance,
                               event_driven=True, signal_max_age=90)

    def test_event_heartbeat_is_idle_without_inventory_or_mutations(self):
        self.event_engine()
        self.enable()
        count = self.engine.status()['metrics']['inventory_calls']
        operations = len(self.bd.calls)
        for index in range(4):
            self.now += 30
            self.tick(event='heartbeat', event_id=index)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.status()['metrics']['inventory_calls'], count)
        self.assertEqual(len(self.bd.calls), operations)

    def test_cached_probe_does_not_extend_unknown_grace(self):
        self.event_engine()
        self.enable()
        stamp = self.now
        for seconds in (30, 60, 89):
            self.now = stamp + seconds
            self.tick(timestamp=stamp, session_verification={'probe_timestamp': stamp})
            self.assertEqual(self.engine.last_good, stamp)
        self.now = stamp + 90
        self.tick('unknown', reason='uu_windows_unavailable')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_pause_arriving_during_probe_prevents_remote_entry(self):
        self.event_engine()
        self.engine.tick(dict(timestamp=self.now, state='disconnected', trusted=True),
                         {'id': 'enable1', 'action': 'enable'})
        c.atomic_json(self.directory / 'command.json', {'id': 'enable1', 'action': 'enable'})
        def probe(_):
            c.atomic_json(self.directory / 'command.json', {'id': 'urgent', 'action': 'pause'})
            return dict(timestamp=self.now, state='connected', trusted=True)
        detector, command = c.prepare_session_input(self.directory, self.engine,
            SimpleNamespace(resolve=probe), dict(timestamp=self.now, state='unknown', trusted=True))
        self.engine.tick(detector, command)
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.engine.state['last_command'], 'urgent')
        self.assertEqual(self.bd.calls, [])

    def test_event_display_change_captures_hotplug_once(self):
        self.event_engine()
        self.enable()
        self.bd.devices.append(display('late', True, False))
        self.tick(event='display', event_id='display1')
        self.assertIn('late', self.engine.state['journal']['physical'])
        self.assertFalse(self.bd.devices[-1]['connected'])
        count = self.engine.status()['metrics']['inventory_calls']
        self.tick(event='display', event_id='display1')
        self.assertEqual(self.engine.status()['metrics']['inventory_calls'], count)

    def test_event_signal_lease_expiration_restores_and_waits_for_verified_reconnection(self):
        self.event_engine()
        self.enable()
        signal = dict(timestamp=self.now, state='connected', trusted=True, event='heartbeat')
        self.now += 89
        self.engine.tick(signal)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.next_timeout(signal), 1)
        self.now += 1
        self.engine.tick(signal)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertTrue(self.engine.state['desired_enabled'])
        self.assertEqual(self.engine.status()['waiting_for'], 'trusted UU connection')
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_event_transient_mode_snapshot_settles_without_recovery(self):
        self.event_engine(); self.enable()
        original = self.bd.inventory
        count = [0]
        def transient():
            values = original(); count[0] += 1
            if count[0] == 1:
                values[1]['hiDPI'] = 'off'
            return values
        self.bd.inventory = transient
        self.tick(event='display', event_id='mode-change')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertFalse(self.bd.devices[0]['connected'])
        self.assertGreaterEqual(count[0], 2)

    def test_event_mode_change_is_corrected_without_restoring_physical_displays(self):
        self.event_engine(); self.enable(); start = self.now
        journal = copy.deepcopy(self.engine.state['journal'])
        self.bd.devices[1]['hiDPI'] = 'off'
        self.bd.calls.clear()
        self.tick(event='display', event_id='bad-mode')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertFalse(self.bd.devices[0]['connected'])
        self.assertTrue(self.engine.state['desired_enabled'])
        self.assertEqual(self.bd.devices[1]['resolution'], '1376x1032')
        self.assertEqual(self.bd.devices[1]['hiDPI'], 'on')
        self.assertEqual(len(self.bd.calls), 1)
        self.assertEqual(self.bd.calls[0][0], c.TARGET_UUID)
        self.assertEqual(self.bd.calls[0][1].get('hiDPI'), 'on')
        self.assertTrue(all(key in c.MODE and value == c.MODE[key]
                            for key, value in self.bd.calls[0][1].items()))
        self.assertEqual(self.engine.state['journal'], journal)
        self.assertAlmostEqual(self.now-start, 3)
        self.assertEqual(json.loads((self.directory / 'state.json').read_text())['mode_repair_attempts'],
                         [self.now])

    def test_event_failed_mode_correction_restores_and_pauses(self):
        self.event_engine(); self.enable(); start = self.now
        self.bd.devices[1]['hiDPI'] = 'off'
        self.bd.fail = lambda d, f: d['kind'] == 'VirtualScreen' and ('resolution' in f or 'hiDPI' in f)
        self.tick(event='display', event_id='failed-correction')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertGreaterEqual(self.now - start, 5)  # 3s quality grace + two 1s recovery checks.
        self.assertLess(self.now - start, 5.3)

    def test_event_ignored_mode_correction_has_bounded_verification(self):
        self.event_engine(); self.enable(); start = self.now
        original = self.bd.inventory
        def bad_mode():
            values = original()
            if values[1]['connected']:
                values[1]['hiDPI'] = 'off'
            return values
        self.bd.inventory = bad_mode
        self.tick(event='display', event_id='ignored-correction')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertGreaterEqual(self.now - start, 10)  # Includes bounded physical stabilization.
        self.assertLess(self.now - start, 10.3)

    def test_event_repeated_mode_changes_exhaust_durable_budget(self):
        self.event_engine(); self.enable()
        for index in range(3):
            self.bd.devices[1]['hiDPI'] = 'off'
            self.tick(event='display', event_id='repeated-mode-' + str(index))
            self.assertEqual(self.engine.state['mode'], 'ACTIVE' if index < 2 else 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertIn('budget', self.engine.state['last_error'])
        self.engine = self.make_engine()
        self.assertEqual(len(self.engine.state['mode_repair_attempts']), 2)

    def test_event_mode_repair_budget_expires(self):
        self.event_engine(); self.enable()
        for index in range(3):
            self.bd.devices[1]['hiDPI'] = 'off'
            self.tick(event='display', event_id='spaced-mode-' + str(index))
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            self.now += 61

    def test_first_activation_uses_default_virtual_resolution(self):
        self.enable()
        self.assertEqual(self.bd.devices[1]['resolution'], '1376x1032')
        self.assertEqual(self.bd.devices[1]['hiDPI'], 'on')
        self.assertEqual(self.bd.devices[1]['refreshRate'], '60')

    def test_valid_user_resolutions_are_confirmed_without_mode_mutation_or_budget(self):
        self.event_engine(); self.enable()
        journal = copy.deepcopy(self.engine.state['journal'])
        for index, resolution in enumerate(('1024x768', '1152x864', '1200x900', '1280x960')):
            with self.subTest(resolution=resolution):
                self.bd.devices[1]['resolution'] = resolution
                self.bd.calls.clear()
                start = self.now
                self.tick(event='display', event_id='user-resolution-' + str(index))
                self.tick(event='session', event_id='confirmed-resolution-' + str(index))
                self.assertEqual(self.engine.state['mode'], 'ACTIVE')
                self.assertEqual(self.engine.state['preferred_virtual_mode'],
                                 dict(resolution=resolution, hiDPI='on', refreshRate='60'))
                self.assertEqual(self.bd.devices[1]['resolution'], resolution)
                self.assertEqual(self.engine._verified_target()['resolution'], resolution)
                self.assertEqual(self.bd.calls, [])
                self.assertFalse(self.bd.devices[0]['connected'])
                self.assertTrue(self.engine.state['desired_enabled'])
                self.assertEqual(self.engine.state['journal'], journal)
                self.assertEqual(self.engine.state.get('mode_repair_attempts', []), [])
                self.assertAlmostEqual(self.now - start, .15)
                saved = json.loads((self.directory / 'state.json').read_text())
                self.assertEqual(saved['preferred_virtual_mode'],
                                 self.engine.state['preferred_virtual_mode'])
                self.assertEqual(saved.get('mode_repair_attempts', []), [])

    def test_user_resolution_preference_survives_disconnect_and_controller_rebuild(self):
        self.event_engine(); self.enable()
        self.bd.devices[1]['resolution'] = '1152x864'
        self.tick(event='display', event_id='preferred-resolution')
        self.tick(event='session', event_id='preferred-resolution-proof')
        preferred = dict(resolution='1152x864', hiDPI='on', refreshRate='60')
        self.tick('disconnected', event='session', event_id='disconnect')
        self.now += c.DISCONNECT_GRACE
        self.tick('disconnected', event='heartbeat')
        self.tick('disconnected', event='heartbeat')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(self.engine.state['preferred_virtual_mode'], preferred)
        self.engine = self.make_engine()
        self.assertEqual(self.engine.state['preferred_virtual_mode'], preferred)
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.bd.devices[1]['resolution'], '1152x864')
        self.assertEqual(self.engine._verified_target()['resolution'], '1152x864')
        self.assertEqual(self.engine.state.get('mode_repair_attempts', []), [])

    def test_continuous_valid_resolution_bursts_never_spend_quality_budget(self):
        self.event_engine(); self.enable()
        original = self.bd.inventory
        count = [0]
        def moving_slider():
            devices = original()
            count[0] += 1
            devices[1]['resolution'] = ('1152x864', '1200x900', '1280x960')[count[0] % 3]
            return devices
        self.bd.inventory = moving_slider
        self.bd.calls.clear()
        self.engine.state['mode_repair_attempts'] = [self.now - 2, self.now - 1]
        budget = list(self.engine.state['mode_repair_attempts'])
        for index in range(3):
            self.tick(event='display', event_id='resolution-burst-' + str(index))
            self.assertEqual(self.engine.state['mode'], 'ACTIVE')
            self.assertFalse(self.bd.devices[0]['connected'])
            self.assertEqual(self.engine.state['mode_repair_attempts'], budget)
            self.assertEqual(self.bd.calls, [])

    def test_resolution_preference_waits_for_post_change_session_observation(self):
        self.event_engine(); self.enable()
        self.bd.devices[1]['resolution'] = '1200x900'
        self.tick(event='display', event_id='select', session_observed_at=self.now - .1)
        self.assertIsNotNone(self.engine.pending_resolution)
        self.assertNotEqual(self.engine._preferred_mode()['resolution'], '1200x900')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertTrue(self.engine.needs_resolution_refresh())
        self.assertFalse(self.engine.needs_resolution_refresh())
        inventory_calls = self.engine.metrics['inventory_calls']
        self.now += .1
        self.tick(event='session', event_id='fresh-connection-proof')
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1200x900')
        self.assertIsNone(self.engine.pending_resolution)
        self.assertEqual(self.engine.metrics['inventory_calls'], inventory_calls)

    def test_new_mode_reads_invalidate_older_uncommitted_resolution(self):
        self.event_engine(); self.enable()
        self.bd.devices[1]['resolution'] = '1200x900'
        self.tick(event='display', event_id='first-choice', session_observed_at=self.now - .1)
        self.assertEqual(self.engine.pending_resolution['resolution'], '1200x900')
        original = self.bd.inventory
        count = [0]
        def changed_modes():
            values = original(); count[0] += 1
            values[1]['resolution'] = ('1152x864', '1280x960')[count[0] % 2]
            return values
        self.bd.inventory = changed_modes
        self.now += .1
        self.tick(event='display', event_id='newer-choices')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertNotEqual(self.engine._preferred_mode()['resolution'], '1200x900')

    def test_throttled_resolution_refresh_has_one_bounded_retry(self):
        self.event_engine(); self.enable()
        self.bd.devices[1]['resolution'] = '1200x900'
        self.tick(event='display', event_id='select', session_observed_at=self.now - .1)
        self.assertTrue(self.engine.needs_resolution_refresh())
        self.assertFalse(self.engine.needs_resolution_refresh())
        signal = dict(timestamp=self.now, state='connected', trusted=True, sleeping=False)
        self.assertAlmostEqual(self.engine.next_timeout(signal), 1.25)
        self.now += 1.25
        self.assertTrue(self.engine.needs_resolution_refresh())
        self.assertFalse(self.engine.needs_resolution_refresh())
        self.assertAlmostEqual(self.engine.next_timeout(signal), 30)

    def test_saved_size_is_applied_after_physical_topology_changes(self):
        self.engine.state['preferred_virtual_mode'] = dict(resolution='1280x960', hiDPI='on', refreshRate='60')
        original_set = self.bd.set
        def topology_transaction(device, **fields):
            original_set(device, **fields)
            if device['identity'] == 'physical1' and fields.get('connected') is False:
                self.bd.devices[1]['resolution'] = '1376x1032'
        self.bd.set = topology_transaction
        self.enable()
        physical_off = next(index for index, (identity, fields) in enumerate(self.bd.calls)
                            if identity == 'physical1' and fields.get('connected') is False)
        size_set = max(index for index, (identity, fields) in enumerate(self.bd.calls)
                       if identity == c.TARGET_UUID and fields.get('resolution') == '1280x960')
        self.assertGreater(size_set, physical_off)
        self.assertEqual(self.bd.devices[1]['resolution'], '1280x960')

    def test_late_topology_mode_reset_settles_before_final_preference(self):
        self.engine.state['preferred_virtual_mode'] = dict(resolution='1280x960', hiDPI='on', refreshRate='60')
        original_set, original_inventory = self.bd.set, self.bd.inventory
        off_at = [None];reset = [False]
        def set_and_record(device, **fields):
            original_set(device, **fields)
            if device['identity'] == 'physical1' and fields.get('connected') is False:
                off_at[0] = self.now
        def late_mode():
            if off_at[0] is not None and self.now - off_at[0] >= .6 and not reset[0]:
                self.bd.devices[1]['resolution'] = '1376x1032'
                reset[0] = True
            return original_inventory()
        self.bd.set, self.bd.inventory = set_and_record, late_mode
        self.enable()
        self.assertTrue(reset[0])
        self.assertGreaterEqual(self.now - off_at[0], 1.6)
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.bd.devices[1]['resolution'], '1280x960')

    def test_initial_uu_mode_restore_is_corrected_once_without_pinning_user_modes(self):
        self.event_engine()
        self.engine.state['preferred_virtual_mode'] = dict(resolution='1280x960', hiDPI='on', refreshRate='60')
        self.enable()
        self.bd.latest_mode_writer = lambda _: 'UURemoteServer'
        self.bd.devices[1]['resolution'] = '1376x1032'
        self.bd.calls.clear()
        self.tick(event='display', event_id='uu-session-reset')
        self.assertEqual(self.bd.devices[1]['resolution'], '1280x960')
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1280x960')
        self.assertTrue(self.engine.initial_uu_mode_corrected)
        self.assertEqual(self.engine.state.get('mode_repair_attempts', []), [])
        self.bd.devices[1]['resolution'] = '1200x900'
        self.bd.calls.clear()
        self.tick(event='display', event_id='next-choice')
        self.tick(event='session', event_id='next-choice-proof')
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1200x900')

    def test_betterdisplay_user_mode_is_respected_during_initial_guard(self):
        self.event_engine();self.enable()
        self.bd.latest_mode_writer = lambda _: 'BetterDisplay'
        self.bd.devices[1]['resolution'] = '1200x900'
        self.bd.calls.clear()
        self.tick(event='display', event_id='user-during-startup')
        self.tick(event='session', event_id='fresh-proof')
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1200x900')

    def test_user_change_during_origin_lookup_supersedes_uu_correction(self):
        self.event_engine();self.enable()
        self.bd.devices[1]['resolution'] = '1280x960'
        def changed_while_reading(device):
            if device['resolution'] == '1280x960':
                self.bd.devices[1]['resolution'] = '1200x900'
                return 'UURemoteServer'
            return 'BetterDisplay'
        self.bd.latest_mode_writer = changed_while_reading
        self.bd.calls.clear()
        self.tick(event='display', event_id='changing')
        self.tick(event='session', event_id='fresh-proof')
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1200x900')

    def test_mode_origin_queries_stop_after_initial_window(self):
        self.event_engine();self.enable()
        def forbidden(_):raise AssertionError('Unexpected steady-state log query')
        self.bd.latest_mode_writer = forbidden
        self.now += 11
        self.bd.devices[1]['resolution'] = '1200x900'
        self.tick(event='display', event_id='normal-choice')
        self.tick(event='session', event_id='fresh-proof')
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1200x900')

    def test_mode_writer_uses_last_matching_display_and_mode_intent(self):
        prefix = '2026-10-31 11:57:46 Df '
        def event(process, display, mode):
            return prefix + process + '[123:abc] SLSCompleteDisplayConfigurationWithOption: Display 0x' + display + ': kCGSDisplayConfigMode ' + mode
        log = '\n'.join([event('UURemoteServer','00000019','11'),
                          event('BetterDisplay','00000019','8'),
                          event('BetterDisplay','00000003','11')])
        self.assertEqual(c.BetterDisplay.mode_writer_from_log(log,25,11),'UURemoteServer')
        log += '\n' + event('BetterDisplay','00000019','11')
        self.assertEqual(c.BetterDisplay.mode_writer_from_log(log,25,11),'BetterDisplay')

    def test_uu_teardown_mode_reset_does_not_replace_saved_user_resolution(self):
        self.event_engine(); self.enable()
        self.bd.devices[1]['resolution'] = '1280x960'
        self.tick(event='display', event_id='user-choice')
        self.tick(event='session', event_id='user-choice-proof')
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1280x960')
        self.bd.devices[1]['resolution'] = '1376x1032'
        self.bd.calls.clear()
        self.tick(event='display', event_id='uu-teardown-reset', session_observed_at=self.now - .1)
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1280x960')
        self.assertEqual(self.bd.calls, [])
        self.tick('disconnected', reason='ax_complete_home_no_local_control', session_negative_verified=True)
        self.assertIsNone(self.engine.pending_resolution)
        self.now += c.DISCONNECT_GRACE
        self.tick('disconnected')
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(self.engine._preferred_mode()['resolution'], '1280x960')
        self.tick(event='session', event_id='reconnect')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.bd.devices[1]['resolution'], '1280x960')

    def test_transient_resolution_is_not_saved_as_preference(self):
        self.event_engine(); self.enable()
        original = self.bd.inventory
        reads = [0]
        persisted = []
        def transient():
            values = original()
            reads[0] += 1
            persisted.append(json.loads((self.directory / 'state.json').read_text()).get(
                'preferred_virtual_mode', {}).get('resolution'))
            if reads[0] == 1:
                values[1]['resolution'] = '1024x768'
            return values
        self.bd.inventory = transient
        self.bd.calls.clear()
        self.tick(event='display', event_id='transient-resolution')
        self.assertGreaterEqual(reads[0], 2)
        self.assertNotIn('1024x768', persisted)
        self.assertNotEqual(self.engine.state.get('preferred_virtual_mode', {}).get('resolution'),
                            '1024x768')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(self.engine.state.get('mode_repair_attempts', []), [])

    def test_quality_repair_preserves_current_valid_user_resolution(self):
        self.event_engine(); self.enable()
        for index, quality in enumerate((dict(hiDPI='off'), dict(refreshRate='30'))):
            with self.subTest(quality=quality):
                self.bd.devices[1].update(resolution='1200x900', hiDPI='on', refreshRate='60')
                self.bd.devices[1].update(quality)
                self.bd.calls.clear()
                start = self.now
                self.tick(event='display', event_id='quality-' + str(index))
                self.tick(event='session', event_id='quality-proof-' + str(index))
                self.assertEqual(self.engine.state['mode'], 'ACTIVE')
                self.assertEqual(self.bd.devices[1]['resolution'], '1200x900')
                self.assertEqual(self.bd.devices[1]['hiDPI'], 'on')
                self.assertEqual(self.bd.devices[1]['refreshRate'], '60')
                self.assertEqual(self.engine.state['preferred_virtual_mode'],
                                 dict(resolution='1200x900', hiDPI='on', refreshRate='60'))
                self.assertFalse(self.bd.devices[0]['connected'])
                self.assertAlmostEqual(self.now - start, 3)
                self.assertTrue(self.bd.calls)
                self.assertTrue(all(identity == c.TARGET_UUID for identity, _ in self.bd.calls))
                self.assertTrue(all(fields.get('resolution', '1200x900') == '1200x900'
                                    for _, fields in self.bd.calls))

    def test_malformed_resolution_is_not_adopted_as_preference(self):
        self.event_engine(); self.enable()
        self.bd.devices[1]['resolution'] = 'unknown'
        self.tick(event='display', event_id='malformed-resolution')
        self.assertNotEqual(self.engine.state.get('preferred_virtual_mode', {}).get('resolution'),
                            'unknown')
        saved = json.loads((self.directory / 'state.json').read_text())
        self.assertNotEqual(saved.get('preferred_virtual_mode', {}).get('resolution'), 'unknown')

    def test_event_mirrored_mode_mismatch_is_not_repaired(self):
        self.event_engine(); self.enable()
        self.bd.devices[1].update(resolution='2752x2064', mirrored=True)
        self.bd.calls.clear()
        self.tick(event='display', event_id='mirrored-mode')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(any('resolution' in f for d, f in self.bd.calls if d == c.TARGET_UUID))

    def test_event_missing_target_does_not_wait_for_mode_settling(self):
        self.event_engine(); self.enable(); start = self.now
        self.bd.devices[1]['connected'] = False
        self.tick(event='display', event_id='lost-target')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        online = next(phase for phase in self.engine.metrics['phases']
                      if phase['phase'] == 'physical displays online')
        self.assertEqual(online['timestamp'], start)  # Restore starts immediately, without quality grace.
        self.assertLess(self.now - start, 2.3)  # Only the two physical stability checks remain.

    def test_display_revision_survives_overwritten_event_kind(self):
        self.event_engine()
        self.enable()
        self.tick(event='heartbeat', event_id=1, run_id='detector1', display_revision=0)
        self.bd.devices.append(display('hotplug', True, False))
        self.tick(event='heartbeat', event_id=3, run_id='detector1', display_revision=1)
        self.assertIn('hotplug', self.engine.state['journal']['physical'])
        self.assertFalse(self.bd.devices[-1]['connected'])
        count = self.engine.status()['metrics']['inventory_calls']
        self.tick(event='heartbeat', event_id=4, run_id='detector1', display_revision=1)
        self.assertEqual(self.engine.status()['metrics']['inventory_calls'], count)

    def test_active_detached_record_does_not_schedule_unexecutable_retry(self):
        self.event_engine()
        self.enable()
        self.engine.state['detached'] = {'absent': display('absent', False, False)}
        self.engine.retry_at = 0
        self.assertEqual(self.engine.next_timeout(dict(timestamp=self.now, state='connected', trusted=True)), 30)

    def test_unknown_after_disconnect_deadline_does_not_spin(self):
        self.event_engine()
        self.enable()
        self.tick('disconnected', event='session')
        self.now += 9
        self.tick('unknown', event='session')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.engine.next_timeout(dict(timestamp=self.now, state='unknown', trusted=True)), 30)

    def test_shutdown_preserves_consumed_enable_command(self):
        self.event_engine()
        self.enable()
        consumed = self.engine.state['last_command']
        self.engine._begin_restore('Controller stopped')
        self.engine.tick({})
        self.assertEqual(self.engine.state['last_command'], consumed)
        self.engine = self.make_engine()
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True),
                         {'id': consumed, 'action': 'enable'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_event_untrusted_fresh_signals_do_not_extend_lease(self):
        self.event_engine()
        self.enable()
        for _ in range(3):
            self.now += 30
            self.tick('unknown', trusted=False, event='heartbeat')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertTrue(self.engine.state['desired_enabled'])

    def test_event_deadlines_cover_disconnect_and_recovery(self):
        self.event_engine()
        self.enable()
        self.tick('disconnected', event='session', event_id='disconnected')
        self.assertEqual(self.engine.next_timeout(dict(timestamp=self.now, state='disconnected', trusted=True)), c.DISCONNECT_GRACE)
        self.now += c.DISCONNECT_GRACE
        self.tick('disconnected', event='heartbeat')
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertEqual(self.engine.next_timeout(), 0)
        self.tick('disconnected', event='heartbeat')
        self.assertEqual(self.engine.state['mode'], 'ARMED')

    def test_fifo_wakes_and_drains_durable_command(self):
        wake = c.WakeFIFO(self.directory)
        try:
            c.atomic_json(self.directory / 'command.json', {'id': 'wake1', 'action': 'enable'})
            c.wake_controller(self.directory)
            self.assertTrue(wake.selector.select(0))
            wake.wait(0)
            self.assertFalse(wake.selector.select(0))
            self.assertEqual(c.read_json(self.directory / 'command.json')['id'], 'wake1')
        finally:
            wake.close()

    def test_native_inventory_cache_refreshes_identity_and_uses_actual_mode(self):
        class NativeBD(c.BetterDisplay):
            def __init__(self):
                super().__init__()
                self.identifier_calls = 0
                self.native = [dict(displayID=3, uuid='physical-uuid', width=1512, height=982,
                                    pixelWidth=3024, pixelHeight=1964, originX=-1512, originY=0,
                                    refreshRate=0, rotation=0, main=True, mirrored=False)]
            def _native_topology(self):
                return self.native
            def _system_inventory(self):
                raise AssertionError('Native inventory must not call system_profiler')
            def _run(self, *args):
                self.identifier_calls += 1
                return json.dumps([dict(deviceType='Display', tagID=2, UUID='physical-uuid',
                                        displayID=3, name='builtin')])
        bd = NativeBD()
        device = bd.inventory()[0]
        self.assertEqual(device['resolution'], '1512x982')
        self.assertEqual(device['placement'], '-1512x0')
        self.assertIsNone(device['refreshRate'])
        bd.inventory()
        self.assertEqual(bd.identifier_calls, 1)
        bd.native[0]['displayID'] = 99  # Native identity stays valid while BD's ID lags.
        self.assertTrue(bd.inventory()[0]['connected'])
        self.assertEqual(bd.identifier_calls, 2)
        bd.native[0]['uuid'] = 'replacement-uuid'
        with self.assertRaisesRegex(RuntimeError, 'UUID mismatch'):
            bd.inventory()
        self.assertEqual(bd.identifier_calls, 3)

    def test_disabled_does_nothing(self):
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'DISABLED')
        self.assertEqual(self.bd.calls, [])

    def test_physical_displays_with_identical_edid_match_their_distinct_uuids(self):
        class NativeBD(c.BetterDisplay):
            def _native_topology(self):
                return [dict(displayID=i, uuid='uuid-' + str(i), vendor=1552, model=99, serial=0,
                             width=1920, height=1080, pixelWidth=3840, pixelHeight=2160,
                             originX=(i-3)*1920, originY=0, refreshRate=60, rotation=0,
                             main=i == 3, mirrored=False) for i in (3, 4)]
            def _run(self, *args):
                return json.dumps([dict(deviceType='Display', tagID=i, UUID='uuid-' + str(i),
                                        displayID=i, vendor=1552, model=99, serial=0,
                                        name='Identical model') for i in (3, 4)])
        values = NativeBD().inventory()
        self.assertEqual({d['identity'] for d in values}, {'uuid-3', 'uuid-4'})
        self.assertTrue(all(d['connected'] for d in values))
        self.assertEqual([d['placement'] for d in values], ['0x0', '1920x0'])

    def test_duplicate_virtual_fingerprint_still_fails_closed(self):
        bd = c.BetterDisplay()
        bd._identifier_cache = [dict(deviceType='VirtualScreen', tagID='198', UUID='UNKNOWN',
                                    vendor=2198, model=55152, serial=1000000001,
                                    name=c.TARGET_NAME, **{'tagID (Display)': '199'})]
        values = [dict(displayID=i, uuid='different-' + str(i), vendor=2198,
                       model=55152, serial=1000000001) for i in (3, 4)]
        bd._topology_key = tuple(sorted((str(v['displayID']), str(v['uuid'])) for v in values))
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous native virtual display fingerprint'):
            bd._native_inventory(values)

    def test_virtual_linked_selector_arrives_after_native_output_without_topology_change(self):
        class NativeBD(c.BetterDisplay):
            def __init__(self):
                super().__init__()
                self.identifier_calls = 0
            def _native_topology(self):
                return [dict(displayID=50, uuid=c.TARGET_UUID, vendor=2198, model=55152,
                             serial=1000000001, width=1376, height=1032, pixelWidth=2752,
                             pixelHeight=2064, originX=0, originY=0, refreshRate=60,
                             rotation=0, main=False, mirrored=False)]
            def _run(self, *args):
                self.identifier_calls += 1
                item = dict(deviceType='VirtualScreen', tagID='198', UUID='UNKNOWN',
                            vendor='2198', model='55152', serial='1000000001', name=c.TARGET_NAME)
                if self.identifier_calls > 1:
                    item['tagID (Display)'] = '199'
                return json.dumps([item])
        bd = NativeBD()
        with self.assertRaisesRegex(RuntimeError, 'linked selector is not ready'):
            bd.inventory()
        self.assertIsNone(bd._identifier_cache)
        live = bd.inventory()[0]
        self.assertTrue(live['connected'])
        self.assertEqual(live['mode_tag'], '199')
        self.assertEqual(bd.identifier_calls, 2)

    def test_virtual_main_never_targets_virtualscreen_selector(self):
        bd = c.BetterDisplay()
        with patch.object(bd, '_run') as run:
            with self.assertRaisesRegex(RuntimeError, 'without linked virtual selector'):
                bd.set(dict(kind='VirtualScreen', identity=c.TARGET_ID, tag='198', mode_tag='198'), main=True)
            run.assert_not_called()
            bd.set(dict(kind='VirtualScreen', identity=c.TARGET_ID, tag='198', mode_tag='199'), main=True)
            run.assert_called_once_with('set', '-tagID=199', '-main=on')

    def test_physical_batch_has_only_exact_scoped_commands(self):
        bd = c.BetterDisplay()
        calls = []
        bd._run = lambda *args: calls.append(args)
        bd.set_connections([display('one'), display('two')], False)
        self.assertCountEqual(calls, [('set', '-tagID=one', '-connected=off'),
                                     ('set', '-tagID=two', '-connected=off')])
        with self.assertRaises(RuntimeError):
            bd.set_connections([display('one'), target()], False)
        with self.assertRaises(RuntimeError):
            bd.set_connections([display('one'), display('one')], False)
        self.assertEqual(len(calls), 2)

    def test_successful_CLI_diagnostics_do_not_inherit_outer_exception(self):
        events = []
        with patch.object(c, '_runtime_logging', True), patch.object(c, 'runtime_event',
                side_effect=lambda *a, **kw: events.append(kw)), patch.object(c.subprocess, 'run',
                return_value=SimpleNamespace(stdout='on')):
            try:
                raise RuntimeError('Outer recovery context')
            except RuntimeError:
                self.assertEqual(c.BetterDisplay()._run('get', '-connected'), 'on')
        self.assertIsNone(events[-1]['error'])

    def test_activate_and_restore_modes_main(self):
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertFalse(self.bd.devices[0]['connected'])
        self.assertTrue(self.bd.devices[1]['main'])
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertTrue(self.bd.devices[0]['main'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertIsNone(self.engine.state['journal'])

    def test_many_physical_and_preexisting_off(self):
        self.bd.devices.insert(1, display('external', True, False))
        self.bd.devices.insert(2, display('off', False, False))
        self.enable()
        self.assertEqual(len(self.engine.state['journal']['physical']), 2)
        self.engine.tick({}, {'id': 'recover1', 'action': 'recover'})
        self.assertFalse(next(d for d in self.bd.devices if d['identity'] == 'off')['connected'])
        self.assertFalse(any(i == 'off' for i, _ in self.bd.calls))

    def test_hotplug_is_journaled_before_hide(self):
        self.enable()
        self.bd.devices.append(display('late', True, False))
        self.tick()
        self.assertIn('late', self.engine.state['journal']['physical'])
        self.engine.tick({}, {'id': 'recover1', 'action': 'recover'})
        self.assertTrue(self.bd.devices[-1]['connected'])

    def test_unrelated_virtual_untouched(self):
        self.bd.devices.append(display('other', True, False, 'VirtualScreen'))
        self.enable()
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertFalse(any(i == 'other' for i, _ in self.bd.calls))

    def test_fast_reconnect_does_not_restore(self):
        self.enable()
        self.tick('disconnected')
        self.now += c.DISCONNECT_GRACE - 0.1
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertFalse(self.bd.devices[0]['connected'])

    def test_disconnect_debounce_rearms(self):
        self.enable()
        self.tick('disconnected')
        self.now += c.DISCONNECT_GRACE
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_crash_recovers_physical_first_then_rearms(self):
        self.enable()
        self.engine = self.make_engine()
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertTrue(self.engine.state['desired_enabled'])
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_untrusted_and_stale_fail_safe(self):
        self.enable()
        self.now += 10
        self.tick('unknown', trusted=False)
        self.now += 6
        self.tick('unknown', trusted=False)
        self.assertEqual(self.engine.state['mode'], 'ARMED')

    def test_sleep_and_clock_gap_pause(self):
        self.enable()
        self.tick(sleeping=True)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.tick(sleeping=True)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.now += 16
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_rejected_internal_disconnect_rolls_back(self):
        self.bd.ignore_disconnect = True
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertIn('rejected', self.engine.state['last_error'])

    def test_no_physical_keeps_fallback_and_backoff(self):
        self.enable()
        self.bd.devices = self.bd.devices[1:]
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertIsNotNone(self.engine.state['journal'])
        calls = len(self.bd.calls)
        for _ in range(20):
            self.now += 1
            self.tick('unknown', trusted=False)
        self.assertEqual(len(self.bd.calls), calls)
        self.bd.devices.insert(0, display())
        self.now += 10
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_unexpected_virtual_disappearance_restores(self):
        self.enable()
        self.bd.devices.pop()
        self.tick()
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_mode_failure_does_not_hide_physical(self):
        self.bd.fail = lambda device, fields: 'resolution' in fields
        self.enable()
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(any(i == 'physical1' and f.get('connected') is False for i, f in self.bd.calls))
        self.assertIsNone(self.engine.state['journal'])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_failed_restore_keeps_virtual_and_journal(self):
        self.enable()
        self.bd.fail = lambda device, fields: device['kind'] == 'Display' and fields.get('connected') is True
        self.engine.tick({}, {'id': 'recover1', 'action': 'recover'})
        self.assertTrue(self.bd.devices[1]['connected'])
        self.assertIsNotNone(self.engine.state['journal'])

    def test_dedicated_virtual_off_after_recovery_even_if_preexisting_on(self):
        self.bd.devices[1]['connected'] = True
        self.enable()
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_duplicate_command_not_reenabled(self):
        self.enable()
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_identity_ambiguity_rejected(self):
        self.bd.devices.append(target())
        self.enable()
        self.assertEqual(self.bd.calls, [])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_enable_while_active_does_not_replace_journal(self):
        self.enable()
        original = copy.deepcopy(self.engine.state['journal'])
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True),
                         {'id': 'enable2', 'action': 'enable'})
        self.assertEqual(self.engine.state['journal'], original)
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertTrue(self.bd.devices[0]['main'])

    def test_stale_timestamp_and_future_timestamp(self):
        self.enable()
        self.now += 10
        self.tick(timestamp=self.now + 100)
        self.now += 6
        self.tick(timestamp=self.now - 100)
        self.assertEqual(self.engine.state['mode'], 'ARMED')

    def test_automatic_wait_rejects_unverified_signals_without_busy_loop(self):
        self.event_engine(); self.enable(); self.now += 90
        old = dict(timestamp=self.now - 90, state='unknown', trusted=True)
        self.engine.tick(old)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        operations = len(self.bd.calls)
        inventory = self.engine.status()['metrics']['inventory_calls']
        for _ in range(4):
            self.now += 30
            self.engine.tick(old)
            self.assertEqual(self.engine.next_timeout(old), 30)
        self.tick('connected', trusted=False)
        self.tick('connected', timestamp=self.now + 100)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(len(self.bd.calls), operations)
        self.assertEqual(self.engine.status()['metrics']['inventory_calls'], inventory)
        self.tick('connected')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_connection_returning_during_automatic_restore_reenters_after_safe_layout(self):
        self.event_engine(); self.enable(); self.now += 90
        arrived = []
        def on_restore(device, fields):
            if device['kind'] == 'Display' and fields.get('connected') is True:
                arrived.append(dict(timestamp=self.now, state='connected', trusted=True))
            return False
        self.bd.fail = on_restore
        self.tick('unknown')
        self.assertTrue(arrived)
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.engine.tick(arrived[-1])
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertFalse(self.bd.devices[0]['connected'])

    def test_manual_pause_during_automatic_recovery_prevents_future_reentry(self):
        self.event_engine(); self.enable(); self.now += 90
        self.bd.fail = lambda device, fields: device['kind'] == 'Display' and fields.get('connected') is True
        self.tick('unknown')
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.engine.state['desired_enabled'])
        self.bd.fail = None
        self.engine.tick({}, {'id': 'manual-during-recovery', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.tick('connected')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.engine = self.make_engine()
        self.tick('connected')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_supervisor_stop_preserves_automatic_intent_but_not_consumed_command(self):
        self.event_engine(); self.enable()
        consumed = self.engine.state['last_command']
        self.engine.stop_for_restart()
        self.engine.tick({})
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertEqual(self.engine.state['last_command'], consumed)
        self.engine = self.make_engine()
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True),
                         {'id': consumed, 'action': 'enable'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_manual_stop_and_prior_manual_pause_never_auto_enable(self):
        for explicit in (True, False):
            self.enable()
            if not explicit:
                self.engine.tick({}, {'id': 'manual-before-stop', 'action': 'pause'})
            self.engine.stop_for_restart(preserve_intent=not explicit)
            self.engine.tick({})
            self.tick('connected')
            self.assertEqual(self.engine.state['mode'], 'PAUSED')
            self.assertFalse(self.engine.state['desired_enabled'])
            self.engine = self.make_engine()

    def test_durable_target_identity_with_unknown_uuid(self):
        self.bd.devices[1]['identity'] = c.TARGET_ID
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')

    def test_refresh_rate_hz_representation(self):
        self.bd.devices[1].update(connected=True, resolution='1376x1032', hiDPI='on', refreshRate='60Hz')
        self.assertTrue(self.engine._verified_target())
        self.bd.devices[1]['refreshRate'] = '59.94Hz'
        self.assertTrue(self.engine._verified_target())
        self.bd.devices[1]['refreshRate'] = '30Hz'
        with self.assertRaises(RuntimeError):
            self.engine._verified_target()

    def test_adapter_unknown_virtual_uuid_canonical_and_hz(self):
        class ReadOnlyBD(c.BetterDisplay):
            def _native_topology(self):
                return None

            def _native_origins(self):
                return {'21': '-800x0'}

            def _system_inventory(self):
                return {'21': {'spdisplays_online': 'spdisplays_yes',
                               'spdisplays_resolution': '1376 x 1032 @ 60.00Hz',
                               '_spdisplays_pixels': '2752 x 2064'}}

            def _run(self, *args):
                if args == ('get', '-identifiers'):
                    return json.dumps({'deviceType': 'VirtualScreen', 'tagID': '198',
                                       'tagID (Display)': '199', 'UUID': 'UNKNOWN', 'displayID': '21',
                                       'vendor': '2198', 'model': '55152',
                                       'serial': '1000000001', 'name': c.TARGET_NAME})
                field = args[-1][1:]
                return {'connected': 'on', 'main': 'on', 'resolution': '1376x1032',
                        'hiDPI': 'on', 'rotation': '0', 'placement': '0x0',
                        'refreshRate': '59.94Hz'}[field]
        device = ReadOnlyBD().inventory()[0]
        self.assertEqual(device['identity'], c.TARGET_ID)
        self.assertEqual(device['refreshRate'], '60')
        self.assertEqual(device['hiDPI'], 'on')
        self.assertEqual(device['mode_source'], 'system_profiler')
        self.assertEqual(device['mode_tag'], '199')
        self.assertEqual(device['placement'], '-800x0')
        self.assertEqual(device['placement_source'], 'native-topology')

    def test_boolean_csv_only_accepts_unanimous_valid_values(self):
        self.assertTrue(c.parse_bool('on,on'))
        self.assertFalse(c.parse_bool('off,false'))
        for value in ('on,off', 'garbage', '', 'on,'):
            with self.assertRaises(RuntimeError):
                c.parse_bool(value)

    def test_detached_display_retained_and_restored_after_return(self):
        external = display('external', True, False)
        external.update(resolution='2560x1440', placement='1920x0', rotation='90')
        self.bd.devices.append(external)
        self.enable()
        self.bd.devices = [d for d in self.bd.devices if d['identity'] != 'external']
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertIn('external', self.engine.state['detached'])
        self.bd.devices.append(display('external', False, False))
        self.tick()
        self.assertTrue(self.bd.devices[-1]['connected'])
        self.assertEqual(self.bd.devices[-1]['rotation'], '90')
        self.assertEqual(self.bd.devices[-1]['placement'], '1920x0')
        self.assertFalse(self.engine.state['detached'])

    def test_late_main_does_not_replace_baseline_session_main(self):
        self.enable()
        for device in self.bd.devices:
            device['main'] = False
        self.bd.devices.append(display('late', True, True))
        self.tick()
        snapshots = self.engine.state['journal']['physical']
        self.assertTrue(snapshots['physical1']['main'])
        self.assertFalse(snapshots['late']['main'])
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['main'])
        self.assertFalse(self.bd.devices[-1]['main'])

    def test_initial_mirroring_pauses_without_mutation_or_journal(self):
        self.bd.devices[0]['mirrored'] = True
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertIsNone(self.engine.state['journal'])
        self.assertFalse(self.bd.calls)
        self.assertIn('Mirrored', self.engine.state['last_error'])

    def test_late_mirrored_hotplug_restores_instead_of_hiding_it(self):
        self.enable()
        self.bd.devices.append(dict(display('late', True, False), mirrored=True))
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertTrue(self.bd.devices[-1]['connected'])
        self.assertFalse(any(identity == 'late' for identity, fields in self.bd.calls))

    def test_slow_transition_is_not_a_sleep_gap(self):
        enter = self.engine._enter
        def slow_enter():
            enter()
            self.now += 40
        self.engine._enter = slow_enter
        self.enable()
        self.now += 1
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_duplicate_identity_with_other_name_is_rejected(self):
        duplicate = target()
        duplicate['name'] = 'Other virtual'
        duplicate['tag'] = 'other-tag'
        self.bd.devices.append(duplicate)
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.bd.calls)

    def test_group_protection_suspended_and_exact_original_restored(self):
        self.bd.groups = [{'tag': '-1001', 'enabled': True},
                          {'tag': '-1002', 'enabled': False}]
        self.enable()
        self.assertEqual(self.bd.group_calls, [('-1001', False)])
        self.assertFalse(self.bd.groups[0]['enabled'])
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertEqual(self.bd.group_calls, [('-1001', False), ('-1001', False), ('-1001', True)])
        self.assertTrue(self.bd.groups[0]['enabled'])
        self.assertFalse(self.bd.groups[1]['enabled'])

    def test_group_protection_restored_on_failed_entry(self):
        self.bd.groups = [{'tag': '-1001', 'enabled': True}]
        self.bd.fail = lambda d, f: d['kind'] == 'VirtualScreen' and f.get('main') is True
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.groups[0]['enabled'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_group_protection_crash_recovery_and_failure_journal(self):
        self.bd.groups = [{'tag': '-1001', 'enabled': True}]
        self.enable()
        self.engine = self.make_engine()
        self.bd.protection_fail = lambda group, enabled: enabled
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertIsNotNone(self.engine.state['journal'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.bd.protection_fail = None
        self.now += 30
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.groups[0]['enabled'])

    def test_crash_after_protection_reenabled_suspends_again_before_restore(self):
        self.bd.groups = [{'tag': '-1001', 'enabled': True}]
        self.enable()
        # Crash window after virtual-off/protection-on, before journal clearing.
        original = self.engine.state['journal']['physical']['physical1']
        self.bd.devices[0].update(copy.deepcopy(original))
        self.bd.devices[1]['connected'] = False
        self.bd.groups[0]['enabled'] = True
        self.engine = self.make_engine()
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertTrue(self.bd.groups[0]['enabled'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertEqual(self.bd.group_calls[-2:], [('-1001', False), ('-1001', True)])

    def test_delayed_physical_online_and_transient_system_inventory(self):
        self.enable()
        original_set = self.bd.set
        original_inventory = self.bd.inventory
        due = {}
        failures = [3]
        def delayed_set(device, **fields):
            original_set(device, **fields)
            if device['kind'] == 'Display' and fields.get('connected') is True:
                real = next(d for d in self.bd.devices if d['identity'] == device['identity'])
                real['connected'] = False
                due[device['identity']] = self.now + 7.5
        def delayed_inventory():
            if failures[0]:
                failures[0] -= 1
                raise RuntimeError('System display inventory unavailable')
            for device in self.bd.devices:
                if device['identity'] in due and self.now >= due[device['identity']]:
                    device['connected'] = True
            return original_inventory()
        self.bd.set = delayed_set
        self.bd.inventory = delayed_inventory
        start = self.now
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertGreaterEqual(self.now - start, 9)
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])
        self.assertIsNone(self.engine.state['journal'])

    def test_delayed_virtual_online_main_and_watchdog_heartbeat(self):
        original_set = self.bd.set
        original_inventory = self.bd.inventory
        pending = []
        observed_waits = []
        original_advance = self.advance
        def observe_sleep(seconds):
            status = json.loads((self.directory / 'status.json').read_text())
            observed_waits.append(status['waiting_for'])
            original_advance(seconds)
        self.engine.sleeper = observe_sleep
        def delayed_set(device, **fields):
            real = next(d for d in self.bd.devices if d['identity'] == device['identity'])
            before = copy.deepcopy(real)
            original_set(device, **fields)
            if device['kind'] == 'VirtualScreen' and (fields.get('connected') is True or fields.get('main')):
                real.update(before)
                pending.append((self.now + 2, real, dict(fields)))
        def delayed_inventory():
            for item in list(pending):
                due, real, fields = item
                if self.now >= due:
                    if fields.get('main'):
                        for d in self.bd.devices:
                            d['main'] = False
                    real.update(fields)
                    pending.remove(item)
            return original_inventory()
        self.bd.set = delayed_set
        self.bd.inventory = delayed_inventory
        self.enable()
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertIn('virtual display online', observed_waits)
        self.assertIn('virtual main display', observed_waits)
        self.assertEqual(json.loads((self.directory / 'status.json').read_text())['updated_at'], self.now)

    def test_recovery_does_not_reapply_unchanged_modes_or_placement(self):
        self.enable()
        self.bd.calls.clear()
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        physical_calls = [fields for identity, fields in self.bd.calls if identity == 'physical1']
        self.assertFalse(any(any(field in fields for field in c.FIELDS) for fields in physical_calls))
        self.assertEqual(physical_calls, [{'connected': True}, {'main': True}])

    def test_recovery_restores_each_changed_mode_and_placement(self):
        self.enable()
        self.bd.devices[0].update(resolution='800x600', hiDPI='on', rotation='90',
                                  refreshRate='30', placement='50x50')
        self.bd.calls.clear()
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        physical_calls = [fields for identity, fields in self.bd.calls if identity == 'physical1']
        mode_call = next(fields for fields in physical_calls if 'resolution' in fields)
        self.assertEqual(mode_call, {'resolution': '1920x1080', 'hiDPI': 'off',
                                     'rotation': '0', 'refreshRate': '60'})
        self.assertIn({'placement': '0x0'}, physical_calls)
        self.assertNotIn('placement', mode_call)

    def test_builtin_adaptive_or_unreported_refresh_is_not_forced(self):
        for suffix in (' Retina', ' @ 0.00Hz'):
            with self.subTest(suffix=suffix):
                class BuiltinBD(c.BetterDisplay):
                    def _native_topology(self):
                        return None

                    def _native_origins(self):
                        return {'3': '0x0'}

                    def _system_inventory(self):
                        return {'3': {'spdisplays_online': 'spdisplays_yes',
                                      'spdisplays_main': 'spdisplays_yes',
                                      'spdisplays_resolution': '1512 x 982' + suffix,
                                      '_spdisplays_pixels': '3024 x 1964'}}

                    def _run(self, *args):
                        if args == ('get', '-identifiers'):
                            return json.dumps({'deviceType': 'Display', 'tagID': '2',
                                               'UUID': 'builtin-uuid', 'displayID': '3',
                                               'name': 'Built-in Liquid Retina XDR'})
                        raise AssertionError('Unexpected CLI query')
                device = BuiltinBD().inventory()[0]
                self.assertEqual(device['resolution'], '1512x982')
                self.assertEqual(device['hiDPI'], 'on')
                self.assertIsNone(device['refreshRate'])
                self.assertTrue(device['refreshRate_unreported_or_adaptive'])
                self.assertNotIn('refreshRate', c.Engine._mode_changes(device, device))
                changed = dict(device, refreshRate='60')
                self.assertTrue(c.Engine._matches([changed], {device['identity']: device}))

    def test_normal_disconnect_restore_transient_retry_preserves_rearm(self):
        self.enable()
        self.tick('disconnected')
        self.now += c.DISCONNECT_GRACE
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        original_restore = self.engine._restore
        calls = [0]
        def flaky_restore():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError('Temporary identifier inventory timeout')
            return original_restore()
        self.engine._restore = flaky_restore
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertEqual(self.engine.after_restore, 'ARMED')
        self.assertEqual(self.engine.retry_at, self.now + 5)
        self.now += 4
        self.tick('connected')
        self.assertEqual(calls[0], 1)
        self.now += 1
        self.tick('disconnected')
        self.assertEqual(self.engine.state['mode'], 'ARMED')
        self.assertIsNone(self.engine.state['last_error'])
        self.assertEqual(self.engine.retry_at, 0)
        self.tick('connected')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_runtime_events_are_append_only_durable_json_lines(self):
        self.assertTrue(c.runtime_event(self.directory, 'process_start', python='test'))
        self.assertTrue(c.runtime_event(self.directory, 'process_exit', reason='exception',
                                       traceback='traceback example'))
        events = [json.loads(line) for line in (self.directory / 'runtime-events.jsonl').read_text().splitlines()]
        self.assertEqual([event['event'] for event in events], ['process_start', 'process_exit'])
        self.assertEqual(events[1]['traceback'], 'traceback example')
        self.assertTrue(all('pid' in event and 'timestamp' in event for event in events))

    def test_manual_pause_latch_survives_restart(self):
        self.enable()
        self.engine.tick({}, {'id': 'pause1', 'action': 'pause'})
        self.assertFalse(self.engine.state['desired_enabled'])
        self.engine = self.make_engine()
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_manual_recovery_crash_keeps_paused_destination(self):
        self.enable()
        self.engine._begin_restore('Manual recover')
        self.engine = self.make_engine()
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])

    def test_three_interrupted_restarts_in_five_minutes_disable_intent(self):
        self.enable()
        for _ in range(3):
            self.now += 1
            self.engine = self.make_engine()
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertEqual(self.engine.after_restore, 'PAUSED')
        self.assertIn('budget', self.engine.state['last_error'])
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_restart_budget_expires_and_fatal_entry_disables_intent(self):
        self.enable()
        self.engine = self.make_engine()
        self.now += 301
        self.engine = self.make_engine()
        self.assertEqual(len(self.engine.state['interrupted_restarts']), 1)
        self.assertEqual(self.engine.after_restore, 'ARMED')
        self.tick()
        self.bd.devices[1]['name'] = 'Wrong target'
        self.tick()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])

    def test_corrupt_state_is_preserved(self):
        path = self.directory / 'state.json'
        path.write_text('broken')
        with self.assertRaises(ValueError):
            self.make_engine()
        self.assertEqual(path.read_text(), 'broken')

    def test_identifier_stream_parse(self):
        self.assertEqual(c.parse_identifiers('{"tagID":"1"},{"tagID":"2"}'),
                         [{'tagID': '1'}, {'tagID': '2'}])
        with self.assertRaises(RuntimeError):
            c.parse_identifiers('')


if __name__ == '__main__':
    unittest.main()
