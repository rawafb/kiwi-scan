import threading
import unittest

from kiwi_scan.datamodels import SubscriptionConfig
from kiwi_scan.scan.sync_controller import (
    AbsoluteTimerSource,
    EventSyncSource,
    SyncController,
)


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class TestSubscriptionSyncTimeout(unittest.TestCase):
    def test_timeout_is_parsed(self):
        sub = SubscriptionConfig.from_dict({
            "name": "position_sync",
            "role": "sync",
            "pv": "TEST:RBV",
            "timeout": "0.02",
        })
        self.assertEqual(sub.timeout, 0.02)

    def test_negative_timeout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timeout must be >= 0"):
            SubscriptionConfig.from_dict({
                "name": "position_sync",
                "role": "sync",
                "pv": "TEST:RBV",
                "timeout": -0.1,
            })


class TestSyncSources(unittest.TestCase):
    def test_event_source_relative_timeout_restarts_on_arm(self):
        source = EventSyncSource("position", timeout=0.5)

        source.arm(10.0)
        self.assertFalse(source.is_ready(10.49))
        self.assertTrue(source.is_ready(10.5))

        source.arm(12.0)
        self.assertFalse(source.is_ready(12.49))
        source.note_event()
        self.assertTrue(source.is_ready(12.49))

    def test_absolute_timer_skips_missed_slots_without_drift(self):
        timer = AbsoluteTimerSource(period=1.0)

        timer.arm(0.0)
        self.assertEqual(timer.next_deadline(), 1.0)

        # Work finished late: 1.0 was missed, but the next slot remains 2.0.
        timer.arm(1.4)
        self.assertEqual(timer.next_deadline(), 2.0)

        # Several slots can be skipped without shifting the phase.
        timer.arm(3.2)
        self.assertEqual(timer.next_deadline(), 4.0)


class TestSyncController(unittest.TestCase):
    def test_external_sync_source_defines_rhythm(self):
        clock = FakeClock(0.0)
        sub = SubscriptionConfig("position", "sync", pv="TEST:RBV")
        controller = SyncController([sub], timer_period=0.1, clock=clock)

        controller.arm()

        # The internal timer does not bypass a configured external source.
        clock.value = 0.2
        self.assertFalse(controller.is_ready())

        controller.note_event("position")
        self.assertTrue(controller.wait())

    def test_relative_timeout_bridges_missing_secondary_source(self):
        clock = FakeClock(0.0)
        primary = SubscriptionConfig("primary", "sync", pv="TEST:PRIMARY")
        secondary = SubscriptionConfig(
            "secondary",
            "sync",
            pv="TEST:SECONDARY",
            timeout=0.2,
        )
        controller = SyncController(
            [primary, secondary],
            timer_period=1.0,
            clock=clock,
        )

        controller.arm()
        clock.value = 0.05
        controller.note_event("primary")

        clock.value = 0.19
        self.assertFalse(controller.is_ready())
        clock.value = 0.2
        self.assertTrue(controller.wait())

    def test_external_rhythm_recovers_after_timeout_cycle(self):
        clock = FakeClock(0.0)
        sub = SubscriptionConfig(
            "position",
            "sync",
            pv="TEST:RBV",
            timeout=0.1,
        )
        controller = SyncController([sub], timer_period=1.0, clock=clock)

        controller.arm()
        clock.value = 0.1
        self.assertTrue(controller.wait())

        # Next cycle is newly armed. A real event can immediately take over
        # again instead of following the previous timeout's phase.
        clock.value = 0.13
        controller.arm()
        clock.value = 0.15
        controller.note_event("position")
        self.assertTrue(controller.wait())

    def test_internal_timer_is_used_without_external_sync_sources(self):
        clock = FakeClock(0.0)
        controller = SyncController([], timer_period=0.1, clock=clock)

        controller.arm()
        clock.value = 0.099
        self.assertFalse(controller.is_ready())
        clock.value = 0.1
        self.assertTrue(controller.wait())

        # 30 ms of scan work does not shift the next timer slot to 0.23.
        clock.value = 0.13
        controller.arm()
        clock.value = 0.199
        self.assertFalse(controller.is_ready())
        clock.value = 0.2
        self.assertTrue(controller.is_ready())

    def test_timed_out_source_does_not_spin_while_other_source_pending(self):
        clock = FakeClock(0.0)
        timed = SubscriptionConfig("timed", "sync", pv="TEST:TIMED", timeout=0.1)
        pending = SubscriptionConfig("pending", "sync", pv="TEST:PENDING")
        controller = SyncController([timed, pending], clock=clock)

        controller.arm()
        clock.value = 0.2  # "timed" is ready through its timeout.

        wait_timeouts = []
        cond_wait = controller._cond.wait

        def recording_wait(timeout=None):
            wait_timeouts.append(timeout)
            if len(wait_timeouts) == 1:
                # Deliver the pending event from another thread while blocked.
                threading.Timer(0.05, controller.note_event, ("pending",)).start()
            return cond_wait(timeout)

        controller._cond.wait = recording_wait

        self.assertTrue(controller.wait())
        # One blocking wait for the event, not a zero-timeout spin.
        self.assertEqual(wait_timeouts, [None])

    def test_set_timer_period_restarts_timer_epoch(self):
        clock = FakeClock(5.0)
        controller = SyncController([], timer_period=1.0, clock=clock)
        controller.arm()

        controller.set_timer_period(0.5)
        clock.value = 6.0
        controller.arm()
        clock.value = 6.49
        self.assertFalse(controller.is_ready())
        clock.value = 6.5
        self.assertTrue(controller.is_ready())

    def test_next_wait_time_ignores_passed_deadlines(self):
        clock = FakeClock(0.0)
        first = SubscriptionConfig("first", "sync", pv="TEST:A", timeout=0.1)
        second = SubscriptionConfig("second", "sync", pv="TEST:B", timeout=0.3)
        controller = SyncController([first, second], clock=clock)

        controller.arm()
        self.assertAlmostEqual(controller._next_wait_time(0.0), 0.1)
        # After the first timeout, wait for the second deadline, not 0.
        self.assertAlmostEqual(controller._next_wait_time(0.15), 0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
