import copy
import json
import unittest
from unittest.mock import patch
import controller as c
import device_profiles as p
from profile_runtime import DeviceCoordinator
import test_profile_runtime as fixtures


class MultiDeviceTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick
    enable = fixtures.ProfileRuntimeTests.enable

    def add_b(self, same_model=False):
        profile = p.make_profile(p.LEGACY_ID if same_model else 'ipad-mini-a17-pro', id='device-b',
                                 orientation='portrait', cursor_scale=2)
        cfg = copy.deepcopy(self.coordinator.config)
        cfg['profiles'].append(profile)
        cfg['bindings'].update({p.normalize_name('Tablet A'): p.LEGACY_ID,
                                p.normalize_name('Tablet B'): profile['id']})
        self.coordinator.config = self.coordinator.store.save(cfg)
        return profile

    def connect(self, name):
        for _ in range(3):
            self.advance(.2)
            self.tick(self.signal(names=[name]))
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')

    def test_a_b_a_keeps_three_preferences_and_separate_identity(self):
        b = self.add_b()
        self.tick(self.signal(names=['Tablet A']), action='enable')
        original = copy.deepcopy(self.coordinator._profile())
        self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'], b['id'])
        self.assertEqual(self.engine._preferred_mode()['resolution'], b['resolution'])
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json')['scale'], 2)
        self.assertFalse(next(d for d in self.bd.devices if d['identity']==c.TARGET_UUID)['connected'])
        self.connect('Tablet A')
        self.assertEqual(self.coordinator._profile(), original)
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json')['scale'], 3)
        self.assertEqual(self.coordinator._profile(b['id']), b)
        self.assertEqual(sum(d['connected'] for d in self.bd.devices), 1)

    def test_short_disconnect_then_other_device(self):
        b = self.add_b()
        self.tick(self.signal(names=['Tablet A']), action='enable')
        self.tick(self.signal('disconnected', []))
        self.advance(1)
        self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'], b['id'])

    def test_multi_to_single_switches_only_after_owner_leaves(self):
        b = self.add_b()
        self.tick(self.signal(names=['Tablet A']), action='enable')
        calls = len(self.bd.calls)
        self.tick(self.signal(names=['Tablet A','Tablet B'], activeControllerCount=2))
        self.assertEqual(len(self.bd.calls),calls)
        self.assertEqual(self.engine.state['active_profile_id'],p.LEGACY_ID)
        self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'],b['id'])

    def test_held_or_stale_names_cannot_switch(self):
        self.add_b();self.tick(self.signal(names=['Tablet A']),action='enable')
        for extra in ({'session_held':True}, {'session_observed_at':self.now-100}):
            detector = self.signal(names=['Tablet B']);detector.update(extra)
            self.tick(detector)
            self.assertEqual(self.engine.state['active_profile_id'],p.LEGACY_ID)

    def test_unknown_replacement_restores_then_waits_without_guessing(self):
        self.enable();self.tick(self.signal(names=['Unknown Tablet']))
        self.tick(self.signal(names=['Unknown Tablet']))
        self.assertEqual(self.engine.state['mode'],'ARMED')
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_same_model_same_label_has_distinct_screen_names(self):
        a = p.make_profile('ipad-mini-a17-pro',id='mini-a')
        b = p.make_profile('ipad-mini-a17-pro',id='mini-b')
        self.assertNotEqual(a['virtual']['name'],b['virtual']['name'])
        cfg=copy.deepcopy(self.coordinator.config);cfg['profiles'] += [a,b]
        cfg['bindings'].update({p.normalize_name('Tablet A'):a['id'],p.normalize_name('Tablet B'):b['id']})
        self.coordinator.config=self.coordinator.store.save(cfg)
        self.tick(self.signal(names=['Tablet A']),action='enable');self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'],b['id'])

    def test_failed_handover_retains_configs_and_recovers_physical(self):
        self.add_b();self.tick(self.signal(names=['Tablet A']),action='enable')
        before=copy.deepcopy(self.coordinator.config)
        self.provisioner.fail=True
        self.tick(self.signal(names=['Tablet B']));self.tick(self.signal(names=['Tablet B']))
        self.assertEqual(self.engine.state['mode'],'ARMED')
        self.assertEqual(self.coordinator.config,before)
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.bd.devices[1]['connected'])

    def test_restore_pending_rejects_new_profile_and_cursor_edit(self):
        self.add_b();self.enable()
        self.bd.fail=lambda device,fields: device['kind']=='Display' and fields.get('connected') is True
        self.tick(action='pause');self.assertEqual(self.engine.state['mode'],'RESTORING')
        before=copy.deepcopy(self.coordinator.config);cursor=c.read_json(self.directory/'cursor-profile-config.json')
        self.tick(action='profile_select',profile_id='device-b')
        self.tick(action='profile_cursor',scale=1.5)
        self.assertEqual(self.coordinator.config,before)
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json'),cursor)
        self.assertEqual(self.coordinator.last_result['status'],'error')

    def test_switch_during_entry_aborts_before_hiding_physical(self):
        self.add_b()
        original=self.bd.set
        def setting(device,**fields):
            original(device,**fields)
            if device['kind']=='VirtualScreen' and fields.get('connected') is True:
                c.atomic_json(self.directory/'signal.json',self.signal(names=['Tablet B']))
        self.bd.set=setting
        c.atomic_json(self.directory/'signal.json',self.signal(names=['Tablet A']))
        self.tick(self.signal(names=['Tablet A']),action='enable')
        self.assertNotEqual(self.engine.state['mode'],'ACTIVE')
        self.assertTrue(self.bd.devices[0]['connected'])

    def test_second_controller_during_entry_aborts_even_if_names_lag(self):
        self.add_b()
        original=self.bd.set
        def setting(device,**fields):
            original(device,**fields)
            if device['kind']=='VirtualScreen' and fields.get('connected') is True:
                c.atomic_json(self.directory/'signal.json',self.signal(names=['Tablet A'],activeControllerCount=2))
        self.bd.set=setting
        c.atomic_json(self.directory/'signal.json',self.signal(names=['Tablet A'],activeControllerCount=1))
        self.tick(self.signal(names=['Tablet A']),action='enable')
        self.assertNotEqual(self.engine.state['mode'],'ACTIVE')
        self.assertTrue(self.bd.devices[0]['connected'])

    def test_held_snapshot_cannot_apply_different_device(self):
        self.add_b();self.enable();calls=len(self.bd.calls)
        self.tick(self.signal(session_held=True,raw_state='unknown'),action='profile_select',profile_id='device-b')
        self.assertEqual(self.coordinator.last_result['status'],'error')
        self.assertEqual(self.engine.state['active_profile_id'],p.LEGACY_ID)
        self.assertEqual(len(self.bd.calls),calls)

    def test_cursor_shortcut_with_wrong_profile_id_cannot_change_current_device(self):
        b=self.add_b();self.enable();before=copy.deepcopy(self.coordinator.config)
        self.tick(action='profile_cursor',profile_id=b['id'],scale=1.5)
        self.assertEqual(self.coordinator.last_result['status'],'error')
        self.assertEqual(self.coordinator.config,before)
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json')['scale'],3)

    def test_count_change_with_empty_names_interrupts_entry(self):
        self.add_b();original=self.bd.set
        def setting(device,**fields):
            original(device,**fields)
            if device['kind']=='VirtualScreen' and fields.get('connected') is True:
                c.atomic_json(self.directory/'signal.json',self.signal(names=[],activeControllerCount=2))
        self.bd.set=setting
        c.atomic_json(self.directory/'signal.json',self.signal(names=['Tablet A'],activeControllerCount=1))
        self.tick(self.signal(names=['Tablet A']),action='enable')
        self.assertNotEqual(self.engine.state['mode'],'ACTIVE')
        self.assertTrue(self.bd.devices[0]['connected'])

    def test_restart_recovers_old_screen_before_using_new_profile(self):
        b=self.add_b();self.tick(self.signal(names=['Tablet A']),action='enable')
        self.engine=c.Engine(self.bd,self.directory,clock=lambda:self.now,monotonic=lambda:self.now,
                             sleeper=self.advance,signal_max_age=90,event_driven=True)
        self.coordinator=DeviceCoordinator(self.engine,self.provisioner)
        self.assertEqual(self.engine.state['mode'],'RESTORING')
        self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'],b['id'])
        self.assertEqual(self.engine.state['journal']['physical']['physical1']['resolution'],'1920x1080')

    def test_deleted_offline_definition_is_recreated_with_same_identity(self):
        b=self.add_b();self.tick(self.signal(names=['Tablet B']),action='enable')
        for _ in range(3):self.advance(4);self.tick(self.signal('disconnected',[]))
        self.bd.devices=[d for d in self.bd.devices if d['identity']!=b['virtual']['identity']]
        self.connect('Tablet B')
        self.assertEqual(self.engine.state['active_profile_id'],b['id'])
        self.assertEqual(sum(d['identity']==b['virtual']['identity'] for d in self.bd.devices),1)

    def test_other_owned_screen_is_closed_without_touching_unowned_definition(self):
        b=self.add_b();other=self.provisioner.ensure(b);other['connected']=True
        self.tick(self.signal(names=['Tablet A']),action='enable')
        self.assertEqual(sum(d['connected'] for d in self.bd.devices),1)
        self.assertFalse(next(d for d in self.bd.devices if d['identity']==b['virtual']['identity'])['connected'])
        self.assertIn(b['virtual']['identity'],self.engine.state['journal']['other_managed_virtuals'])

    def test_same_profile_alias_does_not_lose_manual_preferences(self):
        self.add_b();self.tick(self.signal(names=['Tablet A']),action='enable')
        self.tick(self.signal(names=['Tablet A']),action='profile_apply',profile_id=p.LEGACY_ID,
                  draft={'orientation':'portrait','resolution':'900x1200','cursor_scale':2.5})
        self.connect('Tablet B');self.connect('Tablet A')
        a=self.coordinator._profile()
        self.assertEqual((a['orientation'],a['resolution'],a['cursor_scale']),('portrait','900x1200',2.5))
