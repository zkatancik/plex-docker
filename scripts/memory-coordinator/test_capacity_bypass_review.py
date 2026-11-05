"""Independent capacity bypass regressions; in-memory clocks/state only."""
import unittest

import test_warning_policy as warning_fixtures
from coordinator import APIError, Coordinator


class CapacityBypassReviewTests(unittest.TestCase):
    setUp = warning_fixtures.WarningAdmissionTests.setUp
    submit = warning_fixtures.WarningAdmissionTests.submit
    step = warning_fixtures.WarningAdmissionTests.step
    qualify = warning_fixtures.WarningAdmissionTests.qualify
    stop = warning_fixtures.WarningAdmissionTests.stop

    def restart(self):
        self.engine = Coordinator(self.store, self.health, self.engine.config,
            clock=self.wall, monotonic_clock=self.clock)

    def test_native_warning_head_can_be_bypassed_but_never_warning_granted(self):
        self.submit('native-head', 512, guest=False)
        self.submit('guest-one', 1024)
        self.submit('guest-two', 1024)
        self.submit('guest-three', 1024)
        self.qualify()
        for key in ('guest-one', 'guest-two'):
            result = self.engine.get(key)
            self.assertEqual(result['state'], 'granted')
            self.assertEqual(result['admission_mode'], 'warning')
            self.stop(result)
        self.assertEqual(self.engine.get('guest-three')['state'], 'queued')
        self.assertIn('age_reservation', self.engine.get('native-head')['reason'])
        for _ in range(24):
            self.step()
        third = self.engine.get('guest-three')
        self.assertEqual(third['state'], 'granted')
        self.assertEqual(self.engine.state['requests']['native-head']['state'], 'queued')
        self.stop(third)
        self.health.pressure = 1
        self.engine.tick()
        for _ in range(12):
            self.step()
        native = self.engine.get('native-head')
        self.assertEqual(native['state'], 'granted')
        self.assertEqual(native['admission_mode'], 'normal')

    def test_large_capacity_head_never_forced_even_after_repeated_reservations(self):
        self.health.available = 4607
        created = self.submit('large-head', 4096)['created_at']
        self.qualify()
        for cycle in range(3):
            for i in range(2):
                key = 'small-%d-%d' % (cycle, i)
                self.submit(key, 512)
                result = self.engine.get(key)
                self.assertEqual(result['state'], 'granted')
                self.stop(result)
            self.assertIn('age_reservation', self.engine.get('large-head')['reason'])
            for _ in range(24):
                self.step()
            self.assertEqual(self.engine.get('large-head')['state'], 'queued')
        self.assertEqual(self.engine.state['requests']['large-head']['created_at'], created)
        self.assertEqual(self.engine.state['requests']['large-head']['peak_mib'], 4096)
        self.health.available = 4608
        self.assertEqual(self.engine.get('large-head')['state'], 'granted')

    def test_oldest_fitting_absent_owner_expires_without_incidental_grant(self):
        self.health.available = 2500
        self.submit('large-head', 4096)
        self.submit('absent-fit', 1024)
        self.submit('younger-fit', 512)
        self.qualify()
        self.engine.status()
        self.engine.tick()
        self.assertIsNone(self.engine.state['active'])
        self.assertEqual(self.engine.get('younger-fit')['state'], 'queued')
        for _ in range(6):
            self.clock.advance(5)
            self.wall.advance(5)
            self.engine.get('large-head')
            result = self.engine.get('younger-fit')
        self.assertEqual(self.engine.state['requests']['absent-fit']['state'], 'expired')
        self.assertNotIn('granted_at', self.engine.state['requests']['absent-fit'])
        self.assertEqual(result['state'], 'granted')

    def test_queued_restart_preserves_ids_counts_and_requalifies_without_auto_grant(self):
        self.health.available = 2500
        self.submit('large-head', 4096)
        self.submit('small-one', 512)
        self.submit('small-two', 512)
        self.submit('small-three', 512)
        self.qualify()
        self.stop(self.engine.get('small-one'))
        self.stop(self.engine.get('small-two'))
        self.step(5)
        queue = list(self.engine.state['queue'])
        created = self.engine.state['requests']['large-head']['created_at']
        self.restart()
        self.assertEqual(self.engine.state['queue'], queue)
        self.assertEqual(self.engine.state['requests']['large-head']['capacity_bypass_count'], 2)
        self.assertEqual(self.engine.state['requests']['large-head']['created_at'], created)
        self.assertFalse(self.engine.warning_window.ready)
        self.assertEqual(self.engine.capacity_reservation, {})
        self.qualify()
        self.assertIsNone(self.engine.state['active'])
        self.assertEqual(self.engine.get('small-three')['state'], 'queued')
        for _ in range(24):
            self.step()
        self.assertIsNone(self.engine.state['active'])
        self.assertEqual(self.engine.get('small-three')['state'], 'granted')

    def test_restarted_active_bypass_remains_orphan_until_exact_owner_confirms_stop(self):
        self.health.available = 2500
        self.submit('large-head', 4096)
        self.submit('small-one', 512)
        self.submit('small-two', 512)
        self.qualify()
        first = self.engine.get('small-one')
        self.restart()
        self.qualify()
        self.assertEqual(self.engine.state['active'], 'small-one')
        self.assertEqual(self.engine.get('small-one')['state'], 'blocked_orphan')
        self.assertEqual(self.engine.get('small-two')['state'], 'queued')
        for body in ({'lease_id': 'wrong', 'stopped': True}, {'lease_id': first['lease_id']}):
            with self.assertRaises(APIError):
                self.engine.finish('small-one', body)
            self.assertEqual(self.engine.state['active'], 'small-one')
            self.assertEqual(self.engine.state['requests']['large-head'].get('capacity_bypass_count', 0), 0)
        self.stop(first)
        self.assertEqual(self.engine.state['requests']['large-head']['capacity_bypass_count'], 1)
        self.assertIsNone(self.engine.state['active'])
        self.assertEqual(self.engine.get('small-two')['state'], 'granted')

    def test_critical_unknown_and_maintenance_block_all_bypass_candidates(self):
        for field, value in [('pressure', 4), ('pressure', None), ('maintenance', True)]:
            with self.subTest(field=field, value=value):
                self.setUp()
                self.health.available = 2500
                self.submit('large-head', 4096)
                self.submit('small-one', 512)
                self.qualify()
                setattr(self.health, field, value)
                self.step()
                self.assertEqual(self.engine.get('small-one')['state'], 'queued')
                self.assertIsNone(self.engine.state['active'])

    def test_unknown_guest_capacity_never_uses_small_declared_peak_as_fallback(self):
        self.submit('large-head', 4096)
        self.submit('small-one', 512)
        self.qualify()
        self.health.available = None
        self.assertEqual(self.engine.get('small-one')['state'], 'queued')
        self.assertIsNone(self.engine.state['active'])

    def test_cancelling_reserved_head_removes_hold_without_granting_successor(self):
        self.health.available = 2500
        self.submit('large-head', 4096)
        self.submit('small-one', 512)
        self.submit('small-two', 512)
        self.submit('small-three', 512)
        self.qualify()
        self.stop(self.engine.get('small-one'))
        self.stop(self.engine.get('small-two'))
        self.assertIn('large-head', self.engine.capacity_reservation)
        self.engine.finish('large-head', {}, cancel=True)
        self.assertNotIn('large-head', self.engine.capacity_reservation)
        self.assertIsNone(self.engine.state['active'])
        self.assertEqual(self.engine.get('small-three')['state'], 'granted')


if __name__ == '__main__':
    unittest.main()
