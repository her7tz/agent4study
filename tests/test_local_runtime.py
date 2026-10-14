"""Offline regressions for shared scheduling and video fallback behavior."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness import JobStore, Settings
from scheduler import Scheduler, SchedulerLease
from video_summary import _get_video_material


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = JobStore(self.root / "tasks.sqlite3")
        self.settings = Settings()
        self.settings.data_home = self.root
        self.runner = Mock(return_value={"title": "结果", "path": "summary.md"})
        self.scheduler = Scheduler(self.store, self.settings, self.runner, Mock())

    def tearDown(self):
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
        self.store.add("missed", "https://youtu.be/example", "once", None, 100)
        self.scheduler.tick(now=200)
        self.runner.assert_not_called()
        self.assertEqual(self.store.recent_runs()[0]["status"], "missed")
        self.assertFalse(self.store.get_jobs()[0]["enabled"])
        notify.assert_called_once()

    @patch("scheduler.notify_local")
    def test_one_time_job_runs_only_once(self, notify):
        self.store.add("due", "https://youtu.be/example", "once", None, 100)
        self.scheduler.tick(now=105)
        self.scheduler.tick(now=110)
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
