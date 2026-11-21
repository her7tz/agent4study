"""Run the scheduler against disposable SQLite storage, without network or launchd."""
import concurrent.futures
import io
import logging
import os
import queue
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness import JobStore
from scheduler import Scheduler, SchedulerLease


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


class Occurrences(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = JobStore(Path(folder.name)/'jobs.db')
        self.runner = Mock(return_value={'title':'合成周报', 'path':'fixture.md'})
        self.scheduler = Scheduler(self.store, SimpleNamespace(data_home=Path(folder.name)),
                                   self.runner, Mock(), executor=InlineExecutor())
        self.note = patch('scheduler.notify_local').start()
        self.addCleanup(patch.stopall)

    def add(self, cadence='once'):
        self.store.add('weekly-report','',cadence,'09:00' if cadence=='daily' else None,
                       100,capability='study_report',parameters={'period':'last_week'})

    def test_legacy_crash_after_dispatch_does_not_replay_occurrence(self):
        self.add()
        self.store.record_run('weekly-report',100,'running','旧版已派发，尚未推进计划')
        self.scheduler.recover_pending()
        self.scheduler.tick(now=105)
        self.runner.assert_not_called()
        self.assertEqual(self.store.recent_runs()[0]['status'],'interrupted')
        self.assertFalse(self.store.get_jobs()[0]['enabled'])
        self.note.assert_not_called()

    def test_disabled_daily_job_cannot_be_reenabled_by_stale_poll(self):
        self.add('daily')
        stale=self.store.get_due_jobs(105)
        self.store.remove('weekly-report')
        with patch.object(self.store,'get_due_jobs',return_value=stale):
            self.scheduler.tick(now=105)
        self.runner.assert_not_called()
        self.assertFalse(self.store.get_jobs()[0]['enabled'])
        self.assertEqual(self.store.recent_runs(),[])

    def test_two_polls_of_same_snapshot_dispatch_once_with_registered_parameters(self):
        self.add()
        stale=self.store.get_due_jobs(105)
        with patch.object(self.store,'get_due_jobs',return_value=stale):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(self.scheduler.tick,[105,105]))
        self.runner.assert_called_once_with('study_report',{'period':'last_week'})
        self.assertEqual([r['status'] for r in self.store.recent_runs()],['completed'])

    def test_old_daily_records_do_not_expire_an_occurrence_due_at_poll_start(self):
        due = time.mktime((2026, 11, 13, 9, 0, 0, -1, -1, -1))
        first_due = time.mktime((2026, 11, 10, 9, 0, 0, -1, -1, -1))
        self.store.add('daily', '', 'daily', '09:00', first_due,
                       capability='study_report', parameters={'period': 'last_week'})
        self.store.add('once', '', 'once', None, due,
                       capability='study_report', parameters={'period': 'this_week'})
        clock = [due + 58]
        journal_order = []
        claim_due = self.store.claim_due

        def claim(job_id, scheduled_at, following, missed, detail):
            journal_order.append((job_id, scheduled_at, missed))
            if scheduled_at < due:
                clock[0] += 5
            return claim_due(job_id, scheduled_at, following, missed, detail)

        with patch('scheduler.time.time', side_effect=lambda: clock[0]), \
             patch.object(self.store, 'claim_due', side_effect=claim):
            self.scheduler.tick()

        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(journal_order[0], ('once', due, False))
        runs = self.store.recent_runs()
        self.assertEqual({run['job_id']: run['status'] for run in runs if run['scheduled_at'] == due},
                         {'daily': 'completed', 'once': 'completed'})
        self.assertEqual(sum(run['status'] == 'missed' for run in runs), 3)
        self.assertTrue(next(job for job in self.store.get_jobs() if job['id'] == 'daily')['enabled'])

    def test_waiting_material_can_finish_without_changing_run_identity(self):
        self.add()
        self.store.record_run('weekly-report',100,'waiting_user','等待上传')
        before=self.store.recent_runs()[0]
        self.store.record_run('weekly-report',100,'completed','补充材料已完成','note.md')
        after=self.store.recent_runs()[0]
        self.assertEqual((after['id'],after['status'],after['output_path']),(before['id'],'completed','note.md'))
        self.store.record_run('weekly-report',100,'running','晚到的进度')
        self.assertEqual(self.store.recent_runs()[0]['status'],'completed')

    def test_failed_journal_insert_rolls_back_plan_advance(self):
        import sqlite3
        self.add()
        with self.store._connect() as db:
            db.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON job_runs BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.scheduler.tick(now=105)
        self.assertTrue(self.store.get_jobs()[0]['enabled'])
        self.assertEqual(self.store.get_jobs()[0]['due_at'],100)
        self.assertEqual(self.store.recent_runs(),[])
        self.runner.assert_not_called()

    def test_partial_poll_notifies_only_committed_misses_before_retry(self):
        first = time.mktime((2026, 11, 12, 9, 0, 0, -1, -1, -1))
        second = time.mktime((2026, 11, 13, 9, 0, 0, -1, -1, -1))
        self.store.add('single', '', 'once', None, first - 60, capability='study_report',
                       parameters={'period': 'this_week'})
        self.store.add('daily', '', 'daily', '09:00', first, capability='study_report',
                       parameters={'period': 'last_week'})
        notices = queue.Queue()
        self.note.side_effect = lambda title, message, _logger: notices.put((title, message))
        claim_due = self.store.claim_due
        connect = self.store._connect

        def short_connection():
            db = connect()
            db.execute('PRAGMA busy_timeout=20')
            return db

        def claim_with_conflict(job_id, due, *args):
            if job_id != 'daily' or due != second:
                return claim_due(job_id, due, *args)
            with connect() as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                return claim_due(job_id, due, *args)

        current = second + 90
        with patch.object(self.store, '_connect', side_effect=short_connection), \
             patch.object(self.store, 'claim_due', side_effect=claim_with_conflict):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'database is locked'):
                self.scheduler.tick(now=current)

        committed = self.store.recent_runs()
        self.assertEqual({(run['job_id'], run['scheduled_at'], run['status']) for run in committed},
                         {('single', first - 60, 'missed'), ('daily', first, 'missed')})
        self.assertTrue(all('超过计划执行的宽限' in run['detail'] for run in committed))
        self.assertEqual(notices.get(timeout=2), ('定时任务错过', '2 个计划共有 2 次执行已跳过，请查看运行记录。'))
        next_daily = next(job for job in self.store.get_jobs() if job['id'] == 'daily')
        self.assertEqual(next_daily['due_at'], second)

        self.scheduler.tick(now=current)
        self.assertEqual(notices.get(timeout=2), ('定时任务错过', '任务 daily 有 1 次执行已跳过。'))
        self.assertEqual(len(self.store.recent_runs()), 3)
        self.assertEqual(self.note.call_count, 2)
        self.runner.assert_not_called()

    def test_standalone_scheduler_keeps_polling_after_a_temporary_write_conflict(self):
        import sqlite3

        due = time.time() - 2
        self.store.add('standalone', '', 'once', None, due,
                       capability='study_report', parameters={'period': 'this_week'})
        claim = self.store.claim_due
        conflicted = threading.Event()

        def claim_after_retry(*args):
            if not conflicted.is_set():
                conflicted.set()
                raise sqlite3.OperationalError('database is locked')
            return claim(*args)

        with patch('scheduler.POLL_SECONDS', .02), patch.object(self.store, 'claim_due', side_effect=claim_after_retry), \
             concurrent.futures.ThreadPoolExecutor(1) as worker:
            service = worker.submit(self.scheduler.run_forever)
            try:
                self.assertTrue(conflicted.wait(2))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    runs = self.store.recent_runs()
                    if runs and runs[0]['status'] == 'completed':
                        break
                    time.sleep(.02)
                self.assertEqual([(run['status'], run['scheduled_at']) for run in runs], [('completed', due)])
                self.assertFalse(service.done())
            finally:
                self.scheduler.stopping.set()
                service.result(timeout=3)
        self.runner.assert_called_once_with('study_report', {'period': 'this_week'})
        lease = SchedulerLease(self.scheduler.settings.data_home/'scheduler.lock')
        try:
            self.assertTrue(lease.acquire())
        finally:
            lease.release()

    def test_cancelled_queued_occurrence_cannot_begin_and_daily_plan_survives(self):
        self.add('daily')
        occurrence, = self.store.get_jobs()
        self.assertEqual(self.store.claim_due('weekly-report', 100, 86500, False), 'new')
        run, = self.store.recent_runs()
        external = JobStore(self.store.database_path)
        self.assertEqual(external.cancel_run(run['id'])['status'], 'cancelled')
        self.scheduler._dispatch(occurrence, 100)
        self.runner.assert_not_called()
        self.note.assert_not_called()
        self.scheduler.recover_pending()
        self.assertEqual(external.cancel_run(run['id'])['status'], 'cancelled')
        job, = self.store.get_jobs()
        self.assertEqual((job['due_at'], job['enabled']), (86500, 1))

    def test_terminal_record_retries_sqlite_lock_without_rerunning_work(self):
        self.add()
        due = 100
        writes = self.store.record_run
        connect = self.store._connect
        blocked_statuses = []
        locked = threading.Event()
        release_lock = threading.Event()
        lock_released = threading.Event()

        def hold_write_lock():
            with connect() as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                locked.set()
                release_lock.wait(3)
            lock_released.set()

        def short_connection():
            connection = connect()
            connection.execute('PRAGMA busy_timeout=20')
            return connection

        def run_once(*_args):
            holder.start()
            self.assertTrue(locked.wait(2))
            return {'title': '合成周报', 'path': 'fixture.md'}

        def observe_write(*args, **kwargs):
            try:
                return writes(*args, **kwargs)
            except sqlite3.OperationalError:
                blocked_statuses.append(args[2])
                if len(blocked_statuses) == 2:
                    release_lock.set()
                    self.assertTrue(lock_released.wait(2))
                raise

        holder = threading.Thread(target=hold_write_lock)
        self.runner.side_effect = run_once
        try:
            with patch.object(self.store, '_connect', side_effect=short_connection), \
                 patch.object(self.store, 'record_run', side_effect=observe_write), \
                 patch('scheduler.CANCELLATION_POLL_SECONDS', .02):
                self.scheduler.tick(now=due + 1)
        finally:
            release_lock.set()
            if holder.ident is not None:
                holder.join(timeout=3)

        self.assertEqual(blocked_statuses, ['completed', 'completed'])
        run, = self.store.recent_runs()
        self.assertEqual((run['status'], run['output_path']), ('completed', 'fixture.md'))
        self.runner.assert_called_once_with('study_report', {'period': 'last_week'})
        self.note.assert_called_once()
        self.assertEqual(self.note.call_args.args[0], '定时任务已完成')

    def test_cancelled_occurrence_keeps_its_status_during_record_retry(self):
        self.add()
        write = self.store.record_run
        external = JobStore(self.store.database_path)

        def cancel_before_retry(*args, **kwargs):
            if args[2] == 'completed':
                run, = external.recent_runs()
                if run['status'] == 'running':
                    self.assertEqual(external.cancel_run(run['id'])['status'], 'cancelled')
                    raise sqlite3.OperationalError('database is locked')
            return write(*args, **kwargs)

        with patch.object(self.store, 'record_run', side_effect=cancel_before_retry), \
             patch('scheduler.CANCELLATION_POLL_SECONDS', .02):
            self.scheduler.tick(now=105)

        run, = self.store.recent_runs()
        self.assertEqual((run['status'], run['output_path']), ('cancelled', None))
        self.runner.assert_called_once()
        self.note.assert_not_called()


