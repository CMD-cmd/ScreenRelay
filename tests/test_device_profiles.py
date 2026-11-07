import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('device_profiles', Path(__file__).resolve().parents[1] / 'device_profiles.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def config(*profiles):
    profiles = list(profiles) or [p.legacy_profile()]
    return dict(version=1, automatic=True, selected_id=profiles[0]['id'], profiles=profiles, bindings={})


class DeviceProfileTests(unittest.TestCase):
    def test_legacy_identity_and_modes(self):
        profile = p.legacy_profile('1200x900', 1)
        self.assertEqual(profile['id'], 'ipad-pro-13-m4')
        self.assertEqual(profile['virtual']['name'], 'iPad Pro 13')
        self.assertEqual(p.target_spec(profile), dict(name='iPad Pro 13', identities=[p.LEGACY_IDENTITY, p.LEGACY_UUID]))
        self.assertEqual(p.preferred_mode(profile), dict(resolution='1200x900', hiDPI='on', refreshRate='60'))
        self.assertEqual(p.presets(profile), ['1376x1032', '1280x960', '1200x900', '1152x864', '1024x768'])
        self.assertEqual(p.presets(profile, 'portrait'), ['1032x1376', '960x1280', '900x1200', '864x1152', '768x1024'])

    def test_migration_is_once_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = p.ProfileStore(directory)
            self.assertIsNone(store.load())
            migrated = store.bootstrap('1152x864', 2.5)
            self.assertEqual(migrated['profiles'][0]['cursor_scale'], 2.5)
            original = store.path.read_bytes()
            self.assertEqual(store.bootstrap('1024x768', 3), migrated)
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(store.path.name, 'profiles.json')

    def test_corrupt_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = p.ProfileStore(directory)
            for damaged in ['{', '{}', '{"version": 1, "version": 1}', 'null']:
                store.path.write_text(damaged)
                for operation in [store.load, store.bootstrap, lambda: store.save(config())]:
                    with self.assertRaises(ValueError):
                        operation()
                    self.assertEqual(store.path.read_text(), damaged)

    def test_atomic_failure_leaves_file_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = p.ProfileStore(directory)
            store.bootstrap()
            original = store.path.read_bytes()
            changed = config()
            changed['automatic'] = False
            with patch.object(p.os, 'replace', side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    store.save(changed)
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [store.path])
            with patch.object(p.os, 'fsync', wraps=p.os.fsync) as sync:
                self.assertEqual(store.save(changed), store.load())
                self.assertGreaterEqual(sync.call_count, 2)

    def test_all_catalog_rotations_and_presets_fit(self):
        self.assertEqual(len({item['id'] for item in p.CATALOG}), len(p.CATALOG))
        self.assertTrue({'ipad-pro-13-m4', 'iphone-16-pro-max', 'iphone-12-mini', 'iphone-air'} <= {i['id'] for i in p.CATALOG})
        for item in p.CATALOG:
            for orientation in ['landscape', 'portrait']:
                with self.subTest(item=item['id'], orientation=orientation):
                    profile = p.make_profile(item['id'], orientation=orientation)
                    self.assertNotEqual(profile['virtual']['identity'], p.LEGACY_IDENTITY)
                    self.assertTrue(profile['virtual']['name'].startswith('UU · ' + profile['label'] + ' · '))
                    self.assertEqual(profile['resolution'], p.presets(profile)[2 if item['id'].startswith('iphone-') else 1])
                    for mode in p.presets(profile):
                        candidate = dict(profile, resolution=mode)
                        self.assertEqual(p.validate_profile(candidate), candidate)
                    other = 'portrait' if orientation == 'landscape' else 'landscape'
                    self.assertEqual(p.presets(profile, other), ['x'.join(reversed(m.split('x'))) for m in p.presets(profile)])

    def test_descriptor_stability_and_custom_device(self):
        first = p.make_profile(id='my-tablet', label='自定义平板', native_width=3001, native_height=2001, ppi=300)
        second = p.make_profile(id='my-tablet', label='renamed', native_width=3001, native_height=2001, ppi=300)
        for key in ['vendor', 'model', 'serial', 'identity']:
            self.assertEqual(first['virtual'][key], second['virtual'][key])
        self.assertEqual(first['cursor_scale'], 3.5)
        self.assertNotEqual(first['virtual']['identity'], p.make_profile(id='different', label='x', native_width=3001, native_height=2001, ppi=300)['virtual']['identity'])
        for ppi, expected in [(50, 1.5), (1000, 4), (264, 3)]:
            self.assertEqual(p.make_profile(label='custom', native_width=2000, native_height=1500, ppi=ppi)['cursor_scale'], expected)

    def test_name_matching_generation_binding_and_multiple_controllers(self):
        cfg = config()
        exact = p.identify([' iPad Pro （13 英寸， M4） '], cfg)
        self.assertEqual(exact['profile_id'], p.LEGACY_ID)
        self.assertEqual(exact['status'], 'matched')
        for name in ['iPad Pro 13', 'iPad Pro', 'iPad', 'iPad Pro 13 M2', 'iPad mini']:
            self.assertIsNone(p.identify([name], cfg)['profile_id'])
        self.assertEqual(p.identify([], cfg)['status'], 'none')
        for names in [['iPad', 'iPad'], ['iPad', 'another'], [p.CATALOG[0]['label'], p.CATALOG[1]['label']]]:
            self.assertEqual(p.identify(names, cfg)['status'], 'multiple')
        cfg['bindings'] = {'My iPad': p.LEGACY_ID}
        bound = p.identify(['MY　iPad'], cfg)
        self.assertEqual(bound['source'], 'binding')
        self.assertEqual(bound['profile_id'], p.LEGACY_ID)
        absent = p.identify(['iPad Air (13 英寸, M2)'], cfg)
        self.assertEqual(absent['catalog_id'], 'ipad-air-13-m2')
        self.assertIsNone(absent['profile_id'])
        duplicate = p.make_profile(p.LEGACY_ID, id='second-pro')
        self.assertEqual(p.identify([p.CATALOG[0]['label']], config(p.legacy_profile(), duplicate))['status'], 'ambiguous')

    def test_strict_validation(self):
        profile = p.legacy_profile()
        bad_values = [('id', ''), ('id', '../bad'), ('label', '\n'), ('native_width', True),
                      ('native_height', 0), ('native_width', 10**1000), ('ppi', float('nan')), ('ppi', True),
                      ('cursor_scale', float('inf')), ('cursor_scale', 0), ('cursor_scale', True),
                      ('orientation', 'up'), ('resolution', '1032x1376'), ('resolution', '1378x1032'),
                      ('resolution', '1200x1000'), ('resolution', '0x0'), ('catalog_id', 'unknown')]
        for key, value in bad_values:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                p.validate_profile(dict(profile, **{key: value}))
        for key, value in [('vendor', True), ('model', -1), ('serial', 2**32), ('identity', 'wrong'), ('legacy_uuid', 'wrong')]:
            broken = copy.deepcopy(profile)
            broken['virtual'][key] = value
            with self.assertRaises(ValueError):
                p.validate_profile(broken)
        self.assertEqual(p.validate_profile(dict(profile, resolution='1377x1033'))['resolution'], '1377x1033')
        for change in [{'version': True}, {'automatic': 1}, {'selected_id': 'missing'}, {'bindings': {'a': 'missing'}}, {'profiles': [profile, profile]}]:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                p.ProfileStore(directory).save(dict(config(), **change))
        for invalid in [None, 'names', [None], [''], [' () ']]:
            with self.assertRaises(ValueError):
                p.identify(invalid, config())
        with self.assertRaises(ValueError):
            p.make_profile(label='incomplete')


if __name__ == '__main__':
    unittest.main()
