"""Clock metadata must agree with the scheduler under different host zones."""
import json
import os
import subprocess
import sys
import unittest


class SchedulerClockTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(__import__('time'), 'tzset'), '需要 POSIX 本地时区支持')
    def test_clock_and_execution_use_same_system_zone(self):
        script = '''
import json, time
from datetime import date, datetime, timezone
from scheduler import scheduler_clock, _local_epoch
time.tzset()
value = scheduler_clock()
value['scheduled_utc_hour'] = datetime.fromtimestamp(_local_epoch(date(2030, 1, 2), 9, 0), timezone.utc).hour
print(json.dumps(value))
'''
        for zone, offset, utc_hour in [('UTC0', '+00:00', 9), ('EST5', '-05:00', 14), ('CST-8', '+08:00', 1)]:
            with self.subTest(zone=zone):
                result = subprocess.run([sys.executable, '-c', script], env={**os.environ, 'TZ': zone},
                                        capture_output=True, text=True, check=True, timeout=10)
                clock = json.loads(result.stdout)
                self.assertEqual(clock['timezone'], 'system-local')
                self.assertTrue(clock['current_time'].endswith(offset))
                self.assertIn(offset, clock['timezone_label'])
                self.assertEqual(clock['scheduled_utc_hour'], utc_hour)


if __name__ == '__main__':
    unittest.main()