class ScheduledReportIntegration(unittest.TestCase):
    def test_workbench_retries_initial_recovery_after_a_real_sqlite_write_lock(self):
        from harness import Settings
        from web_server import WebRuntime

        class ShortBusyTimeout(JobStore):
            def _connect(self):
                db = super()._connect()
                db.execute('PRAGMA busy_timeout=20')
                return db

        locked, release, attempted = threading.Event(), threading.Event(), threading.Event()
        recover = Scheduler.recover_pending

        def delayed_recovery(scheduler):
            if not attempted.is_set():
                with JobStore._connect(scheduler.store) as blocker:
                    blocker.execute('BEGIN IMMEDIATE')
                    locked.set()
                    try:
                        recover(scheduler)
                    finally:
                        attempted.set()
                        release.wait(3)
            else:
                recover(scheduler)

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'MINIMAL_AGENT_HOME': folder, 'MINIMAL_AGENT_OUTPUT': str(Path(folder)/'library'), 'DEEPSEEK_API_KEY': '',
        }), patch('web_server.JobStore', ShortBusyTimeout), patch('web_server.POLL_SECONDS', .02), \
                patch.object(Scheduler, 'recover_pending', delayed_recovery), patch('scheduler.notify_local'), \
                patch('web_server.execute_capability', return_value={'title': '合成周报'}) as execute:
            settings = Settings()
            store = JobStore(settings.database_path)
            due = time.time() - 2
            store.add('old-run', '', 'once', None, due, capability='study_report', parameters={'period': 'last_week'})
            self.assertEqual(store.claim_due('old-run', due, None, False), 'new')
            store.add('expired', '', 'once', None, due - 65, capability='study_report', parameters={'period': 'last_week'})
            store.add('fresh', '', 'once', None, due, capability='study_report', parameters={'period': 'this_week'})
            runtime = WebRuntime(settings, Mock())
            try:
                self.assertTrue(locked.wait(2))
                self.assertTrue(attempted.wait(2))
                self.assertEqual(runtime.status()['scheduler_owner'], 'web')
                self.assertFalse(runtime.status()['scheduler_running'])
                self.assertEqual(store.recent_runs()[0]['status'], 'queued')
                release.set()
                deadline = time.monotonic() + 3
                expected = {'old-run': 'interrupted', 'expired': 'missed', 'fresh': 'completed'}
                while time.monotonic() < deadline:
                    statuses = {run['job_id']: run['status'] for run in store.recent_runs()}
                    if statuses == expected:
                        break
                    time.sleep(.02)
                self.assertEqual(statuses, expected)
                self.assertTrue(runtime.status()['scheduler_running'])
                self.assertTrue(all(not job['enabled'] for job in store.get_jobs()))
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[:2], ('study_report', {'period': 'this_week'}))
            finally:
                release.set()
                runtime.close()

    def test_workbench_scheduled_paper_notifies_once_after_waiting_is_persisted(self):
        from harness import Settings
        from paper_summary import MaterialRequired
        from web_server import WebRuntime

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MINIMAL_AGENT_HOME': folder, 'MINIMAL_AGENT_OUTPUT': str(Path(folder) / 'notes'),
                'DEEPSEEK_API_KEY': 'synthetic-test'}):
            settings = Settings()
            store = JobStore(settings.database_path)
            url = 'https://ieeexplore.ieee.org/document/11029963'
            store.add('scheduled-paper', url, 'once', None, time.time() - 1,
                      capability='paper_summary', parameters={'url': url})
            notices = []
            finished = threading.Event()

            def notify(title, message, logger):
                run, = store.recent_runs()
                with store._connect() as db:
                    task = db.execute('SELECT status,session_id FROM ui_tasks').fetchone()
                notices.append((title, run['status'], task['status'], bool(task['session_id'])))
                if run['status'] == 'waiting_user':
                    finished.set()

            with patch('web_server.execute_capability', side_effect=MaterialRequired('请上传论文 PDF。')), \
                 patch('site_sessions._addresses', return_value=['93.184.216.34']), \
                 patch('web_server.notify_local', side_effect=notify), \
                 patch('scheduler.notify_local', side_effect=notify):
                runtime = WebRuntime(settings, logging.getLogger('scheduled-paper-notice'))
                try:
                    self.assertTrue(finished.wait(5), notices)
                finally:
                    runtime.close()
                    runtime.executor.join(timeout=3)
                self.assertEqual(notices, [('需要论文材料', 'waiting_user', 'waiting_user', True)])

    def test_independent_service_hides_execution_credentials_in_diagnostics(self):
        from agent import main
        from agent_profiles import AgentProfiles
        from harness import Settings

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MINIMAL_AGENT_HOME': folder, 'MINIMAL_AGENT_OUTPUT': str(Path(folder) / 'notes'),
                'DEEPSEEK_API_KEY': 'fixture-common-prefix'}):
            credential = 'fixture-common-prefix-video-key'
            profiles = AgentProfiles(Settings())
            profiles.update('video_learning', {'api_key': credential})
            output = io.StringIO()
            logger = logging.Logger('isolated-service-diagnostics')
            logger.addHandler(logging.StreamHandler(output))
            observed = {}

            def execute(capability, parameters, context):
                context.logger.info('连接阶段：%s', context.settings.api_key)
                context.progress('准备模型 ' + context.settings.api_key)
                observed['stage'] = store.recent_runs()[0]['detail']
                profiles.update('video_learning', {'api_key': 'rotated-test-key'})
                raise RuntimeError('模型握手失败：' + context.settings.api_key)

            def run_service(scheduler):
                try:
                    scheduler.executor.shutdown(wait=True)
                    scheduler.executor = InlineExecutor()
                    scheduler.owns_executor = False
                    scheduler.tick(now=101)
                finally:
                    scheduler.close()

            store = JobStore(Path(folder) / 'harness.sqlite3')
            store.add('diagnostics', 'https://youtu.be/fixture', 'once', None, 100)
            with patch('agent.configure_logging', return_value=logger), \
                 patch.object(Scheduler, 'run_forever', autospec=True, side_effect=run_service), \
                 patch('subagents.run_business_subagent', side_effect=execute), \
                 patch('scheduler.notify_local') as notification:
                self.assertEqual(main(['service', 'run']), 0)
            run, = store.recent_runs()
            self.assertEqual(run['status'], 'failed')
            self.assertEqual(notification.call_args.args[0], '定时任务执行失败')
            for text in (observed['stage'], run['detail'], notification.call_args.args[1], output.getvalue()):
                self.assertNotIn('fixture-common-prefix', text)
                self.assertNotIn('-video-key', text)
                self.assertIn('[已隐藏]', text)
            self.assertEqual(run['detail'], '失败于：准备模型 [已隐藏]\n模型握手失败：[已隐藏]')
            self.assertIn('Traceback', output.getvalue())

    def test_independent_service_reloads_defaults_for_each_start(self):
        from agent import main
        from harness import Settings
        from web_server import WebRuntime

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MINIMAL_AGENT_HOME': folder, 'MINIMAL_AGENT_OUTPUT': str(Path(folder) / 'notes'),
                'MINIMAL_AGENT_MAX_PARALLEL': '2'}):
            os.environ.pop('DEEPSEEK_API_KEY', None)
            os.environ.pop('DEEPSEEK_MODEL', None)
            (Path(folder) / '.env').write_text('DEEPSEEK_API_KEY=first-fixture\nDEEPSEEK_MODEL=first-model\n')
            first_started, second_started, release = (threading.Event() for _ in range(3))
            finished = threading.Semaphore(0)
            observed = {}

            def execute(capability, parameters, context):
                key = parameters['url'].rsplit('/', 1)[-1]
                observed[key] = (context.settings.model, context.settings.api_key)
                if key == 'first':
                    first_started.set()
                    if not release.wait(5):
                        raise RuntimeError('测试未释放首个任务。')
                    observed['first_after_save'] = (context.settings.model, context.settings.api_key)
                else:
                    second_started.set()
                return {'title': '隔离定时测试', 'markdown': '完成。'}

            def run_service(scheduler):
                lease = SchedulerLease(scheduler.settings.data_home / 'scheduler.lock')
                self.assertTrue(lease.acquire())
                web = None
                try:
                    scheduler.store.add('first', 'https://youtu.be/first', 'once', None, 100)
                    scheduler.store.add('second', 'https://youtu.be/second', 'once', None, 110)
                    scheduler.tick(now=101)
                    self.assertTrue(first_started.wait(3))
                    web = WebRuntime(Settings(), logging.getLogger('schedule-config-web'))
                    self.assertFalse(web.scheduler_owned)
                    web.configure({'model': 'second-model', 'api_key': 'second-fixture'})
                    scheduler.tick(now=111)
                    self.assertTrue(second_started.wait(3))
                    release.set()
                    self.assertTrue(finished.acquire(timeout=3))
                    self.assertTrue(finished.acquire(timeout=3))
                    self.assertEqual({row['status'] for row in scheduler.store.recent_runs()}, {'completed'})
                finally:
                    release.set()
                    scheduler.close()
                    scheduler.join(timeout=3)
                    if web:
                        web.close()
                    lease.release()

            with patch('agent.configure_logging', return_value=logging.getLogger('schedule-config-cli')), \
                 patch.object(Scheduler, 'run_forever', autospec=True, side_effect=run_service), \
                 patch('subagents.run_business_subagent', side_effect=execute), \
                 patch('scheduler.notify_local', side_effect=lambda *_: finished.release()):
                self.assertEqual(main(['service', 'run']), 0)
            self.assertEqual(observed, {'first': ('first-model', 'first-fixture'),
                                        'first_after_save': ('first-model', 'first-fixture'),
                                        'second': ('second-model', 'second-fixture')})

    def test_open_workbench_takes_over_after_independent_lease_holder_exits(self):
        from harness import Settings
        from scheduler import RUN_GRACE_SECONDS
        from web_server import WebRuntime

        owner_script = """
import sys
from pathlib import Path
from scheduler import SchedulerLease
directory = Path(sys.argv[1])
lease = SchedulerLease(directory / 'scheduler.lock')
assert lease.acquire()
(directory / 'owner-ready').touch()
sys.stdin.read()
"""
        logger = Mock(spec=logging.Logger)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MINIMAL_AGENT_HOME': folder, 'MINIMAL_AGENT_OUTPUT': str(Path(folder) / 'notes'),
                'DEEPSEEK_API_KEY': '', 'MINIMAL_AGENT_MAX_PARALLEL': '2'}), \
                patch('web_server.POLL_SECONDS', .02), \
                patch('web_server.execute_capability', return_value={'title': '合成周报', 'markdown': '完成。'}) as execute, \
                patch('scheduler.notify_local'):
            with subprocess.Popen([sys.executable, '-c', owner_script, folder], stdin=subprocess.PIPE,
                                  cwd=Path(__file__).resolve().parents[1]) as owner:
                try:
                    ready = Path(folder) / 'owner-ready'
                    deadline = time.monotonic() + 5
                    while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists(), '独立进程未取得调度锁。')
                    runtime = WebRuntime(Settings(), logger)
                    try:
                        self.assertEqual(runtime.status()['scheduler_owner'], 'external')
                        now = time.time()
                        runtime.store.add('expired', '', 'once', None, now - RUN_GRACE_SECONDS - 10,
                                          capability='study_report', parameters={'period': 'last_week'})
                        runtime.store.add('fresh', '', 'once', None, now - 1,
                                          capability='study_report', parameters={'period': 'this_week'})
                        runtime.store.add('stranded', '', 'once', None, now - 2,
                                          capability='study_report', parameters={'period': 'last_week'})
                        self.assertEqual(runtime.store.claim_due('stranded', now - 2, None, False), 'new')
                        runtime.store.record_run('stranded', now - 2, 'running', '独立进程退出前已开始')
                        execute.assert_not_called()

                        owner.kill()
                        owner.wait(timeout=5)
                        expected = {'expired': 'missed', 'fresh': 'completed', 'stranded': 'interrupted'}
                        statuses = {}
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            statuses = {run['job_id']: run['status'] for run in runtime.store.recent_runs()}
                            if statuses == expected:
                                break
                            time.sleep(.02)
                        self.assertEqual(statuses, expected)
                        self.assertEqual(runtime.status()['scheduler_owner'], 'web')
                        execute.assert_called_once()
                        self.assertEqual(execute.call_args.args[:2], ('study_report', {'period': 'this_week'}))
                        self.assertFalse(any(job['enabled'] for job in runtime.store.get_jobs()))
                    finally:
                        runtime.close()
                    contender = SchedulerLease(Path(folder) / 'scheduler.lock')
                    try:
                        self.assertTrue(contender.acquire(), '关闭工作台后仍未释放调度锁。')
                    finally:
                        contender.release()
                finally:
                    if owner.poll() is None:
                        owner.kill()
                        owner.wait(timeout=5)

    def test_cli_model_update_preserves_other_settings_in_private_file(self):
        from agent import _write_secret

        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / '.env'
            env_file.write_text('DEEPSEEK_API_KEY=fixture\nDEEPSEEK_MODEL=old-model\n')
            _write_secret(SimpleNamespace(data_home=Path(folder), prepare=lambda: None),
                          'DEEPSEEK_MODEL', 'new-model')
            self.assertEqual(env_file.read_text(), 'DEEPSEEK_API_KEY=fixture\nDEEPSEEK_MODEL=new-model\n')
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

    def test_workbench_schedule_uses_same_redaction_in_task_and_occurrence(self):
        from harness import Settings
        from web_server import WebRuntime

        credential = 'workbench-synthetic-secret'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'MINIMAL_AGENT_HOME': folder, 'MINIMAL_AGENT_OUTPUT': str(Path(folder) / 'notes'),
                'DEEPSEEK_API_KEY': credential}):
            settings = Settings()
            store = JobStore(settings.database_path)
            store.add('web-diagnostics', 'https://youtu.be/fixture', 'once', None, time.time() - 1)
            finished = threading.Event()
            observed = {}
            output = io.StringIO()
            logger = logging.Logger('scheduled-workbench-diagnostics')
            logger.addHandler(logging.StreamHandler(output))

            def execute(capability, parameters, settings, *, progress, **kwargs):
                progress('连接模型 ' + credential)
                with store._connect() as db:
                    observed['task_stage'] = db.execute('SELECT stage FROM ui_tasks').fetchone()[0]
                observed['run_stage'] = store.recent_runs()[0]['detail']
                raise RuntimeError('模型握手失败：' + credential)

            with patch('web_server.execute_capability', side_effect=execute), \
                 patch('scheduler.notify_local', side_effect=lambda *_: finished.set()) as notification:
                runtime = WebRuntime(settings, logger)
                try:
                    self.assertTrue(finished.wait(5))
                    task, = runtime.tasks.list()
                    run, = store.recent_runs()
                    self.assertEqual((task['status'], run['status']), ('failed', 'failed'))
                    self.assertEqual(observed, {'task_stage': '连接模型 [已隐藏]',
                                                'run_stage': '连接模型 [已隐藏]'})
                    self.assertEqual(task['stage'], '失败于：连接模型 [已隐藏]')
                    self.assertEqual(task['error'], '模型握手失败：[已隐藏]')
                    self.assertEqual(run['detail'], task['stage'] + '\n' + task['error'])
                    self.assertEqual(notification.call_args.args[1], run['detail'])
                    self.assertIn('Traceback', output.getvalue())
                    self.assertNotIn(credential, output.getvalue())
                finally:
                    runtime.close()
                    runtime.executor.join(timeout=3)

    def test_app_scheduler_executes_registered_report_and_saves_one_file(self):
        from harness import Settings
        from tests.test_study_report import StudyFixture
        from web_server import WebRuntime
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{
                'MINIMAL_AGENT_HOME':folder,'MINIMAL_AGENT_OUTPUT':str(Path(folder)/'notes'),
                'DEEPSEEK_API_KEY':'synthetic-test'}):
            settings=Settings()
            store=JobStore(settings.database_path)
            due=time.time()-1
            store.add('due-report','','once',None,due,capability='study_report',parameters={'period':'last_week'})
            done=threading.Event()
            with patch('study_report.StudyLog',return_value=StudyFixture(0)), \
                    patch('scheduler.notify_local',side_effect=lambda *_:done.set()), \
                    patch('harness.DeepSeekClient.chat',side_effect=AssertionError('空周期不应调用模型')):
                app=WebRuntime(settings,logging.getLogger('scheduled-integration'))
                try:
                    self.assertTrue(done.wait(5),'到期任务未在期限内结束')
                    run,=store.recent_runs()
                    self.assertEqual(run['status'],'completed',run['detail'])
                    task,=app.tasks.list()
                    self.assertEqual((task['capability'],task['schedule_job_id'],task['scheduled_at']),
                                     ('study_report','due-report',due))
                    report=settings.output_dir/task['output_file']
                    self.assertEqual(report.resolve(),Path(run['output_path']).resolve())
                    self.assertEqual(report.parent.name,'study_report')
                    self.assertIn('没有学习或实验记录',report.read_text())
                    self.assertFalse(store.get_jobs()[0]['enabled'])
                finally:
                    app.close()
                    app.executor.join(timeout=3)
