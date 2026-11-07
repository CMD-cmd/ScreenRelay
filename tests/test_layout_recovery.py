"""Physical restoration with WindowServer-style coordinate constraints."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_controller import c, FakeBD, display, target


class SnappingBD(FakeBD):
    def set(self, device, **fields):
        super().set(device, **fields)
        # The real incident: CLI succeeds, but the old virtual-width origin is
        # snapped to the wider physical main's edge by the display server.
        if device['identity'] == 'builtin' and fields.get('placement') == '1376x50':
            next(d for d in self.devices if d['identity'] == 'builtin')['placement'] = '1920x50'


class LayoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.now = 1000.0
        self.bd = SnappingBD([display(), target()])
        self.engine = self.make_engine()

    def tearDown(self):
        self.temp.cleanup()

    def make_engine(self):
        self.bd.directory = self.directory
        return c.Engine(self.bd, self.directory, clock=lambda: self.now,
                        monotonic=lambda: self.now, sleeper=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def enable(self):
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True),
                         {'id': 'enable', 'action': 'enable'})
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def pause(self):
        self.engine.tick({}, {'id': 'pause', 'action': 'recover'})

    def builtin(self, placement='1376x50'):
        result = display('builtin', True, False)
        result.update(resolution='1512x982', placement=placement, hiDPI='on', refreshRate='120')
        return result

    def incident(self):
        left = display('left', True, False)
        left.update(resolution='800x1280', rotation='90', placement='-800x0', refreshRate='165')
        self.bd.devices.insert(1, left)
        self.enable()
        self.bd.devices.append(self.builtin())
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True))
        self.assertIn('builtin', self.engine.state['journal']['physical'])
        return self.engine.state['journal']['physical']

    def test_real_three_screen_incident_completes_and_keeps_original_snapshot(self):
        self.bd.groups = [{'tag': 'layout', 'enabled': True}]
        captured = self.incident()
        self.assertTrue(captured['builtin']['captured_during_remote'])
        # Previously saved journals have no provenance flag; they must recover.
        captured['builtin'].pop('captured_during_remote')
        saved = copy.deepcopy(captured)
        self.engine._save()
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertIsNone(self.engine.state['journal'])
        self.assertIsNone(self.engine.state['last_error'])
        current = {d['identity']: d for d in self.bd.devices}
        self.assertEqual(current['builtin']['placement'], '1920x50')
        self.assertEqual(current['left']['placement'], '-800x0')
        self.assertEqual(current['left']['resolution'], '800x1280')
        self.assertTrue(current['physical1']['main'])
        self.assertFalse(current[c.TARGET_UUID]['connected'])
        self.assertEqual(self.bd.group_calls[-1], ('layout', True))
        self.assertEqual(captured, saved)
        self.assertNotIn(('builtin', {'placement': '1376x50'}), self.bd.calls)

    def test_valid_saved_layout_is_not_replaced_by_arbitrary_current_layout(self):
        self.bd.devices.append(self.builtin('1920x50'))
        self.enable()
        builtin = next(d for d in self.bd.devices if d['identity'] == 'builtin')
        builtin['placement'] = '5000x50'
        original_set = self.bd.set
        def ignore_placement(device, **fields):
            if device['identity'] == 'builtin' and 'placement' in fields:
                return
            return original_set(device, **fields)
        self.bd.set = ignore_placement
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.engine.state['journal'])
        self.assertTrue(next(d for d in self.bd.devices if d['kind'] == 'VirtualScreen')['connected'])
        self.assertIn('physical display layout', self.engine.state['last_error'])

    def test_first_physical_display_arriving_during_remote_becomes_zero_origin_main(self):
        self.bd.devices = [target()]
        self.enable()
        self.bd.devices.append(self.builtin())
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True))
        captured = self.engine.state['journal']['physical']
        saved = copy.deepcopy(captured)
        self.assertTrue(captured['builtin']['main'])
        self.pause()
        current = next(d for d in self.bd.devices if d['identity'] == 'builtin')
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertTrue(current['main'])
        self.assertEqual(current['placement'], '0x0')
        self.assertEqual(captured, saved)

    def test_detached_return_avoids_current_physical_outputs_without_moving_them(self):
        self.bd.devices.append(self.builtin('1920x50'))
        pending = self.builtin()
        self.engine.state['detached'] = {'builtin': pending}
        saved = copy.deepcopy(pending)
        self.engine._save()
        self.bd.calls.clear()
        self.engine._restore_detached(self.now)
        self.assertFalse(self.engine.state['detached'])
        self.assertEqual(pending, saved)
        self.assertEqual(self.bd.devices[-1]['placement'], '1920x50')
        self.assertFalse(any(identity == 'physical1' for identity, _ in self.bd.calls))

    def test_normalized_coordinates_do_not_hide_mode_failure(self):
        self.incident()
        builtin = next(d for d in self.bd.devices if d['identity'] == 'builtin')
        builtin['resolution'] = '800x600'
        self.bd.fail = lambda device, fields: device['identity'] == 'builtin' and 'resolution' in fields
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.engine.state['journal'])
        self.assertTrue(next(d for d in self.bd.devices if d['kind'] == 'VirtualScreen')['connected'])

    def test_virtual_disconnect_mode_writeback_is_repaired_before_protection(self):
        self.bd.devices.append(self.builtin('1920x50'))
        self.bd.groups = [{'tag': 'layout', 'enabled': True}]
        self.enable()
        original_set = self.bd.set
        def disconnect_writeback(device, **fields):
            original_set(device, **fields)
            if device['kind'] == 'VirtualScreen' and fields.get('connected') is False:
                next(d for d in self.bd.devices if d['identity'] == 'builtin').update(
                    resolution='1147x745', refreshRate='60', placement='1920x52')
        self.bd.set = disconnect_writeback
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertIsNone(self.engine.state['journal'])
        builtin = next(d for d in self.bd.devices if d['identity'] == 'builtin')
        self.assertEqual(builtin['resolution'], '1512x982')
        self.assertEqual(builtin['refreshRate'], '120')
        self.assertEqual(builtin['placement'], '1920x50')
        self.assertEqual(self.bd.group_calls[-1], ('layout', True))

    def test_delayed_writeback_is_repaired_before_recovery_receipt(self):
        self.bd.devices.append(self.builtin('1920x50'))
        self.enable()
        original_set, original_inventory = self.bd.set, self.bd.inventory
        due = [None]
        def delayed_set(device, **fields):
            original_set(device, **fields)
            if device['kind'] == 'VirtualScreen' and fields.get('connected') is False:
                due[0] = self.now + .5
        def delayed_inventory():
            if due[0] is not None and self.now >= due[0]:
                next(d for d in self.bd.devices if d['identity'] == 'builtin').update(
                    resolution='1147x745', refreshRate='60', placement='1920x52')
                due[0] = None
            return original_inventory()
        self.bd.set, self.bd.inventory = delayed_set, delayed_inventory
        self.pause()
        self.assertIsNone(due[0])
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        builtin = next(d for d in self.bd.devices if d['identity'] == 'builtin')
        self.assertEqual((builtin['resolution'], builtin['refreshRate'], builtin['placement']),
                         ('1512x982', '120', '1920x50'))
        self.assertIsNone(self.engine.state['journal'])

    def test_continuous_writeback_keeps_recovery_record_with_bounded_repairs(self):
        self.bd.devices.append(self.builtin('1920x50'))
        self.enable()
        original_set, original_inventory = self.bd.set, self.bd.inventory
        changing = [False]
        def changing_set(device, **fields):
            original_set(device, **fields)
            if device['kind'] == 'VirtualScreen' and fields.get('connected') is False:
                changing[0] = True
        def changing_inventory():
            if changing[0]:
                next(d for d in self.bd.devices if d['identity'] == 'builtin')['refreshRate'] = '60'
            return original_inventory()
        self.bd.set, self.bd.inventory = changing_set, changing_inventory
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertTrue(self.engine.state['journal'])
        self.assertFalse(self.engine.state['desired_enabled'])

    def test_protection_failure_keeps_journal_and_restart_preserves_manual_pause(self):
        self.bd.groups = [{'tag': 'layout', 'enabled': True}]
        captured = self.incident()
        saved = copy.deepcopy(captured)
        self.bd.protection_fail = lambda group, enabled: enabled
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertEqual(self.engine.state['journal']['physical'], saved)
        self.bd.protection_fail = None
        self.engine = self.make_engine()
        self.engine.tick(dict(timestamp=self.now, state='connected', trusted=True))
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertIsNone(self.engine.state['journal'])

    def test_equivalent_refresh_reporting_uses_same_tolerance_through_final_verification(self):
        self.bd.devices[0]['refreshRate'] = '75'
        self.enable()
        self.bd.devices[0]['refreshRate'] = '75.0004Hz'
        self.bd.calls.clear()
        self.pause()
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertIsNone(self.engine.state['journal'])
        self.assertFalse(any('refreshRate' in fields for _, fields in self.bd.calls))

    def test_real_refresh_difference_still_requires_restoration(self):
        expected = display()
        actual = copy.deepcopy(expected)
        actual['refreshRate'] = '75'
        self.assertEqual(c.Engine._mode_changes(actual, expected), {'refreshRate': '60'})
        self.assertFalse(c.Engine._matches([actual], {'physical1': expected}))

    def test_valid_negative_and_portrait_layout_is_preserved_exactly(self):
        snapshots = {'physical1': display(), 'builtin': self.builtin('1920x50')}
        left = display('left', True, False)
        left.update(resolution='800x1280', rotation='90', placement='-800x0')
        snapshots['left'] = left
        saved = copy.deepcopy(snapshots)
        self.assertEqual(c.Engine._recovery_layout(snapshots, snapshots), snapshots)
        self.assertEqual(snapshots, saved)

    def test_nonzero_main_translates_the_whole_plan_consistently(self):
        main = display()
        main['placement'] = '1376x50'
        other = self.builtin('3296x70')
        snapshots = {'physical1': main, 'builtin': other}
        saved = copy.deepcopy(snapshots)
        plan = c.Engine._recovery_layout(snapshots, snapshots)
        self.assertEqual(plan['physical1']['placement'], '0x0')
        self.assertEqual(plan['builtin']['placement'], '1920x20')
        self.assertEqual(snapshots, saved)

    def test_unmanaged_output_at_main_origin_is_not_moved_or_silently_accepted(self):
        current = {'other': display('other', True, False)}
        with self.assertRaisesRegex(RuntimeError, 'unmanaged physical output'):
            c.Engine._recovery_layout({'physical1': display()}, current)

    def test_moving_conflict_does_not_displace_originally_clear_screen(self):
        snapshots = {'physical1': display(), 'overlap': self.builtin(), 'clear': self.builtin('3000x50')}
        for key in ('overlap', 'clear'):
            snapshots[key].update(identity=key, captured_during_remote=True)
        saved = copy.deepcopy(snapshots)
        plan = c.Engine._recovery_layout(snapshots, snapshots)
        self.assertEqual(plan['clear']['placement'], '3000x50')
        self.assertEqual(plan['physical1']['placement'], '0x0')
        self.assertNotEqual(plan['overlap']['placement'], '1376x50')
        self.assertEqual(snapshots, saved)

    def test_multi_screen_overlap_is_resolved_without_changing_modes_or_mutating_saved_data(self):
        snapshots = {'physical1': display()}
        for index in range(4):
            screen = display('extra-' + str(index), True, False)
            screen.update(placement='100x100', rotation='90' if index % 2 else '0')
            snapshots[screen['identity']] = screen
        saved = copy.deepcopy(snapshots)
        plan = c.Engine._recovery_layout(snapshots, snapshots)
        rectangles = []
        for identity, device in plan.items():
            self.assertEqual(c.Engine._mode_changes(device, saved[identity]), {})
            x, y = map(int, device['placement'].split('x'))
            w, h = map(int, device['resolution'].split('x'))
            for ox, oy, ow, oh in rectangles:
                self.assertTrue(x + w <= ox or ox + ow <= x or y + h <= oy or oy + oh <= y)
            rectangles.append((x, y, w, h))
        self.assertEqual(plan['physical1']['placement'], '0x0')
        self.assertEqual(snapshots, saved)

    def physical_mode_fixture(self):
        helper = self.directory / 'native-topology'
        helper.write_text('test fixture; never executed')
        bd = c.BetterDisplay(native_helper=helper)
        device = self.builtin('1920x50')
        device.update(native_display_id=1, refreshRate='60')
        modes = dict(displayID=1, uuid='builtin', vendor=1552, model=1, serial=1,
                     modes=[dict(width=1512, height=982, pixelWidth=3024, pixelHeight=1964,
                                 refreshRate=120, modeID=54),
                            dict(width=1512, height=982, pixelWidth=3024, pixelHeight=1964,
                                 refreshRate=60, modeID=55)])
        return bd, device, modes

    def test_physical_promotion_selects_exact_fresh_mode_without_fixed_120_selector(self):
        bd, device, modes = self.physical_mode_fixture()
        with patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(modes))), \
                patch.object(bd, '_run') as call:
            bd.set(device, refreshRate='120')
        call.assert_called_once_with('set', '-tagID=builtin', '-displayModeNumber=54')

    def test_physical_mode_identity_change_blocks_set_even_if_edid_matches(self):
        bd, device, modes = self.physical_mode_fixture()
        modes['uuid'] = 'replacement'
        device['identity'] = 'virtual:1552:1:1'
        with patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(modes))), \
                patch.object(bd, '_run') as call:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                bd.set(device, refreshRate='120')
        call.assert_not_called()

    def test_unavailable_exact_physical_mode_does_not_silently_choose_60hz(self):
        bd, device, modes = self.physical_mode_fixture()
        modes['modes'] = modes['modes'][1:]
        with patch.object(c.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(modes))), \
                patch.object(bd, '_run') as call:
            bd.set(device, refreshRate='120')
        call.assert_called_once_with('set', '-tagID=builtin', '-refreshRate=120')

    def test_rotated_physical_output_keeps_existing_rotation_aware_selector(self):
        bd, device, _ = self.physical_mode_fixture()
        device['rotation'] = '90'
        with patch.object(c.subprocess, 'run') as native, patch.object(bd, '_run') as call:
            bd.set(device, resolution='800x1280')
        native.assert_not_called()
        call.assert_called_once_with('set', '-tagID=builtin', '-resolution=800x1280')


if __name__ == '__main__':
    unittest.main()
