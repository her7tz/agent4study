"""Offline regressions for shared scheduling and video fallback behavior."""

import tempfile
import logging
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness import JobStore, Settings, notify_local
from paper_summary import MaterialRequired
from scheduler import Scheduler, SchedulerLease
from video_summary import _get_video_material


class LocalNotificationTests(unittest.TestCase):
    def test_logs_nonzero_status_without_logging_a_successful_display(self):
        logger = Mock()
        with patch('harness.platform.system', return_value='Darwin'), \
             patch('harness.shutil.which', return_value='/usr/bin/osascript'), \
             patch('harness.subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
            notify_local('需要论文材料', '请上传论文 PDF。', logger)
        logger.info.assert_called_once_with('尝试本地通知：%s — %s', '需要论文材料', '请上传论文 PDF。')
        logger.warning.assert_called_once_with('本地通知命令未成功（退出码 %s）：%s', 1, '需要论文材料')


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = JobStore(self.root / "tasks.sqlite3")
        self.settings = Settings()
        self.settings.data_home = self.root
        self.settings.output_dir = self.root / "summaries"
        self.runner = Mock(return_value={"title": "结果", "path": "summary.md"})
        self.scheduler = Scheduler(self.store, self.settings, self.runner, Mock())

    def tearDown(self):
        self.scheduler.close()
        self.scheduler.join(timeout=2)
        self.folder.cleanup()

    def test_only_one_scheduler_can_hold_the_shared_lease(self):
        first = SchedulerLease(self.root / "scheduler.lock")
        second = SchedulerLease(self.root / "scheduler.lock")
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
        finally:
            first.release()
            second.release()

    @patch("scheduler.notify_local")
    def test_missed_one_time_job_is_recorded_and_never_executed(self, notify):
        attempted = threading.Event()
        notify.side_effect = lambda *args: attempted.set()
        self.store.add("missed", "https://youtu.be/example", "once", None, 100)
        self.scheduler.tick(now=200)
        self.runner.assert_not_called()
        self.assertEqual(self.store.recent_runs()[0]["status"], "missed")
        self.assertFalse(self.store.get_jobs()[0]["enabled"])
        self.assertTrue(attempted.wait(timeout=3), "错过的任务应尝试通知")
        notify.assert_called_once()

    @patch("scheduler.notify_local")
    def test_one_time_job_runs_only_once(self, notify):
        completed = threading.Event()
        notify.side_effect = lambda *args: completed.set()
        self.store.add("due", "https://youtu.be/example", "once", None, 100)
        self.scheduler.tick(now=105)
        self.scheduler.tick(now=110)
        self.assertTrue(completed.wait(timeout=3), "后台任务应在超时前完成")
        self.runner.assert_called_once()
        self.assertEqual(self.store.recent_runs()[0]["status"], "completed")

    @patch("scheduler.notify_local")
    def test_missed_daily_job_keeps_its_next_occurrence(self, notify):
        now = time.mktime((2026, 10, 13, 10, 0, 0, -1, -1, -1))
        due = time.mktime((2026, 10, 12, 9, 0, 0, -1, -1, -1))
        self.store.add("daily", "https://youtu.be/example", "daily", "09:00", due)
        self.scheduler.tick(now=now)
        self.runner.assert_not_called()
        job = self.store.get_jobs()[0]
        self.assertTrue(job["enabled"])
        self.assertGreater(job["due_at"], now)
        self.assertTrue(all(run["status"] == "missed" for run in self.store.recent_runs()))

    @patch("scheduler.notify_local")
    def test_slow_missed_notice_does_not_delay_this_tick_or_next(self, notify):
        notice_started, release_notice, tick_finished, completed = (threading.Event() for _ in range(4))

        def blocking_notice(title, *_):
            if title == "定时任务错过":
                notice_started.set()
                release_notice.wait(timeout=5)
            else:
                completed.set()

        def first_tick():
            self.scheduler.tick(now=200)
            tick_finished.set()

        notify.side_effect = blocking_notice
        self.store.add("missed", "https://youtu.be/example", "once", None, 100)
        self.store.add("also-missed", "https://youtu.be/example", "once", None, 101)
        self.store.add("on-time", "https://youtu.be/example", "once", None, 149)
        poll = threading.Thread(target=first_tick)
        poll.start()
        try:
            self.assertTrue(notice_started.wait(timeout=3))
            self.assertTrue(tick_finished.wait(timeout=1), "系统通知不能阻塞调度轮询")
            self.assertTrue(completed.wait(timeout=3))
            completed.clear()
            self.store.add("next-tick", "https://youtu.be/example", "once", None, 201)
            self.scheduler.tick(now=202)
            self.assertTrue(completed.wait(timeout=3))
            self.assertEqual({run["job_id"]: run["status"] for run in self.store.recent_runs()},
                             {"missed": "missed", "also-missed": "missed", "on-time": "completed", "next-tick": "completed"})
            missed_calls = [call for call in notify.call_args_list if call.args[0] == "定时任务错过"]
            self.assertEqual(len(missed_calls), 1)
            self.assertIn("2 个计划共有 2 次", missed_calls[0].args[1])
        finally:
            release_notice.set()
            poll.join(timeout=3)

    def test_waiting_material_survives_denied_notification(self):
        self.runner.side_effect = MaterialRequired('需要上传可读取的论文 PDF。')
        self.store.add('paper-wait', '', 'once', None, 100, capability='paper_summary',
                       parameters={'url': 'https://example.org/paper'})
        attempted = threading.Event()
        def denied(*args, **kwargs):
            attempted.set()
            return subprocess.CompletedProcess(args[0], 1)
        with patch('harness.platform.system', return_value='Darwin'), \
             patch('harness.shutil.which', return_value='/usr/bin/osascript'), \
             patch('harness.subprocess.run', side_effect=denied) as command:
            self.scheduler.tick(now=105)
            self.assertTrue(attempted.wait(timeout=3), '等待材料时应尝试通知')
            run = self.store.recent_runs()[0]
            self.assertEqual(run['status'], 'waiting_user')
            self.assertEqual(run['detail'], '需要上传可读取的论文 PDF。')
            self.assertIn('需要论文材料', command.call_args.args[0][-1])
            self.assertFalse(self.store.get_jobs()[0]['enabled'])

    def test_failed_and_missed_records_survive_notification_failure(self):
        for index, timeout in enumerate((False, True)):
            with self.subTest(timeout=timeout):
                job_id = 'failure-' + str(index)
                self.runner.side_effect = RuntimeError('转录阶段失败，原因为合成解码错误。')
                self.store.add(job_id, 'https://youtu.be/example', 'once', None, 100)
                attempted = threading.Event()
                def failed_notification(*args, **kwargs):
                    attempted.set()
                    if timeout:
                        raise subprocess.TimeoutExpired(args[0], 10)
                    return subprocess.CompletedProcess(args[0], 1)
                with patch('harness.platform.system', return_value='Darwin'), \
                     patch('harness.shutil.which', return_value='/usr/bin/osascript'), \
                     patch('harness.subprocess.run', side_effect=failed_notification):
                    self.scheduler.tick(now=105)
                    self.assertTrue(attempted.wait(timeout=3))
                    run = next(run for run in self.store.recent_runs() if run['job_id'] == job_id)
                    self.assertEqual(run['status'], 'failed')
                    self.assertIn('解码错误', run['detail'])
                    self.scheduler.logger.log.assert_any_call(logging.ERROR, '定时任务 {} 执行失败'.format(job_id))
        self.runner.reset_mock()
        self.store.add('missed-denied', 'https://youtu.be/example', 'once', None, 100)
        attempted = threading.Event()

        def denied(*args, **kwargs):
            attempted.set()
            return subprocess.CompletedProcess(args[0], 1)

        with patch('harness.platform.system', return_value='Darwin'), \
             patch('harness.shutil.which', return_value='/usr/bin/osascript'), \
             patch('harness.subprocess.run', side_effect=denied):
            self.scheduler.tick(now=200)
            self.assertTrue(attempted.wait(timeout=3))
        self.runner.assert_not_called()
        run = next(run for run in self.store.recent_runs() if run['job_id'] == 'missed-denied')
        self.assertEqual(run['status'], 'missed')


class VideoFallbackTests(unittest.TestCase):
    @patch("video_summary._download_audio")
    @patch("video_summary.importlib.util.find_spec", return_value=None)
    @patch("video_summary._get_metadata", return_value={})
    def test_missing_asr_is_reported_before_downloading_audio(self, metadata, spec, audio):
        with self.assertRaisesRegex(RuntimeError, "语音转录"):
            _get_video_material("https://youtu.be/example", Path("/tmp"))
        audio.assert_not_called()

    @patch("video_summary._transcribe_audio", return_value=([
        {"start": 0, "end": 2, "text": "转录文字"}
    ], "zh"))
    @patch("video_summary._download_audio", return_value=Path("audio.webm"))
    @patch("video_summary.importlib.util.find_spec", return_value=object())
    @patch("video_summary._download_caption", side_effect=RuntimeError("字幕不可用"))
    @patch("video_summary._get_metadata", return_value={
        "subtitles": {"zh": [{"ext": "vtt"}]}
    })
    def test_platform_caption_failure_falls_back_to_local_transcription(
        self, metadata, caption, spec, audio, transcribe
    ):
        progress = Mock()
        material = _get_video_material("https://youtu.be/example", Path("/tmp"), progress, Mock())
        self.assertEqual(material["source"], "本地 faster-whisper 转录")
        self.assertEqual(material["transcript"][0]["text"], "转录文字")
        audio.assert_called_once()
        transcribe.assert_called_once()
        self.assertIn("正在本地转录音频", [item.args[0] for item in progress.call_args_list])


if __name__ == "__main__":
    unittest.main()
