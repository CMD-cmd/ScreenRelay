"""Published model specifications, metadata, and single-identity panel switching."""
import copy
import unittest
from urllib.parse import urlparse

import controller as c
import device_profiles as p
import test_profile_runtime as fixtures


class CatalogCoverageTests(unittest.TestCase):
    def test_supported_model_inventory_and_exact_name_matching(self):
        phones = [x for x in p.CATALOG if x['id'].startswith('iphone-')]
        pads = [x for x in p.CATALOG if x['id'].startswith('ipad-')]
        self.assertEqual(len(phones), 37)
        self.assertEqual(len(pads), 30)
        home = {x['id'] for x in p.CATALOG if x['hardware'].endswith('home_button')}
        self.assertEqual(home, {'iphone-se-2','iphone-se-3','ipad-7','ipad-8','ipad-9','ipad-mini-5','ipad-air-3'})
        keys = {}
        config = dict(version=1,automatic=True,selected_id=p.LEGACY_ID,profiles=[p.legacy_profile()],bindings={})
        for item in p.CATALOG:
            with self.subTest(item=item['id']):
                self.assertIn(urlparse(item['source_url']).hostname, {'support.apple.com','www.apple.com'})
                for name in [item['label'], *item['aliases']]:
                    key = p.normalize_name(name)
                    self.assertIn(keys.get(key), (None,item['id']))
                    keys[key] = item['id']
                    self.assertEqual(p.identify([name],config)['catalog_id'],item['id'])
                profile = p.make_profile(item['id'])
                snapshot = p.profile_snapshot(profile)
                self.assertEqual(snapshot['hardware'],item['hardware'])
                self.assertNotIn('hardware',profile)
        for name in ['iPad','iPad Pro','iPhone','iPhone SE']:
            self.assertNotEqual(p.identify([name],config)['status'],'matched')

    def test_every_catalog_model_enters_and_restores_using_its_metadata(self):
        for item in p.CATALOG:
            with self.subTest(item=item['id']):
                f = fixtures.ProfileRuntimeTests(); f.setUp()
                try:
                    f.tick(f.signal(names=[item['label']]),action='enable')
                    self.assertEqual(f.engine.state['mode'],'ACTIVE')
                    profile = f.coordinator._profile()
                    self.assertEqual(profile['catalog_id'],item['id'])
                    self.assertEqual(f.engine._preferred_mode()['hiDPI'],'on')
                    f.tick(f.signal('disconnected',[]),action='pause')
                    self.assertTrue(f.bd.devices[0]['connected'])
                    self.assertIsNone(f.engine.state['journal'])
                finally:
                    f.tearDown()

    def test_panel_selection_is_validated_and_keeps_one_identity(self):
        outer = p.make_profile('iphone-duo')
        inner = p.make_profile('iphone-duo',id=outer['id'],virtual=outer['virtual'],display_variant='inner')
        self.assertEqual(outer['display_variant'],'outer')
        self.assertEqual((inner['native_width'],inner['native_height'],inner['ppi']),(2670,1878,430))
        self.assertEqual(outer['virtual'],inner['virtual'])
        for variant in ['outer','inner']:
            for orientation in ['landscape','portrait']:
                profile = p.make_profile('iphone-duo',display_variant=variant,orientation=orientation)
                for mode in p.presets(profile):
                    p.validate_profile(dict(profile,resolution=mode))
        for broken in [dict(outer,display_variant='unknown'),dict(outer,display_variant=None),
                       dict(outer,display_variant='inner'),dict(p.make_profile('iphone-16-pro-max'),display_variant='outer')]:
            with self.assertRaises(ValueError):p.validate_profile(broken)


class PanelRuntimeTests(unittest.TestCase):
    setUp = fixtures.ProfileRuntimeTests.setUp
    tearDown = fixtures.ProfileRuntimeTests.tearDown
    advance = fixtures.ProfileRuntimeTests.advance
    signal = fixtures.ProfileRuntimeTests.signal
    tick = fixtures.ProfileRuntimeTests.tick

    def test_live_panel_roundtrip_keeps_profile_virtual_and_physical_baseline(self):
        self.tick(self.signal(names=['iPhone Duo']),action='enable')
        first = copy.deepcopy(self.coordinator._profile())
        baseline = copy.deepcopy({k:self.engine.state['journal'][k] for k in ('physical','created','protections')})
        self.bd.calls.clear()
        for panel in ['inner','outer']:
            self.tick(self.signal(names=['iPhone Duo']),action='profile_apply',profile_id=first['id'],draft={'display_variant':panel})
            self.assertEqual(self.coordinator.last_result['status'],'ok')
            profile = self.coordinator._profile()
            self.assertEqual(profile['display_variant'],panel)
            self.assertEqual(profile['resolution'], '1068x751' if panel == 'inner' else '814x559')
            self.assertEqual(profile['virtual'],first['virtual'])
            self.assertEqual(profile['cursor_scale'],first['cursor_scale'])
            self.assertEqual({k:self.engine.state['journal'][k] for k in baseline},baseline)
            self.assertFalse(self.engine.state['journal'].get('transient_virtuals'))
        self.assertFalse(any(i=='physical1' and f.get('connected') is True for i,f in self.bd.calls))
        self.assertEqual(len([x for x in self.coordinator.config['profiles'] if x['catalog_id']=='iphone-duo']),1)
        self.tick(self.signal('disconnected',[]),action='pause')
        self.assertTrue(self.bd.devices[0]['connected'])

    def test_arbitrary_panel_dimensions_rejected_before_output_change(self):
        self.tick(self.signal(names=['iPhone Duo']),action='enable')
        first = copy.deepcopy(self.coordinator._profile()); self.bd.calls.clear()
        self.tick(self.signal(names=['iPhone Duo']),action='profile_apply',profile_id=first['id'],
                  draft={'display_variant':'inner','native_width':2400,'native_height':1878,'ppi':430})
        self.assertEqual(self.coordinator.last_result['status'],'error')
        self.assertEqual(self.coordinator._profile(),first)
        self.assertEqual(self.bd.calls,[])
