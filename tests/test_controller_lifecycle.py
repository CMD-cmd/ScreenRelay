"""Shutdown ownership and completion receipts using only temporary fake devices."""
import copy
import errno
import fcntl
import os
import signal
import time
import unittest
from unittest.mock import patch

import test_disk_full_runtime as disk

c = disk.c


class ControllerLifecycleTests(unittest.TestCase):
    # Reuse the fixture, not its TestCase inheritance (which would rerun its tests).
    run_controller = disk.DiskFullRuntimeTests.run_controller
    connected = disk.DiskFullRuntimeTests.connected
    seed_active = disk.DiskFullRuntimeTests.seed_active

    def setUp(self):
        disk.DiskFullRuntimeTests.setUp(self)
        self.args.once = False
        self.cursor_outcomes = []
        self.workers = []
        self.close_events = []
        self.retry_waits = []
        owner = self

        class Wake(c.WakeFIFO):
            def wait_for_retry(self, timeout):
                # Exercise the production selector, shortening only its timeout.
                # The fixture's normal wait is virtual and cannot prove backoff.
                started = time.monotonic()
                owner.real_wake.wait_for_retry(self, min(timeout, 0.025))
                owner.retry_waits.append((timeout, time.monotonic() - started))
                owner.sleep(timeout)

        p = patch.object(c, 'WakeFIFO', Wake)
        p.start()
        self.addCleanup(p.stop)

        class Cursor:
            def __init__(self, *args, **kwargs):
                owner.assert_lock_owned()
                owner.assertTrue(all(worker.stopped for worker in owner.workers),
                                 'replacement worker started while an old worker was alive')
                self.stopped = False
                self.syncs = []
                owner.workers.append(self)

            def sync(self, active):
                self.syncs.append(active)

            def close(self):
                owner.assert_lock_owned()
                outcome = (owner.cursor_outcomes.pop(0) if owner.cursor_outcomes else
                           dict(stopped=True, restored=True, pending_recovery=False))
                self.stopped = outcome['stopped']
                owner.close_events.append((self, copy.deepcopy(outcome)))
                return outcome

        p = patch('cursor_profile.CursorProfileWorker', Cursor)
        p.start()
        self.addCleanup(p.stop)

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        if self.workers:  # seed_active runs an isolated Engine before the daemon exists.
            self.assert_lock_owned()
        self.on_wait()
        self.assertLess(len(self.waits), 100, 'unbounded recovery loop')

    def assert_lock_owned(self):
        with (self.directory / 'controller.lock').open('a') as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def quit_command(self):
        command = dict(id='quit-request', action='recover', exit_after_restore=True)
        self.write(self.directory / 'command.json', command)
        return command

    def test_system_exit_restores_outputs_and_remembers_enabled_for_next_start(self):
        self.seed_active()
        command = self.quit_command()
        command['preserve_enabled_after_exit'] = True
        self.write(self.directory / 'command.json', command)
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assert_completion(command)
        self.assertEqual(saved['mode'], 'ARMED')
        self.assertTrue(saved['desired_enabled'])
        self.assertTrue(saved['exit_completed']['resume_enabled'])
        self.assertNotIn('exit_resume_enabled', saved)
        self.assertTrue(next(d for d in self.bd.devices if d['identity'] == 'physical1')['connected'])
        self.assertFalse(next(d for d in self.bd.devices if d['identity'] == c.TARGET_UUID)['connected'])
        restarted = self.real_engine(self.bd, self.directory)
        restarted.sleeper = lambda seconds: setattr(self, 'now', self.now + seconds)
        self.write(self.directory / 'signal.json', self.connected())
        restarted.tick(self.connected())
        self.assertEqual(restarted.state['mode'], 'ACTIVE')

    def test_repeated_preserve_exit_inherits_only_current_pending_resume_intent(self):
        self.seed_active()
        engine = self.engines[-1]
        self.bd.fail = lambda device, fields: fields.get('connected') is True
        first = dict(id='first-system-exit', action='recover', exit_after_restore=True,
                     preserve_enabled_after_exit=True)
        engine.tick({'profile_entry_blocked': True}, first)
        self.assertFalse(engine.state['desired_enabled'])
        self.assertTrue(engine.state['exit_resume_enabled'])
        second = dict(first, id='second-system-exit')
        engine.tick({'profile_entry_blocked': True}, second)
        self.assertEqual(engine.state['exit_after_restore'], second['id'])
        self.assertTrue(engine.state['exit_resume_enabled'])
        engine.tick({'profile_entry_blocked': True}, dict(id='explicit-pause', action='pause'))
        engine.tick({'profile_entry_blocked': True}, dict(first, id='exit-after-pause'))
        self.assertFalse(engine.state['exit_resume_enabled'])

    def test_ordinary_exit_superseding_preserve_exit_stays_disabled(self):
        self.seed_active()
        engine = self.engines[-1]
        self.bd.fail = lambda device, fields: fields.get('connected') is True
        engine.tick({'profile_entry_blocked': True}, dict(id='system-exit', action='recover',
                    exit_after_restore=True, preserve_enabled_after_exit=True))
        engine.tick({'profile_entry_blocked': True}, dict(id='user-quit', action='recover',
                    exit_after_restore=True))
        self.assertFalse(engine.state['exit_resume_enabled'])
        self.assertEqual(engine.state['exit_after_restore'], 'user-quit')

    def shutdown_supersession(self, *, fifo=False, commit=False, action='pause',
                              exit_request=False, preserve=False, fail_persist=False,
                              live_worker=False, fail_state=False):
        self.seed_active()
        self.args.event_driven = fifo
        old = dict(self.quit_command(), preserve_enabled_after_exit=True)
        self.write(self.directory / 'command.json', old)
        newer = dict(id='newer-intent', action=action)
        if exit_request:
            newer.update(exit_after_restore=True, preserve_enabled_after_exit=preserve)
        sent, observed, failures = [], [], []
        blocked = [fail_persist]
        if live_worker:
            self.cursor_outcomes = [dict(stopped=False, restored=False, pending_recovery=True),
                                    dict(stopped=False, restored=False, pending_recovery=True)]
        elif not commit:
            self.cursor_outcomes = [dict(stopped=True, restored=False, pending_recovery=True)]

        def send():
            if sent:
                return
            sent.append(True)
            if fifo:
                self.assertTrue(c.wake_controller(self.directory, newer))
            else:
                self.write(self.directory / 'command.json', newer)

        def ticked():
            engine = self.engines[-1]
            if commit and not sent and engine.state.get('exit_after_restore') == old['id']:
                worker = self.workers[-1]
                close = worker.close
                def close_and_send():
                    result = close()
                    send()
                    return result
                worker.close = close_and_send
            if engine.state['last_command'] == newer['id'] and not exit_request:
                observed.append(engine.state['mode'])
                self.assertEqual(engine.state['mode'], 'PAUSED')
                self.assertFalse(engine.state['desired_enabled'])
                self.args.once = True

        def waiting():
            if fail_persist and failures:
                self.assertNotIn('exit_completed', c.read_json(self.directory / 'state.json'))
                self.assertEqual(c.read_json(self.directory / 'command.json')['id'], old['id'])
                blocked[0] = False
            elif self.close_events:
                send()

        def write(path, value):
            if (fail_state and path.name == 'state.json' and
                    value.get('last_command') == newer['id'] and not failures):
                failures.append(True)
                raise OSError(errno.EACCES, 'superseding command state temporarily unavailable')
            if path.name == 'command.json' and value.get('id') == newer['id'] and blocked[0]:
                failures.append(True)
                raise OSError(errno.ENOSPC, 'fallback command cannot persist yet')
            return self.write(path, value)

        self.on_tick, self.on_wait = ticked, waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        self.assertEqual(sent, [True])
        self.assertEqual(c.read_json(self.directory / 'command.json')['id'], newer['id'])
        saved = c.read_json(self.directory / 'state.json')
        self.assertEqual(saved['last_command'], newer['id'])
        self.assertIsNone(saved['journal'])
        if exit_request:
            self.assert_completion(newer)
            self.assertEqual(saved['exit_completed']['resume_enabled'], preserve)
            self.assertEqual(saved['desired_enabled'], preserve)
        else:
            self.assertEqual(observed, ['PAUSED'])
            self.assertNotIn('exit_completed', saved)
            self.assertNotIn('exit_after_restore', saved)
            self.assertFalse(saved['desired_enabled'])
            reopened = self.real_engine(self.bd, self.directory)
            reopened.tick(self.connected(), newer)
            self.assertEqual(reopened.state['mode'], 'PAUSED')
        if fail_persist or fail_state:
            self.assertTrue(failures)

    def test_disk_pause_during_shutdown_cancels_old_preserve_exit(self):
        self.shutdown_supersession()

    def test_disk_recover_at_commit_cancels_old_preserve_exit(self):
        self.shutdown_supersession(commit=True, action='recover')

    def test_fifo_pause_during_shutdown_is_durable_and_cancels_old_exit(self):
        self.shutdown_supersession(fifo=True)

    def test_fifo_recover_at_commit_is_drained_before_old_receipt(self):
        self.shutdown_supersession(fifo=True, commit=True, action='recover')

    def test_fifo_pause_cannot_exit_while_enospc_prevents_its_persistence(self):
        self.shutdown_supersession(fifo=True, commit=True, fail_persist=True)

    def test_pause_cancellation_survives_multiple_live_worker_retries(self):
        self.shutdown_supersession(fifo=True, commit=True, live_worker=True)

    def test_pause_cancellation_survives_state_save_failure(self):
        self.shutdown_supersession(commit=True, fail_state=True)

    def test_superseding_exit_receipt_identity_survives_state_save_failure(self):
        self.shutdown_supersession(commit=True, fail_state=True, exit_request=True,
                                   action='recover')

    def test_new_disk_user_quit_replaces_old_refresh_receipt(self):
        self.shutdown_supersession(exit_request=True, action='recover')

    def test_new_fifo_user_quit_at_commit_replaces_old_refresh_receipt(self):
        self.shutdown_supersession(fifo=True, commit=True, exit_request=True, action='recover')

    def test_new_disk_preserve_exit_inherits_pending_enabled_intent(self):
        self.shutdown_supersession(exit_request=True, preserve=True, action='recover')

    def test_new_fifo_preserve_exit_at_commit_inherits_pending_enabled_intent(self):
        self.shutdown_supersession(fifo=True, commit=True, exit_request=True, preserve=True,
                                   action='recover')

    def test_queued_fifo_pause_then_preserve_exit_keeps_pause_intent(self):
        self.queued_fifo_preserve_exit()

    def test_queued_fifo_pause_enospc_then_preserve_exit_keeps_pause_intent(self):
        self.queued_fifo_preserve_exit(fail_pause_write=True)

    def test_queued_fifo_repeated_enospc_retries_really_wait_despite_later_command(self):
        self.queued_fifo_preserve_exit(fail_pause_write=3)
        self.assertEqual([delay for delay, _ in self.retry_waits], [5, 10, 20])
        self.assertTrue(all(elapsed >= 0.02 for _, elapsed in self.retry_waits),
                        'blocked FIFO queue must not bypass the actual selector wait')

    def test_retry_wait_accepts_new_wakeup_without_consuming_blocked_queue(self):
        fifo = self.real_wake(self.directory)
        self.addCleanup(fifo.close)
        command = dict(id='still-queued', action='recover')
        self.assertTrue(c.wake_controller(self.directory, command))
        fifo.drain()
        self.write(self.directory / 'command.json', dict(id='new-enable', action='enable'))
        self.assertTrue(c.wake_controller(self.directory))
        started = time.monotonic()
        fifo.wait_for_retry(1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(fifo.take_manual_command()['id'], command['id'])
        self.assertEqual(c.read_json(self.directory / 'command.json')['id'], 'new-enable')

    def queued_fifo_preserve_exit(self, fail_pause_write=False):
        self.seed_active()
        self.args.event_driven = True
        old = dict(self.quit_command(), preserve_enabled_after_exit=True)
        self.write(self.directory / 'command.json', old)
        pause = dict(id='queued-pause', action='pause')
        newer = dict(id='queued-preserve-exit', action='recover', exit_after_restore=True,
                     preserve_enabled_after_exit=True)
        sent = []
        def ticked():
            if sent:
                return
            sent.append(True)
            worker = self.workers[-1]
            close = worker.close
            def close_and_send():
                result = close()
                self.assertTrue(c.wake_controller(self.directory, pause))
                self.assertTrue(c.wake_controller(self.directory, newer))
                worker.close = close
                return result
            worker.close = close_and_send
        failed, persisted = [], []
        def write(path, value):
            if path.name == 'command.json':
                if fail_pause_write and value.get('id') == pause['id'] and len(failed) < int(fail_pause_write):
                    failed.append(True)
                    raise OSError(errno.ENOSPC, 'queued pause persistence temporarily unavailable')
                persisted.append(value.get('id'))
            return self.write(path, value)
        def waiting():
            if failed and pause['id'] not in persisted:
                self.assertNotIn('exit_completed', c.read_json(self.directory / 'state.json'))
                self.assertEqual(c.read_json(self.directory / 'command.json')['id'], old['id'])
        self.on_tick, self.on_wait = ticked, waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        self.assertEqual(persisted, [pause['id'], newer['id']])
        self.assertEqual(len(failed), int(fail_pause_write))
        self.assert_completion(newer)
        saved = c.read_json(self.directory / 'state.json')
        self.assertEqual(saved['last_command'], newer['id'])
        self.assertFalse(saved['desired_enabled'])
        self.assertFalse(saved['exit_completed']['resume_enabled'])
        self.assertEqual(c.read_json(self.directory / 'command.json')['id'], newer['id'])

    def test_enable_during_incomplete_recovery_is_deferred_and_not_lost(self):
        self.seed_active()
        engine = self.engines[-1]
        engine._begin_restore('Manual pause', category='manual')
        self.bd.fail = lambda device, fields: device['identity'] == 'physical1' and fields.get('connected')
        engine.tick(self.connected(), dict(id='new-enable', action='enable'))
        self.assertEqual(engine.state['mode'], 'RESTORING')
        self.assertTrue(engine.state['desired_enabled'])
        self.assertIsNotNone(engine.state['journal'])
        self.bd.fail = None
        self.now += 5
        engine.tick({'profile_entry_blocked': True})
        self.assertEqual(engine.state['mode'], 'ARMED')
        self.assertIsNone(engine.state['journal'])
        self.write(self.directory / 'signal.json', self.connected())
        engine.tick(self.connected())
        self.assertEqual(engine.state['mode'], 'ACTIVE')

    def assert_completion(self, command):
        for name in ('state.json', 'status.json'):
            saved = c.read_json(self.directory / name)
            self.assertNotIn('exit_after_restore', saved)
            receipt = saved['exit_completed']
            self.assertEqual(receipt['id'], command['id'])
            self.assertEqual(receipt['controller_pid'], os.getpid())
            self.assertIsInstance(receipt['timestamp'], (int, float))
            self.assertIsNone(saved['journal'])

    def test_active_runtime_exception_restores_before_original_exception_escapes(self):
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', dict(id='enable', action='enable'))
        failure = RuntimeError('non-storage coordinator failure')

        def ticked():
            self.assertEqual(self.engines[-1].state['mode'], 'ACTIVE')
            raise failure

        self.on_tick = ticked
        with self.assertRaises(RuntimeError) as raised:
            self.run_controller()
        self.assertIs(raised.exception, failure)
        saved = c.read_json(self.directory / 'state.json')
        self.assertIsNone(saved['journal'])
        self.assertTrue(saved['desired_enabled'])
        self.assertTrue(next(d for d in self.bd.devices if d['identity'] == 'physical1')['connected'])
        self.assertFalse(next(d for d in self.bd.devices if d['identity'] == c.TARGET_UUID)['connected'])

    def test_sigterm_retries_display_recovery_under_same_lock_and_preserves_enabled(self):
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', dict(id='enable', action='enable'))
        armed = [False]

        def ticked():
            armed[0] = True
            self.bd.fail = lambda device, fields: armed[0] and fields.get('connected') is True
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)

        def waiting():
            armed[0] = False
            self.assertIsNotNone(self.engines[-1].state['journal'])

        self.on_tick, self.on_wait = ticked, waiting
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assertIsNone(saved['journal'])
        self.assertTrue(saved['desired_enabled'])
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(self.waits)
        self.assertEqual(saved['interrupted_restarts'], [])
        self.assertNotIn('failure_recorded', saved)

    def test_sigint_restores_to_paused(self):
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', dict(id='enable', action='enable'))
        self.on_tick = lambda: self.handlers[signal.SIGINT](signal.SIGINT, None)
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assertEqual(saved['mode'], 'PAUSED')
        self.assertFalse(saved['desired_enabled'])
        self.assertIsNone(saved['journal'])

    def test_quit_waits_for_live_worker_and_pending_recovery_before_receipt(self):
        command = self.quit_command()
        self.cursor_outcomes = [
            dict(stopped=False, restored=False, pending_recovery=True),
            dict(stopped=False, restored=False, pending_recovery=True),
            dict(stopped=True, restored=False, pending_recovery=True),
            dict(stopped=True, restored=True, pending_recovery=False),
        ]

        def waiting():
            self.assertNotIn('exit_completed', c.read_json(self.directory / 'state.json'))
            self.assertEqual(self.engines[-1].state['exit_after_restore'], command['id'])
            if len(self.close_events) < 3:
                self.assertEqual(len(self.workers), 1)

        self.on_wait = waiting
        self.run_controller()
        self.assertEqual(len(self.workers), 2)
        self.assertIs(self.close_events[0][0], self.close_events[2][0])
        self.assertIsNot(self.close_events[2][0], self.close_events[3][0])
        self.assertGreaterEqual(len(self.waits), 3)
        self.assert_completion(command)

    def test_explicit_quit_does_not_take_raw_signal_two_batch_limit(self):
        command = self.quit_command()
        self.cursor_outcomes = [dict(stopped=True, restored=False, pending_recovery=True)
                                for _ in range(4)]
        self.run_controller()
        self.assertEqual(len(self.workers), 5)
        self.assertEqual(self.waits, [5, 10, 20, 30])
        self.assert_completion(command)

    def test_failed_completion_state_write_retains_exit_intent_and_retries(self):
        self.completion_write_failure('state.json')

    def test_failed_completion_status_write_retains_durable_exit_intent_and_retries(self):
        self.completion_write_failure('status.json')

    def completion_write_failure(self, filename):
        command = self.quit_command()
        rejected = []

        def write(path, value):
            if path.name == filename and value.get('exit_completed') and not rejected:
                rejected.append(copy.deepcopy(value))
                raise OSError(errno.EACCES, 'completion receipt temporarily unavailable')
            return self.write(path, value)

        def waiting():
            self.assertEqual(self.engines[-1].state['exit_after_restore'], command['id'])
            self.assertNotIn('exit_completed', self.engines[-1].state)
            self.assertEqual(c.read_json(self.directory / 'state.json')['exit_after_restore'], command['id'])

        self.on_wait = waiting
        with patch.object(c, 'atomic_json', write):
            self.run_controller()
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.waits, [5])
        self.assert_completion(command)

    def test_new_enable_cancels_quit_but_restores_old_display_journal_before_standby(self):
        self.seed_active()
        command = self.quit_command()
        blocked = [True]
        self.bd.fail = lambda device, fields: blocked[0] and fields.get('connected') is True
        self.cursor_outcomes = [dict(stopped=True, restored=False, pending_recovery=True)]
        enabled = dict(id='enable-after-quit', action='enable')

        def waiting():
            if self.engines[-1].state.get('exit_after_restore'):
                self.assertIsNotNone(self.engines[-1].state['journal'])
                self.write(self.directory / 'command.json', enabled)
                blocked[0] = False
                self.write(self.directory / 'signal.json', {})

        def ticked():
            engine = self.engines[-1]
            if engine.state.get('last_command') == enabled['id']:
                self.assertNotIn('exit_after_restore', engine.state)
                if engine.state['journal']:
                    self.assertEqual(engine.state['mode'], 'RESTORING')
                    self.assertEqual(engine.state['journal']['restore_destination'], 'ARMED')
                    self.assertFalse(self.workers[-1].syncs and self.workers[-1].syncs[-1])
                    return
                self.assertIsNone(engine.state['journal'])
                self.assertEqual(engine.state['mode'], 'ARMED')
                self.assertTrue(engine.state['desired_enabled'])
                self.handlers[signal.SIGTERM](signal.SIGTERM, None)

        self.on_wait, self.on_tick = waiting, ticked
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assertNotIn('exit_after_restore', saved)
        self.assertNotIn('exit_completed', saved)
        self.assertEqual(saved['last_command'], enabled['id'])
        self.assertTrue(saved['desired_enabled'])
        self.assertIsNone(saved['journal'])

    def test_split_quit_receipt_recovers_then_next_open_preserves_enabled_preference(self):
        self.split_receipt_restart(True)

    def test_split_quit_receipt_recovers_then_next_open_preserves_paused_preference(self):
        self.split_receipt_restart(False)

    def split_receipt_restart(self, preserve):
        self.seed_active()
        command = self.quit_command()
        command['preserve_enabled_after_exit'] = preserve
        self.write(self.directory / 'command.json', command)
        split = []

        def capture(path, value):
            if path.name == 'status.json' and value.get('exit_completed') and not split:
                split.append((copy.deepcopy(c.read_json(self.directory / 'state.json')),
                              copy.deepcopy(c.read_json(self.directory / 'status.json'))))
            return self.write(path, value)

        with patch.object(c, 'atomic_json', capture):
            self.run_controller()
        self.assertEqual(len(split), 1)
        state, status = split[0]
        self.assertIn('exit_completed', state)
        self.assertNotEqual(status.get('exit_completed'), state['exit_completed'])
        # Rewind only fake test records to the exact crash boundary.
        self.write(self.directory / 'state.json', state)
        self.write(self.directory / 'status.json', status)
        recovering = self.real_engine(self.bd, self.directory)
        self.assertEqual(recovering.state['exit_after_restore'], command['id'])
        self.assertEqual(recovering.state['mode'], 'PAUSED')
        self.assertFalse(recovering.state['desired_enabled'])
        self.assertEqual(recovering.state['exit_resume_enabled'], preserve)
        self.write(self.directory / 'signal.json', self.connected())
        self.ticks.clear()
        self.run_controller()
        self.assertTrue(self.ticks)
        self.assertTrue(all(state['mode'] != 'ACTIVE' for state, _ in self.ticks))
        self.assert_completion(command)
        reopened = self.real_engine(self.bd, self.directory)
        self.assertEqual(reopened.state['settled_exit_command'], command['id'])
        self.assertNotIn('exit_after_restore', reopened.state)
        reopened.sleeper = lambda seconds: setattr(self, 'now', self.now + seconds)
        self.write(self.directory / 'signal.json', self.connected())
        reopened.tick(self.connected(), command)
        self.assertEqual(reopened.state['mode'], 'ACTIVE' if preserve else 'PAUSED')
        self.assertEqual(reopened.state['desired_enabled'], preserve)
        self.assertNotIn('exit_after_restore', reopened.state)
        # The settled marker survives later status changes and a further restart.
        final = self.real_engine(self.bd, self.directory)
        self.assertNotIn('exit_after_restore', final.state)

    def test_new_enable_waits_for_closing_worker_before_effective_active_sync(self):
        self.quit_command()
        self.cursor_outcomes = [dict(stopped=False, restored=False, pending_recovery=True),
                                dict(stopped=False, restored=False, pending_recovery=True),
                                dict(stopped=True, restored=False, pending_recovery=True)]
        enabled = dict(id='enable-while-worker-closes', action='enable')

        def waiting():
            if len(self.close_events) < 3:
                self.assertEqual(len(self.workers), 1)
                self.assertFalse(self.workers[0].stopped)
                self.write(self.directory / 'command.json', enabled)
                self.write(self.directory / 'signal.json', self.connected())

        def ticked():
            engine = self.engines[-1]
            if engine.state['last_command'] == enabled['id'] and engine.state['mode'] == 'ACTIVE':
                self.assertEqual(len(self.workers), 2)
                self.assertTrue(self.workers[0].stopped)
                self.assertFalse(self.workers[1].stopped)
                self.handlers[signal.SIGTERM](signal.SIGTERM, None)

        self.on_wait, self.on_tick = waiting, ticked
        self.run_controller()
        self.assertEqual(len(self.workers), 2)
        self.assertIn(True, self.workers[1].syncs)
        self.assertNotIn('exit_completed', c.read_json(self.directory / 'state.json'))

    def test_exception_during_explicit_quit_finally_keeps_unbounded_restore_handshake(self):
        command = self.quit_command()
        self.cursor_outcomes = [dict(stopped=True, restored=False, pending_recovery=True)
                                for _ in range(4)]
        failure = RuntimeError('coordinator failed after consuming explicit Quit')

        def ticked():
            self.assertEqual(self.engines[-1].state['exit_after_restore'], command['id'])
            raise failure

        self.on_tick = ticked
        with self.assertRaises(RuntimeError) as raised:
            self.run_controller()
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(self.workers), 5)
        self.assertEqual(self.waits, [5, 10, 20, 30])
        self.assert_completion(command)

    def test_previous_enable_baseline_cannot_cancel_fifo_quit(self):
        self.args.event_driven = True
        command = dict(id='fifo-quit', action='recover', exit_after_restore=True)
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', dict(id='old-enable', action='enable'))
        self.cursor_outcomes = [dict(stopped=True, restored=False, pending_recovery=True)]
        sent = []

        def waiting():
            if not sent and self.engines[-1].state['mode'] == 'ACTIVE':
                self.assertTrue(c.wake_controller(self.directory, command))
                sent.append(True)

        def ticked():
            if self.engines[-1].state.get('exit_after_restore'):
                self.assertEqual(self.engines[-1].state['exit_previous_command_id'], 'old-enable')
                self.write(self.directory / 'command.json', dict(id='old-enable', action='enable'))

        self.on_wait, self.on_tick = waiting, ticked
        self.run_controller()
        self.assertEqual(sent, [True])
        self.assertEqual(len(self.workers), 2)
        self.assert_completion(command)

    def test_three_cleanly_restored_fatal_runs_pause_and_old_enable_cannot_replay(self):
        self.write(self.directory / 'command.json', dict(id='original-enable', action='enable'))
        failures = []

        def ticked():
            self.assertEqual(self.engines[-1].state['mode'], 'ACTIVE')
            failure = RuntimeError('fatal active run ' + str(len(failures) + 1))
            failures.append(failure)
            raise failure

        self.on_tick = ticked
        for attempt in range(3):
            self.write(self.directory / 'signal.json', self.connected())
            with self.assertRaises(RuntimeError) as raised:
                self.run_controller()
            self.assertIs(raised.exception, failures[-1])
            saved = c.read_json(self.directory / 'state.json')
            self.assertIsNone(saved['journal'])
            self.assertEqual(len(saved['interrupted_restarts']), attempt + 1)
            self.assertEqual(saved['desired_enabled'], attempt < 2)
            self.assertEqual(saved['mode'], 'ARMED' if attempt < 2 else 'PAUSED')
            self.assertTrue(next(d for d in self.bd.devices if d['identity'] == 'physical1')['connected'])
            self.assertFalse(next(d for d in self.bd.devices if d['identity'] == c.TARGET_UUID)['connected'])
        self.args.once = True
        observed = []

        def paused():
            observed.append(self.engines[-1].state['mode'])
            self.assertEqual(self.engines[-1].state['last_command'], 'original-enable')

        self.on_tick = paused
        self.write(self.directory / 'signal.json', self.connected())
        self.run_controller()
        self.assertEqual(observed, ['PAUSED'])
        saved = c.read_json(self.directory / 'state.json')
        self.assertFalse(saved['desired_enabled'])
        self.assertEqual(len(saved['interrupted_restarts']), 3)
        self.assertNotIn('failure_recorded', saved)

    def test_fatal_with_pending_display_journal_is_counted_once_on_restart(self):
        self.write(self.directory / 'signal.json', self.connected())
        self.write(self.directory / 'command.json', dict(id='enable', action='enable'))
        failure = RuntimeError('fatal followed by unavailable display restore')

        def ticked():
            self.bd.fail = lambda device, fields: device['identity'] == 'physical1' and fields.get('connected')
            raise failure

        self.on_tick = ticked
        with self.assertRaises(RuntimeError) as raised:
            self.run_controller()
        self.assertIs(raised.exception, failure)
        saved = c.read_json(self.directory / 'state.json')
        self.assertIsNotNone(saved['journal'])
        self.assertTrue(saved['failure_recorded'])
        self.assertEqual(len(saved['interrupted_restarts']), 1)
        restarted = self.real_engine(self.bd, self.directory)
        self.assertEqual(len(restarted.state['interrupted_restarts']), 1)
        self.assertNotIn('failure_recorded', restarted.state)
        self.assertEqual(restarted.state['mode'], 'RESTORING')
        self.bd.fail = None
        restarted.sleeper = lambda seconds: setattr(self, 'now', self.now + seconds)
        restarted.tick({'profile_entry_blocked': True})
        self.assertIsNone(restarted.state['journal'])
        self.assertEqual(len(restarted.state['interrupted_restarts']), 1)

    def test_quit_preserve_enabled_cannot_override_exhausted_restart_budget(self):
        self.seed_active()
        engine = self.engines[-1]
        engine.state['interrupted_restarts'] = [self.now - 3, self.now - 2, self.now - 1]
        engine._save()
        command = self.quit_command()
        command['preserve_enabled_after_exit'] = True
        self.write(self.directory / 'command.json', command)
        self.run_controller()
        self.assert_completion(command)
        saved = c.read_json(self.directory / 'state.json')
        self.assertEqual(saved['mode'], 'PAUSED')
        self.assertFalse(saved['desired_enabled'])
        self.assertFalse(saved['exit_completed']['resume_enabled'])
        reopened = self.real_engine(self.bd, self.directory)
        reopened.tick(self.connected(), command)
        self.assertEqual(reopened.state['mode'], 'PAUSED')
        self.assertFalse(reopened.state['desired_enabled'])

    def test_corrupt_status_with_partial_completion_still_reconstructs_quit(self):
        command = self.quit_command()
        self.run_controller()
        saved = c.read_json(self.directory / 'state.json')
        self.assertIn('exit_completed', saved)
        (self.directory / 'status.json').write_text('{broken status')
        restarted = self.real_engine(self.bd, self.directory)
        self.assertEqual(restarted.state['exit_after_restore'], command['id'])
        self.assertEqual(restarted.state['mode'], 'PAUSED')
        self.assertFalse(restarted.state['desired_enabled'])
        self.assertNotIn('settled_exit_command', restarted.state)
        self.run_controller()
        self.assert_completion(command)


if __name__ == '__main__':
    unittest.main()
