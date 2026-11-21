"""Exercise real local-time conversion in isolated processes and disposable storage."""
import os
import subprocess
import sys
import unittest


PRELUDE = '''
import tempfile, time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from harness import JobStore
from scheduler import Scheduler, _local_epoch, _next_daily_after, add_capability_once_job
from tests.test_schedule_occurrences import InlineExecutor
time.tzset()
folder = tempfile.TemporaryDirectory()
store = JobStore(Path(folder.name) / 'jobs.db')
runner = Mock(return_value={'path':'fixture.md'})
scheduler = Scheduler(store, SimpleNamespace(data_home=Path(folder.name)), runner, Mock(), executor=InlineExecutor())
def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
'''


@unittest.skipUnless(hasattr(__import__('time'), 'tzset'), '需要 POSIX 本地时区支持')
class DaylightSavingTests(unittest.TestCase):
    def run_scenario(self, script):
        result = subprocess.run([sys.executable, '-c', PRELUDE + script],
            env={**os.environ, 'TZ': 'America/New_York'}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nonexistent_once_time_is_rejected_and_daily_is_skipped(self):
        self.run_scenario('''
with patch('scheduler.time.time', return_value=utc('2027-03-01T12:00:00')):
    try:
        add_capability_once_job(store, 'study_report', {'period':'last_week'}, '2027-03-14 02:30')
    except ValueError as exc:
        assert '不存在' in str(exc)
    else:
        raise AssertionError('nonexistent local time silently accepted')
due = _local_epoch(date(2027,3,13),2,30)
store.add('daily','','daily','02:30',due,capability='study_report',parameters={'period':'last_week'})
with patch('scheduler.notify_local') as notify:
    scheduler.tick(now=due)
    gap = store.get_jobs()[0]['due_at']
    scheduler.tick(now=gap)
    assert runner.call_count == 1, 'nonexistent daily time executed'
    missed = [run for run in store.recent_runs() if run['status']=='missed']
    assert len(missed)==1 and '不存在' in missed[0]['detail']
    next_due = store.get_jobs()[0]['due_at']
    assert time.strftime('%Y-%m-%d %H:%M',time.localtime(next_due))=='2027-03-15 02:30'
    scheduler.tick(now=next_due)
    assert runner.call_count == 2
''')

    def test_repeated_hour_runs_once_and_wake_skips_past_days(self):
        self.run_scenario('''
first = _local_epoch(date(2027,11,7),1,30)
assert first == utc('2027-11-07T05:30:00'), 'must consistently choose first occurrence'
store.add('daily','','daily','01:30',first,capability='study_report',parameters={'period':'last_week'})
with patch('scheduler.notify_local'):
    scheduler.tick(now=first)
    scheduler.tick(now=first+3600)
    assert runner.call_count==1, 'repeated local hour executed twice'
    scheduler.tick(now=utc('2027-11-10T12:00:00'))
    assert runner.call_count==1, 'wake replayed missed runs'
    assert len([run for run in store.recent_runs() if run['status']=='missed'])==3
    assert time.strftime('%Y-%m-%d %H:%M',time.localtime(store.get_jobs()[0]['due_at']))=='2027-11-11 01:30'
''')
