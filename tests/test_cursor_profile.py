import importlib.util
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / 'cursor_profile.py'
spec = importlib.util.spec_from_file_location('cursor_profile', MODULE)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CursorProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.now = 1000
        self.calls = []
        self.should_fail = lambda args: False
        self.applied = {}
        names = ['ArrowS', 'IBeamS', 'PointingHand', 'ClosedHand', 'OpenHand', 'Wait',
                 'Busy', 'Cross', 'Move', 'ResizeLeft', 'ResizeRight', 'ResizeUp',
                 'ResizeDown', 'ResizeLeftRight', 'ResizeUpDown', 'Copy', 'Alias',
                 'NotAllowed', 'ContextualMenu', 'DisappearingItem']
        self.catalog = [dict(name=name, identifier='com.apple.coregraphics.' + name,
                             frames=12 if name in ('Wait', 'Busy') else 1,
                             duration=0.8 if name in ('Wait', 'Busy') else 0,
                             width=32, height=32, imageCount=24 if name in ('Wait', 'Busy') else 2)
                        for name in names]
        self.manager = self.make_manager()

    def make_manager(self):
        return c.CursorProfile(self.directory, helper='/mock/helper',
                               clock=lambda: self.now, runner=self.run_helper)

    def config(self, scale=3, names=None, **extra):
        value = dict(enabled=True, scale=scale, names=names or ['ArrowS', 'IBeamS'])
        value.update(extra)
        (self.directory / 'cursor-profile-config.json').write_text(json.dumps(value))

    def journal(self):
        return json.loads((self.directory / 'cursor-profile-state.json').read_text())

    def run_helper(self, args, **kwargs):
        self.assertEqual(kwargs, dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, timeout=5, check=False))
        self.assertEqual(args[0], '/mock/helper')
        if args[1] == '--list':
            self.assertEqual(args, ['/mock/helper', '--list'])
            self.calls.append(['--list'])
            return SimpleNamespace(returncode=7 if self.should_fail(args) else 0,
                                   stdout=json.dumps(self.catalog))
        op, path = args[1], Path(args[2])
        state = self.journal()
        self.assertTrue(any(record['path'] == str(path) for record in state['pending_originals']))
        self.calls.append(args[1:])
        if self.should_fail(args):
            return SimpleNamespace(returncode=7, stdout='injected readback failure')
        if op == '--backup':
            self.assertFalse(path.exists(), 'Original backup must never be overwritten')
            self.assertNotIn(path.stem, self.applied, 'Compounded capture')
            path.write_text('original pixels ' + args[3])
        elif op == '--restore':
            self.assertTrue(path.exists())
            self.applied.pop(path.stem, None)
        elif op == '--apply':
            self.assertEqual(state['status'], 'applying')
            self.assertTrue(all(record['captured'] and Path(record['path']).exists()
                                for record in state['pending_originals']))
            self.assertEqual(state['desired_profile']['scale'], float(args[3]))
            # Every selected original must have passed its roundtrip first.
            restores = [call[1] for call in self.calls if call[0] == '--restore']
            self.assertTrue(all(record['path'] in restores for record in state['pending_originals']))
            self.applied[path.stem] = float(args[3])
        else:
            self.fail('Unknown helper operation')
        return SimpleNamespace(returncode=0, stdout='verified')

    def test_idle_has_zero_helper_calls(self):
        for _ in range(3):
            self.assertFalse(self.manager.sync(False))
            self.assertFalse(self.manager.sync(True))
        self.manager.close()
        self.assertEqual(self.calls, [])

    def test_three_times_both_journal_before_mutation_and_steady_no_calls(self):
        self.config()
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(self.applied, dict(ArrowS=3, IBeamS=3))
        self.assertEqual([call[0] for call in self.calls],
                         ['--backup', '--backup', '--restore', '--restore', '--apply', '--apply'])
        originals = self.journal()['pending_originals']
        self.assertEqual(len(originals), 2)
        self.assertEqual(self.journal()['original_profile'], dict(scale=3, names=['ArrowS', 'IBeamS']))
        self.manager.sync(True)
        self.now += 100
        self.manager.sync(True)
        self.assertEqual(len(self.calls), 6)

    def test_off_restores_and_retains_original_files(self):
        self.config()
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        self.assertFalse(self.manager.sync(False))
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertTrue(all(Path(record['path']).exists() for record in originals))
        self.assertIsNone(self.journal()['desired_profile'])

    def test_close_restores(self):
        self.config()
        self.manager.sync(True)
        self.manager.close()
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])
        count = len(self.calls)
        self.manager.close()
        self.assertEqual(len(self.calls), count)

    def test_close_persists_disabled_intent_after_recovery_only_event(self):
        self.config()
        self.manager.sync(True)
        restarted = self.make_manager()
        self.assertFalse(restarted.sync(True))
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertIsNotNone(self.journal()['desired_profile'])
        count = len(self.calls)
        restarted.close()
        self.assertIsNone(self.journal()['desired_profile'])
        self.assertEqual(len(self.calls), count)

    def test_partial_apply_failure_rolls_back_every_original(self):
        self.config()
        self.should_fail = lambda args: args[1] == '--apply' and Path(args[2]).stem == 'IBeamS'
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.applied, {})
        self.assertEqual([Path(call[1]).stem for call in self.calls[-2:]], ['ArrowS', 'IBeamS'])
        self.assertTrue(all(call[0] == '--restore' for call in self.calls[-2:]))
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertIn('readback failure', self.journal()['error'])

    def test_restore_failure_keeps_all_originals_and_backs_off(self):
        self.config()
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        self.should_fail = lambda args: args[1] == '--restore' and Path(args[2]).stem == 'ArrowS'
        self.assertFalse(self.manager.sync(False))
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual(self.journal()['status'], 'recovery_pending')
        count = len(self.calls)
        self.now += 4.99
        self.manager.sync(False)
        self.assertEqual(len(self.calls), count)
        self.now += .01
        self.should_fail = lambda args: False
        self.manager.sync(False)
        self.assertEqual(len(self.calls), count + 2)
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_restart_restores_before_new_capture_and_uses_new_files(self):
        self.config()
        self.manager.sync(True)
        paths = [record['path'] for record in self.journal()['pending_originals']]
        restarted = self.make_manager()
        count = len(self.calls)
        self.assertFalse(restarted.sync(True))
        self.assertEqual([call[0] for call in self.calls[count:]], ['--restore', '--restore'])
        self.assertEqual(self.applied, {})
        self.assertTrue(restarted.sync(True))
        self.assertTrue(set(paths).isdisjoint(record['path'] for record in self.journal()['pending_originals']))

    def test_invalid_configs_never_call_helper(self):
        cases = [dict(scale=5), dict(scale=True), dict(names=['Wait']),
                 dict(names=['ArrowS', 'ArrowS']), dict(names='ArrowS'), dict(enabled=False)]
        for case in cases:
            with self.subTest(case=case):
                self.config(**case)
                self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.calls, [])

    def test_malformed_config_restores_active_originals(self):
        self.config()
        self.manager.sync(True)
        self.manager.config_path.write_text('{bad')
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_manual_disable_restores(self):
        self.config()
        self.manager.sync(True)
        self.config(enabled=False)
        self.manager.sync(True)
        self.assertEqual(self.applied, {})

    def test_profile_change_restores_first_and_applies_same_event(self):
        self.config()
        self.manager.sync(True)
        self.config(scale=2, names=['ArrowS'])
        count = len(self.calls)
        self.assertTrue(self.manager.sync(True))
        self.assertEqual([call[0] for call in self.calls[count:]],
                         ['--restore', '--restore', '--backup', '--restore', '--apply'])
        self.assertEqual(self.applied, dict(ArrowS=2))

    def test_timeout_is_contained_and_rolls_back(self):
        self.config()
        real_runner = self.manager.runner

        def timeout_apply(args, **kwargs):
            if args[1] == '--apply':
                raise subprocess.TimeoutExpired(args, 5)
            return real_runner(args, **kwargs)

        self.manager.runner = timeout_apply
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_restart_respects_restore_backoff(self):
        self.config()
        self.manager.sync(True)
        self.should_fail = lambda args: args[1] == '--restore'
        self.manager.sync(False)
        count = len(self.calls)
        restarted = self.make_manager()
        restarted.sync(True)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(self.journal()['pending_originals']), 2)

    def test_bad_journal_fails_closed_and_is_preserved(self):
        self.manager.state_path.write_text('not JSON')
        restarted = self.make_manager()
        self.config()
        self.assertFalse(restarted.sync(True))
        restarted.close()
        self.assertEqual(restarted.state_path.read_text(), 'not JSON')
        self.assertEqual(self.calls, [])

    def test_backup_failure_never_enlarges(self):
        self.config()
        self.should_fail = lambda args: args[1] == '--backup' and Path(args[2]).stem == 'IBeamS'
        self.assertFalse(self.manager.sync(True))
        self.assertFalse(any(call[0] == '--apply' for call in self.calls))
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_validation_failure_never_enlarges_and_preserves_recovery(self):
        self.config()
        self.should_fail = lambda args: args[1] == '--restore' and Path(args[2]).stem == 'IBeamS'
        self.assertFalse(self.manager.sync(True))
        self.assertFalse(any(call[0] == '--apply' for call in self.calls))
        self.assertEqual(self.journal()['status'], 'recovery_pending')
        self.assertEqual(len(self.journal()['pending_originals']), 2)

    def test_all_supported_discovers_twenty_static_and_animated_cursors_once(self):
        self.config(scope='all_supported')
        self.assertTrue(self.manager.sync(True))
        names = [entry['name'] for entry in self.catalog]
        self.assertEqual(self.applied, dict.fromkeys(names, 3))
        self.assertEqual(self.journal()['selected_names'], names)
        self.assertEqual(self.journal()['original_profile']['scope'], 'all_supported')
        self.assertTrue(self.journal()['unsupported_diagnostics'])
        self.assertEqual(self.calls[0], ['--list'])
        self.assertEqual(len(self.calls), 1 + 3 * len(names))
        count = len(self.calls)
        with patch.object(self.manager, '_save') as save:
            for _ in range(5):
                self.assertTrue(self.manager.sync(True))
            save.assert_not_called()
        self.assertEqual(len(self.calls), count)

    def test_all_supported_manual_scales_use_same_event_and_uniform_size(self):
        for scale in c.CursorProfile.SCALES:
            with self.subTest(scale=scale):
                self.config(scale=scale, scope='all_supported')
                count = len(self.calls)
                self.assertTrue(self.manager.sync(True))
                self.assertEqual(set(self.applied.values()), {scale})
                self.assertEqual(len(self.applied), 20)
                new_calls = self.calls[count:]
                self.assertEqual(new_calls[0], ['--list'])
                self.assertEqual(sum(call[0] == '--list' for call in new_calls), 1)
                if scale != c.CursorProfile.SCALES[0]:
                    self.assertEqual([call[0] for call in new_calls[1:]], ['--apply'] * 20)

    def test_catalog_validation_rejects_unsafe_metadata_before_mutation(self):
        original = copy.deepcopy(self.catalog)
        variants = [[], original[1:], {'name': 'ArrowS'}]
        for key, value in [('name', '../ArrowS'), ('name', '箭头'), ('name', 'A' * 65),
                           ('identifier', 'com.other.ArrowS'), ('frames', 0),
                           ('frames', True), ('frames', 1.5), ('frames', 65),
                           ('duration', -1), ('duration', 10.1),
                           ('duration', float('nan')), ('width', 0),
                           ('height', float('inf')), ('imageCount', False)]:
            changed = copy.deepcopy(original)
            changed[0][key] = value
            variants.append(changed)
        variants.append(original + [copy.deepcopy(original[0])])
        for catalog in variants:
            with self.subTest(catalog=catalog):
                self.catalog = catalog
                self.manager = self.make_manager()
                self.config(scope='all_supported')
                count = len(self.calls)
                self.assertFalse(self.manager.sync(True))
                self.assertEqual(self.calls[count:], [['--list']])
                self.assertTrue(self.journal()['error'])
                self.assertEqual(self.journal()['pending_originals'], [])

    def test_invalid_catalog_preserves_active_profile_and_caches_rejection(self):
        self.config()
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        self.catalog = self.catalog[1:]
        self.config(scope='all_supported')
        count = len(self.calls)
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(self.calls[count:], [['--list']])
        self.assertEqual(self.applied, dict(ArrowS=3, IBeamS=3))
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertIn('ArrowS and IBeamS', self.journal()['error'])
        count = len(self.calls)
        with patch.object(c.os, 'replace') as replace:
            for _ in range(3):
                self.assertTrue(self.manager.sync(True))
            replace.assert_not_called()
        self.assertEqual(len(self.calls), count)

    def test_changed_all_supported_catalog_can_include_conditional_subset(self):
        self.catalog = self.catalog[:3]
        self.config(scope='all_supported')
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(set(self.applied), {'ArrowS', 'IBeamS', 'PointingHand'})
        self.assertIn('conditional', self.journal()['unsupported_diagnostics'][0])

    def test_all_supported_partial_failure_rolls_back_all_twenty_originals(self):
        self.config(scope='all_supported')
        self.should_fail = lambda args: args[1] == '--apply' and Path(args[2]).stem == 'Wait'
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.applied, {})
        self.assertEqual(len(self.calls[-20:]), 20)
        self.assertTrue(all(call[0] == '--restore' for call in self.calls[-20:]))
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_changed_profile_restore_failure_never_replaces_journal(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        count = len(self.calls)
        self.should_fail = lambda args: args[1] == '--restore' and Path(args[2]).stem == 'Wait'
        self.catalog = self.catalog[:-1]  # Changed names must take full restoration path.
        self.config(scale=4, scope='all_supported')
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual(self.journal()['status'], 'recovery_pending')
        self.assertFalse(any(call[0] in ('--backup', '--apply') for call in self.calls[count:]))
        count = len(self.calls)
        self.now += 4
        self.manager.sync(True)
        self.assertEqual(len(self.calls), count)

    def test_all_supported_restart_recovers_before_any_catalog_lookup(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        count = len(self.calls)
        restarted = self.make_manager()
        self.assertFalse(restarted.sync(True))
        self.assertEqual([call[0] for call in self.calls[count:]], ['--restore'] * 20)
        count = len(self.calls)
        self.assertTrue(restarted.sync(True))
        self.assertEqual(self.calls[count], ['--list'])

    def test_all_supported_close_restores_every_cursor(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        count = len(self.calls)
        self.manager.close()
        self.assertEqual([call[0] for call in self.calls[count:]], ['--restore'] * 20)
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_enabled_false_with_scale_one_restores(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        self.config(enabled=False, scale=1, scope='all_supported')
        self.manager.sync(True)
        self.assertEqual(self.applied, {})

    def test_explicit_names_support_fractional_scale_without_catalog(self):
        self.config(scale=2.5)
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(self.applied, dict(ArrowS=2.5, IBeamS=2.5))
        self.assertFalse(any(call[0] == '--list' for call in self.calls))

    def test_legacy_journal_without_new_fields_recovers(self):
        self.config()
        self.manager.sync(True)
        state = self.journal()
        state.pop('selected_names')
        state.pop('unsupported_diagnostics')
        self.manager.state_path.write_text(json.dumps(state))
        restarted = self.make_manager()
        self.assertFalse(restarted.sync(True))
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])

    def test_reactivation_discovers_catalog_again(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        self.manager.sync(False)
        self.catalog = self.catalog[:4]
        count = len(self.calls)
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(self.calls[count], ['--list'])
        self.assertEqual(len(self.applied), 4)

    def test_listing_failure_preserves_existing_originals(self):
        self.config()
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        self.should_fail = lambda args: args[1] == '--list'
        self.config(scope='all_supported')
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertIn('failed (7)', self.journal()['error'])

    def test_core_cursor_identifiers_and_thirty_frame_animation(self):
        self.catalog = self.catalog[:2] + [
            dict(name=f'Core{number}', identifier=f'com.apple.cursor.{number}',
                 frames=30 if number == 6 else 1, duration=1 if number == 6 else 0,
                 width=32, height=32, imageCount=60 if number == 6 else 2)
            for number in range(44)] + [
            dict(name='Wait', identifier='com.apple.coregraphics.Wait', frames=30,
                 duration=1, width=32, height=32, imageCount=60)]
        self.config(scope='all_supported')
        self.assertTrue(self.manager.sync(True))
        backups = [call for call in self.calls if call[0] == '--backup']
        self.assertEqual(len(backups), 47)
        for call in backups:
            name = Path(call[1]).stem
            expected = ('com.apple.cursor.' + name[4:] if name.startswith('Core')
                        else 'com.apple.coregraphics.' + name)
            self.assertEqual(call[2], expected)
        self.assertEqual(self.applied['Wait'], 3)
        for name in ('Core0', 'Core2', 'Core3', 'Core4', 'Core5', 'Core6'):
            self.assertEqual(self.applied[name], 3)
        self.manager.close()
        self.assertEqual(self.applied, {})

    def test_invalid_core_numbers_or_aliases_never_mutate(self):
        for name, identifier in [('Core00', 'com.apple.cursor.0'),
                                 ('Core08', 'com.apple.cursor.8'),
                                 ('Core44', 'com.apple.cursor.44'),
                                 ('Core09', 'com.apple.cursor.9'),
                                 ('Core13', 'com.apple.cursor.14'),
                                 ('Core13', 'com.apple.coregraphics.Core13'),
                                 ('ArrowS', 'com.apple.cursor.13')]:
            with self.subTest(name=name, identifier=identifier):
                self.manager = self.make_manager()
                self.catalog = self.catalog[:2] + [dict(
                    name=name, identifier=identifier, frames=1, duration=0,
                    width=32, height=32, imageCount=2)]
                self.config(scope='all_supported')
                count = len(self.calls)
                self.assertFalse(self.manager.sync(True))
                self.assertEqual(self.calls[count:], [['--list']])

    def test_restore_global_deadline_preserves_all_forty_nine_originals(self):
        self.catalog = self.catalog[:4] + [dict(
            name=f'Core{number}', identifier=f'com.apple.cursor.{number}', frames=1,
            duration=0, width=32, height=32, imageCount=2) for number in range(44)]
        self.catalog += [dict(name='Wait', identifier='com.apple.coregraphics.Wait',
                              frames=30, duration=1, width=32, height=32, imageCount=60)]
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        ticks = [0]
        timeouts = []

        def timeout_restore(args, **kwargs):
            self.assertEqual(args[1], '--restore')
            timeouts.append(kwargs['timeout'])
            ticks[0] += kwargs['timeout']
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])

        self.manager.monotonic = lambda: ticks[0]
        self.manager.runner = timeout_restore
        self.assertFalse(self.manager.sync(False))
        self.assertEqual(timeouts, [5, 3])
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertIn('deadline', self.journal()['error'])
        self.assertEqual(self.journal()['status'], 'recovery_pending')

    def make_worker(self, runner=None, join_timeout=2):
        self.worker_active = threading.Event()
        self.worker_off = threading.Event()
        self.worker_created = threading.Event()
        self.worker_manager = None

        def factory(directory, **kwargs):
            kwargs['runner'] = runner or self.run_helper
            manager = c.CursorProfile(directory, **kwargs)
            self.worker_manager = manager
            sync = manager.sync

            def observe(active):
                result = sync(active)
                if active and result:
                    self.worker_active.set()
                if not active:
                    self.worker_off.set()
                return result

            manager.sync = observe
            self.worker_created.set()
            return manager

        worker = c.CursorProfileWorker(self.directory, helper='/mock/helper', factory=factory,
                                       clock=lambda: self.now, join_timeout=join_timeout)
        self.addCleanup(worker.close)
        self.assertTrue(self.worker_created.wait(2))
        self.assertTrue(self.worker_off.wait(2))
        self.worker_off.clear()
        return worker

    def test_restore_deadline_progress_can_finish_later_without_losing_originals(self):
        self.catalog = self.catalog[:3]
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        ticks, restored = [0], []

        def slow_restore(args, **kwargs):
            restored.append(Path(args[2]).stem)
            ticks[0] += 4
            return self.run_helper(args, **dict(kwargs, timeout=5))

        self.manager.monotonic = lambda: ticks[0]
        self.manager.runner = slow_restore
        self.manager.sync(False)
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual(len(self.journal()['restore_progress']), 2)
        self.now += 5
        self.manager.sync(False)
        self.assertEqual(restored, ['ArrowS', 'IBeamS', 'PointingHand'])
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertEqual(self.applied, {})

    def test_worker_sync_nonblocking_and_busy_true_to_false_restores(self):
        self.config()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runner(args, **kwargs):
            if args[1] == '--backup' and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            return self.run_helper(args, **kwargs)

        worker = self.make_worker(runner)
        started = time.monotonic()
        worker.sync(True)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        worker.sync(False)
        self.assertLess(time.monotonic() - started, 0.2)
        release.set()
        self.assertTrue(self.worker_off.wait(2))
        self.assertEqual(self.applied, {})
        self.assertFalse(any(call[0] == '--apply' for call in self.calls))
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertTrue(worker.close()['restored'])

    def test_worker_same_config_heartbeats_do_not_cancel_apply(self):
        self.config()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runner(args, **kwargs):
            if args[1] == '--backup' and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            return self.run_helper(args, **kwargs)

        worker = self.make_worker(runner)
        worker.sync(True)
        self.assertTrue(entered.wait(2))
        for _ in range(20):
            worker.sync(True)
        self.assertFalse(self.worker_manager.cancelled())
        release.set()
        self.assertTrue(self.worker_active.wait(2))
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.applied, dict(ArrowS=3, IBeamS=3))
        self.assertTrue(worker.close()['restored'])

    def test_worker_config_change_cancels_old_and_applies_latest_scale(self):
        self.config()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runner(args, **kwargs):
            if args[1] == '--apply' and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            return self.run_helper(args, **kwargs)

        worker = self.make_worker(runner)
        worker.sync(True)
        self.assertTrue(entered.wait(2))
        self.config(scale=2.5)
        worker.sync(True)
        self.assertTrue(self.worker_manager.cancelled())
        release.set()
        self.assertTrue(self.worker_active.wait(2))
        self.assertEqual(self.applied, dict(ArrowS=2.5, IBeamS=2.5))
        self.assertTrue(worker.close()['restored'])

    def test_worker_startup_restores_before_catalog_or_apply(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        count = len(self.calls)
        worker = self.make_worker()
        self.assertEqual([call[0] for call in self.calls[count:]], ['--restore'] * 20)
        self.assertEqual(self.applied, {})
        worker.sync(True)
        self.assertTrue(self.worker_active.wait(2))
        self.assertTrue(worker.close()['restored'])

    def test_worker_close_is_bounded_while_helper_busy_and_reports_pending(self):
        self.config()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runner(args, **kwargs):
            if args[1] == '--backup' and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            return self.run_helper(args, **kwargs)

        worker = self.make_worker(runner, join_timeout=0.02)
        worker.sync(True)
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        result = worker.close()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(result['stopped'])
        self.assertFalse(result['restored'])
        self.assertTrue(result['pending_recovery'])
        self.assertTrue(worker._thread.daemon)
        release.set()
        worker._thread.join(2)
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker.close()['restored'])

    def test_restore_rotation_reaches_later_originals_despite_first_two_timeouts(self):
        self.catalog = self.catalog[:4] + [dict(
            name=f'Core{number}', identifier=f'com.apple.cursor.{number}', frames=1,
            duration=0, width=32, height=32, imageCount=2) for number in range(44)]
        self.catalog += [dict(name='Wait', identifier='com.apple.coregraphics.Wait',
                              frames=30, duration=1, width=32, height=32, imageCount=60)]
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        ticks, attempts = [0], []

        def restore_with_two_permanent_timeouts(args, **kwargs):
            name = Path(args[2]).stem
            attempts.append(name)
            if name in ('ArrowS', 'IBeamS'):
                ticks[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return self.run_helper(args, **kwargs)

        self.manager.monotonic = lambda: ticks[0]
        self.manager.runner = restore_with_two_permanent_timeouts
        self.manager.sync(False)
        self.assertEqual(attempts, ['ArrowS', 'IBeamS'])
        self.assertEqual(self.journal()['restore_cursor'], 2)
        self.assertEqual(self.journal()['pending_originals'], originals)
        # The next process must resume the rotation, not start at the failures.
        restarted = self.make_manager()
        restarted.monotonic = lambda: ticks[0]
        restarted.runner = restore_with_two_permanent_timeouts
        self.now += 5
        restarted.sync(False)
        self.assertEqual(attempts[2], 'PointingHand')
        self.assertEqual(set(attempts), {entry['name'] for entry in self.catalog})
        self.assertEqual(set(self.applied), {'ArrowS', 'IBeamS'})
        self.assertEqual(len(self.journal()['restore_progress']), 47)
        self.assertEqual(self.journal()['pending_originals'], originals)
        count = len(attempts)
        self.now += 5
        restarted.sync(False)
        self.assertEqual(attempts[count:], ['ArrowS', 'IBeamS'])
        self.assertEqual(self.journal()['pending_originals'], originals)

    def test_cancelled_catalog_is_not_cached_and_same_request_can_retry(self):
        self.config(scope='all_supported')
        cancelled = [True]
        self.manager.cancelled = lambda: cancelled[0]
        self.assertFalse(self.manager.sync(True))
        self.assertIsNone(self.manager._catalog_request)
        self.assertIsNone(self.manager._catalog_error)
        self.assertIsNone(self.manager._catalog_profile)
        self.assertEqual(self.calls, [])
        # A transient A -> B -> A mailbox transition may return to identical A.
        cancelled[0] = False
        self.assertTrue(self.manager.sync(True))
        self.assertEqual(self.calls[0], ['--list'])
        self.assertEqual(len(self.applied), 20)
        self.manager.close()
        self.assertEqual(self.applied, {})

    def test_active_scale_change_reuses_original_paths_and_bytes_then_close_restores(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        data = {record['path']: Path(record['path']).read_bytes() for record in originals}
        count = len(self.calls)
        self.config(scale=2.5, scope='all_supported')
        self.assertTrue(self.manager.sync(True))
        self.assertEqual([call[0] for call in self.calls[count:]], ['--list'] + ['--apply'] * 20)
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual({path: Path(path).read_bytes() for path in data}, data)
        self.assertEqual(self.manager._active['scale'], 2.5)
        self.assertEqual(self.journal()['original_profile']['scale'], 2.5)
        self.assertEqual(self.journal()['desired_profile']['scale'], 2.5)
        self.assertEqual(set(self.applied.values()), {2.5})
        count = len(self.calls)
        self.manager.close()
        self.assertEqual([call[0] for call in self.calls[count:]], ['--restore'] * 20)
        self.assertEqual(self.applied, {})

    def test_active_scale_change_cancel_restores_all_originals(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        cancelled = [False]
        self.manager.cancelled = lambda: cancelled[0]

        def cancel_after_first_apply(args, **kwargs):
            result = self.run_helper(args, **kwargs)
            if args[1] == '--apply':
                cancelled[0] = True
            return result

        self.manager.runner = cancel_after_first_apply
        self.config(scale=2.5, scope='all_supported')
        count = len(self.calls)
        self.assertFalse(self.manager.sync(True))
        self.assertEqual([call[0] for call in self.calls[count:]],
                         ['--list', '--apply'] + ['--restore'] * 20)
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertTrue(all(Path(record['path']).exists() for record in originals))

    def test_active_scale_change_failure_preserves_full_journal_at_restore_deadline(self):
        self.config(scope='all_supported')
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        ticks, timeouts = [0], []

        def fail_apply_and_timeout_restore(args, **kwargs):
            if args[1] == '--restore':
                timeouts.append(kwargs['timeout'])
                ticks[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            if args[1] == '--apply' and Path(args[2]).stem == 'IBeamS':
                return SimpleNamespace(returncode=7, stdout='injected resize readback failure')
            return self.run_helper(args, **kwargs)

        self.manager.runner = fail_apply_and_timeout_restore
        self.manager.monotonic = lambda: ticks[0]
        self.config(scale=2.5, scope='all_supported')
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(timeouts, [5, 3])
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual(self.journal()['status'], 'recovery_pending')
        self.assertIn('deadline', self.journal()['error'])

    def test_failed_scale_change_restart_restores_remaining_original_bytes(self):
        self.config()
        self.manager.sync(True)
        originals = self.journal()['pending_originals']
        data = {record['path']: Path(record['path']).read_bytes() for record in originals}
        self.config(scale=2.5)
        self.should_fail = lambda args: (
            args[1] == '--apply' and Path(args[2]).stem == 'IBeamS'
            or args[1] == '--restore' and Path(args[2]).stem == 'ArrowS')
        self.assertFalse(self.manager.sync(True))
        self.assertEqual(self.applied, {'ArrowS': 2.5})
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual(self.journal()['status'], 'recovery_pending')
        self.assertEqual({path: Path(path).read_bytes() for path in data}, data)
        restarted = self.make_manager()
        self.should_fail = lambda args: False
        self.now += self.manager.RETRY_SECONDS
        count = len(self.calls)
        self.assertFalse(restarted.sync(True))
        self.assertEqual([call[0] for call in self.calls[count:]], ['--restore', '--restore'])
        self.assertEqual(self.applied, {})
        self.assertEqual(self.journal()['pending_originals'], [])
        self.assertEqual({path: Path(path).read_bytes() for path in data}, data)
        restarted.close()

    def test_busy_apply_shutdown_timeout_leaves_durable_originals_for_restart(self):
        self.config()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def runner(args, **kwargs):
            if args[1] == '--apply' and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            return self.run_helper(args, **kwargs)

        worker = self.make_worker(runner, join_timeout=0.02)
        worker.sync(True)
        self.assertTrue(entered.wait(2))
        originals = self.journal()['pending_originals']
        data = {record['path']: Path(record['path']).read_bytes() for record in originals}
        result = worker.close()
        self.assertFalse(result['stopped'])
        self.assertTrue(result['pending_recovery'])
        self.assertEqual(self.journal()['pending_originals'], originals)
        self.assertEqual({path: Path(path).read_bytes() for path in data}, data)
        release.set()
        worker._thread.join(2)
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker.close()['restored'])
        self.assertEqual(self.applied, {})
        self.assertIsNone(self.journal()['desired_profile'])

    def make_recovery_worker(self, failures=1, delay=0.01):
        idle, applied = threading.Event(), threading.Event()
        instances, timeouts = [], []

        class FakeManager:
            def __init__(self, *args, **kwargs):
                self.recovery_calls = 0
                self.calls = []
                self.state = dict(status='recovery_pending', pending_originals=[{'name': 'ArrowS'}],
                                  retry_at=0, desired_profile=None, error=None)
                instances.append(self)

            def sync(self, active):
                self.calls.append(active)
                self.state['desired_profile'] = {'scale': 3} if active else None
                if self.state['pending_originals']:
                    if time.time() < self.state['retry_at']:
                        return False
                    self.recovery_calls += 1
                    if self.recovery_calls <= failures:
                        self.state.update(status='recovery_pending', retry_at=time.time() + delay)
                    else:
                        self.state.update(status='restored', pending_originals=[], retry_at=0)
                    return False
                if active:
                    self.state['status'] = 'active'
                    applied.set()
                    return True
                self.state['status'] = 'idle'
                return False

            def close(self):
                self.state.update(status='restored', pending_originals=[], desired_profile=None)

        class ObservedWorker(c.CursorProfileWorker):
            def _wait_for_change(self, processed, timeout):
                timeouts.append(timeout)
                if timeout is None:
                    idle.set()
                return super()._wait_for_change(processed, timeout)

        worker = ObservedWorker(self.directory, factory=FakeManager, join_timeout=1)
        self.addCleanup(worker.close)
        return worker, instances, idle, applied, timeouts

    def test_worker_pending_startup_automatically_continues_without_external_event(self):
        worker, instances, idle, _, timeouts = self.make_recovery_worker()
        self.assertTrue(idle.wait(2))
        self.assertEqual(instances[0].recovery_calls, 2)
        self.assertEqual(instances[0].calls, [False, False])
        self.assertEqual(worker.state['pending_originals'], [])
        self.assertTrue(any(value is not None for value in timeouts))
        self.assertIsNone(timeouts[-1])

    def test_worker_completed_recovery_immediately_drains_latest_active_apply(self):
        worker, instances, _, applied, _ = self.make_recovery_worker(delay=0.03)
        worker.sync(True)
        self.assertTrue(applied.wait(2))
        self.assertEqual(instances[0].recovery_calls, 2)
        self.assertEqual(instances[0].calls[-2:], [True, True])
        self.assertTrue(worker.close()['restored'])

    def test_worker_hard_failure_automatic_batches_bounded_and_generation_resets_budget(self):
        worker, instances, idle, _, timeouts = self.make_recovery_worker(failures=100)
        self.assertTrue(idle.wait(2))
        self.assertEqual(instances[0].recovery_calls, 4)  # startup + three automatic batches
        self.assertIsNone(timeouts[-1])
        self.assertTrue(worker.state['pending_originals'])
        idle.clear()
        worker.sync(True)  # A new request generation grants three further batches.
        self.assertTrue(idle.wait(2))
        # The explicit event can itself retry if scheduling crossed retry_at.
        self.assertIn(instances[0].recovery_calls, (7, 8))
        self.assertIsNone(timeouts[-1])

    def test_worker_close_interrupts_pending_one_shot_wait(self):
        worker, _, _, _, _ = self.make_recovery_worker(failures=100, delay=5)
        start = time.monotonic()
        result = worker.close()
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertTrue(result['stopped'])


if __name__ == '__main__':
    unittest.main()
