import datetime as dt
import unittest

from hylms.automation_context import occurrence


class OccurrenceTests(unittest.TestCase):
    def test_kst_slots_are_stable_for_delayed_and_duplicate_dispatch(self):
        for utc, expected in (
            ('2026-09-30T23:59:00+00:00', '20260930T1900'),
            ('2026-10-01T01:00:00+00:00', '20261001T1000'),
            ('2026-10-01T09:59:00+00:00', '20261001T1000'),
            ('2026-10-01T10:00:00+00:00', '20261001T1900'),
            ('2026-10-01T17:00:00+00:00', '20261001T1900'),
        ):
            self.assertEqual(occurrence(dt.datetime.fromisoformat(utc)), expected)

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            occurrence(dt.datetime(2026, 10, 1))
