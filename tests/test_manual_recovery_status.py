"""A successful retry must clear a manual restore's resolved error."""
import unittest
import test_controller as fixtures


class ManualRecoveryStatusTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    make_engine = fixtures.ControllerTests.make_engine
    advance = fixtures.ControllerTests.advance
    enable = fixtures.ControllerTests.enable

    def test_manual_recovery_retry_clears_resolved_failure(self):
        self.enable()
        self.bd.fail = lambda device, fields: device['identity'] == 'physical1' and fields.get('connected')
        self.engine.tick({}, {'id': 'pause', 'action': 'pause'})
        self.assertEqual(self.engine.state['mode'], 'RESTORING')
        self.assertIsNotNone(self.engine.state['journal'])
        self.assertIn('Timed out', self.engine.state['last_error'])
        self.bd.fail = None
        self.advance(31)
        self.engine.tick({'profile_entry_blocked': True})
        self.assertEqual(self.engine.state['mode'], 'PAUSED')
        self.assertFalse(self.engine.state['desired_enabled'])
        self.assertIsNone(self.engine.state['journal'])
        self.assertIsNone(self.engine.state['last_error'])


if __name__ == '__main__':
    unittest.main()
