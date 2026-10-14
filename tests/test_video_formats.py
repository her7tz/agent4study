"""Frame fetching must accept platforms that offer separate audio/video streams."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_summary import VIDEO_FORMAT_SELECTOR, _download_video


class VideoFormatTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "yt-dlp Python 包未安装")
    def test_separate_video_stream_is_selected_without_requiring_audio(self):
        from yt_dlp import YoutubeDL

        formats = [
            {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
            {"format_id": "video360", "ext": "mp4", "vcodec": "h264", "acodec": "none", "height": 360},
            {"format_id": "video720", "ext": "mp4", "vcodec": "h264", "acodec": "none", "height": 720},
            {"format_id": "video1080", "ext": "mp4", "vcodec": "h264", "acodec": "none", "height": 1080},
        ]
        for item in formats:
            item["url"] = "https://example.invalid/media"
        with YoutubeDL({"quiet": True}) as downloader:
            selector = downloader.build_format_selector(VIDEO_FORMAT_SELECTOR)
            chosen = list(selector({"formats": formats, "has_merged_format": False,
                                    "incomplete_formats": False}))
        self.assertEqual(chosen[0]["format_id"], "video720")

    @patch("video_summary.resolve_tool", return_value="/fixture/ffmpeg")
    @patch("video_summary._run_ytdlp", side_effect=RuntimeError("requested format is unavailable"))
    def test_frame_fetch_failure_is_logged_for_diagnosis(self, run, resolve):
        logger = Mock()
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(_download_video("https://youtu.be/example", Path(folder), logger))
        logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
