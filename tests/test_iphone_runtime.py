"""iPhone discovery, independent settings and recovery use only FakeBD."""
import copy
import unittest

import controller as c
import device_profiles as p
import test_profile_runtime as fixtures


class IPhoneRuntimeTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick
    enable = fixtures.ProfileRuntimeTests.enable

    def connect(self, name):
        for _ in range(3):
            self.advance(.2)
            self.tick(self.signal(names=[name]))

    def phone(self):
        return next(x for x in self.coordinator.config['profiles'] if x.get('catalog_id') == 'iphone-16-pro-max')

    def output(self, profile):
        return next(d for d in self.bd.devices if d['identity'] == profile['virtual']['identity'])

    def test_iphone_discovers_and_reconnects_without_duplicate_virtual(self):
        self.tick(self.signal(names=['iPhone16ProMax']), action='enable')
        phone = copy.deepcopy(self.phone())
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(phone['resolution'], '1147x528')
        self.assertEqual(phone['cursor_scale'], 4)
        self.assertEqual(self.output(phone)['hiDPI'], 'on')
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json')['scale'], 4)
        for _ in range(3):
            self.advance(4); self.tick(self.signal('disconnected', []))
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.output(phone)['connected'])
        self.connect('iPhone 16 Pro Max')
        self.assertEqual(self.engine.state['mode'], 'ACTIVE')
        self.assertEqual(self.phone(), phone)
        self.assertEqual(len(self.coordinator.config['profiles']), 2)
        self.assertEqual(len([d for d in self.bd.devices if d['identity'] == phone['virtual']['identity']]), 1)

    def test_ipad_phone_handover_remembers_independent_preferences(self):
        self.enable()
        baseline = copy.deepcopy({k:self.engine.state['journal'][k] for k in ('physical','created','protections')})
        self.bd.calls.clear()
        self.connect('iPhone 16 Pro Max')
        phone = self.phone()
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='profile_apply', profile_id=phone['id'],
                  draft={'orientation':'portrait','resolution':'440x956','cursor_scale':2.5})
        self.assertEqual(self.coordinator.last_result['status'], 'ok')
        self.assertEqual(self.output(phone)['resolution'], '440x956')
        identity = phone['virtual']['identity']
        self.connect('iPad Pro (13 英寸, M4)')
        self.assertEqual(self.engine.state['active_profile_id'], p.LEGACY_ID)
        self.assertEqual(self.coordinator._profile()['resolution'], '1280x960')
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json')['scale'], 3)
        self.assertFalse(self.output(phone)['connected'])
        self.connect('iPhone 16 Pro Max')
        self.assertEqual(self.phone()['virtual']['identity'], identity)
        self.assertEqual(self.phone()['orientation'], 'portrait')
        self.assertEqual(self.phone()['resolution'], '440x956')
        self.assertEqual(c.read_json(self.directory/'cursor-profile-config.json')['scale'], 2.5)
        self.assertEqual({k:self.engine.state['journal'][k] for k in baseline}, baseline)
        self.assertFalse(any(i=='physical1' and f.get('connected') is True for i,f in self.bd.calls))
        self.tick(self.signal('disconnected', []), action='pause')
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.output(phone)['connected'])

    def test_other_iphones_use_their_own_identity(self):
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='enable')
        first = copy.deepcopy(self.phone())
        self.connect('iPhone 12 mini')
        second = self.coordinator._profile()
        self.assertEqual(second['catalog_id'], 'iphone-12-mini')
        self.assertEqual((second['native_width'],second['native_height']), (2340,1080))
        self.assertNotEqual(first['virtual']['identity'],second['virtual']['identity'])
        self.assertFalse(self.output(first)['connected'])
        self.assertTrue(self.output(second)['connected'])

    def test_phone_rotation_pause_keeps_physical_recovery(self):
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='enable')
        phone=copy.deepcopy(self.phone()); original=self.bd.set; fired=[]
        def setting(device, **fields):
            original(device, **fields)
            if not fired and device['kind']=='VirtualScreen' and device['identity']!=phone['virtual']['identity'] and fields.get('connected') is True:
                fired.append(device['identity'])
                c.atomic_json(self.directory/'command.json', {'id':'phone-pause','action':'pause'})
        self.bd.set=setting
        self.tick(self.signal(names=['iPhone 16 Pro Max']), action='profile_apply',profile_id=phone['id'],
                  draft={'orientation':'portrait','resolution':'528x1147'})
        self.assertTrue(fired)
        self.assertEqual(self.engine.state['mode'],'PAUSED')
        self.assertIsNone(self.engine.state['journal'])
        self.assertTrue(self.bd.devices[0]['connected'])
        self.assertFalse(self.output(phone)['connected'])
        self.assertFalse(any(d['identity']==fired[0] for d in self.bd.devices))

    def test_catalog_snapshot_matches_auto_creation_for_all_iphones(self):
        phones=[x for x in p.catalog_snapshot() if x['id'].startswith('iphone-')]
        self.assertEqual(len(phones),37)
        for item in phones:
            with self.subTest(item=item['id']):
                profile=p.make_profile(item['id'])
                self.assertEqual(profile['resolution'],item['resolution'])
                self.assertEqual(profile['cursor_scale'],item['cursor_scale'])
                self.assertEqual(p.identify([item['label']],self.coordinator.config)['catalog_id'],item['id'])
                self.assertEqual(item['presets'],p.presets(profile))
                self.assertTrue(item['source_url'].startswith('https://support.apple.com/'))
        self.assertEqual(p.identify(['iPhone'],self.coordinator.config)['status'],'unknown')
        self.assertEqual(p.identify(['iPhone 16'],self.coordinator.config)['catalog_id'],'iphone-16')
