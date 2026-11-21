"""Offline checks for caption-only videos and reuse of guest media metadata."""

import json
import importlib.util
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from video_summary import (
    _choose_caption,
    _download_audio,
    _download_caption,
    _download_video,
    _get_metadata,
    _get_video_material,
    _ytdlp_source,
    validate_video_url,
)


URL = "https://www.bilibili.com/video/BV1it411g7CX/"
FORMATS = [
    {"format_id": "audio", "url": "https://example.invalid/audio", "vcodec": "none"},
    {"format_id": "video", "url": "https://example.invalid/video", "acodec": "none"},
]
CAPTION = "1\n00:00:00,000 --> 00:00:02,000\n真实字幕内容\n\n"


class VideoMetadataTests(unittest.TestCase):
    def cached_info(self, arguments, info):
        self.assertNotIn(URL, arguments)
        self.assertIn("--load-info-json", arguments)
        cache = Path(arguments[arguments.index("--load-info-json") + 1])
        self.assertNotIn(cache.suffix.lower(), {".json", ".json3", ".srt", ".vtt"})
        cached_info = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(cached_info, {key: value for key, value in info.items() if key != "webpage_url"})
        self.assertNotIn("webpage_url", cached_info)
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        return cache

    @patch("video_summary._run_ytdlp")
    def test_metadata_allows_no_formats_for_caption_lookup(self, run):
        info = {"id": "BV1it411g7CX", "formats": [], "subtitles": {}}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(info), "")
        self.assertEqual(_get_metadata(URL), info)
        arguments = run.call_args.args[0]
        self.assertIn("--skip-download", arguments)
        self.assertIn("--ignore-no-formats-error", arguments)
        self.assertIn(URL, arguments)

    def test_explicit_bilibili_part_must_be_positive_and_unambiguous(self):
        for query in ("p=0", "p=-1", "p=", "p=2&p=3", "p=2.5"):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "分集"):
                validate_video_url(URL + "?" + query)
        self.assertEqual(validate_video_url(URL + "?p=02&from=test"), URL + "?p=02&from=test")

    @patch("video_summary._run_ytdlp")
    def test_explicit_part_rejects_wrong_recording_instead_of_using_first(self, run):
        for info in ({"id": "BV1it411g7CX_p1"}, {"id": "BV1Ybpd6gECu_p2"},
                     {"_type": "playlist", "entries": [{"id": "BV1it411g7CX_p1"}]}):
            with self.subTest(info=info), self.assertRaisesRegex(RuntimeError, "指定分集"):
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps(info), "")
                _get_metadata(URL + "?p=2")

    @patch("video_summary._run_ytdlp")
    def test_explicit_part_canonical_source_uses_confirmed_metadata(self, run):
        info = {"id": "BV1it411g7CX_p2", "webpage_url": URL, "formats": FORMATS}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(info), "")
        material = _get_metadata(URL + "?p=02&spm_id_from=fixture")
        self.assertEqual(material["webpage_url"], URL + "?p=2")
        self.assertEqual(material["formats"], FORMATS)

    @patch("video_summary._run_ytdlp")
    def test_explicit_part_checks_readable_material_and_accepts_caption_only(self, run):
        info = {"id": "BV1it411g7CX_p2", "formats": [], "subtitles": {"danmaku": [{"ext": "xml"}]}}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(info), "")
        with self.assertRaisesRegex(RuntimeError, "指定分集没有可读"):
            _get_metadata(URL + "?p=2")
        info["subtitles"]["zh"] = [{"ext": "srt"}]
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(info), "")
        self.assertEqual(_get_metadata(URL + "?p=2")["webpage_url"], URL + "?p=2")

    def test_danmaku_and_unsupported_caption_formats_are_excluded(self):
        info = {"subtitles": {
            "danmaku": [{"ext": "xml"}],
            "zh": [{"ext": "xml"}],
            "en": [{"ext": "srt"}],
        }}
        self.assertEqual(_choose_caption(info), ("en", True))
        self.assertIsNone(_choose_caption({"subtitles": {"danmaku": [{"ext": "xml"}]}}))
        self.assertIsNone(_choose_caption({"subtitles": {"zh": [None, "bad"]}}))

    def test_broken_manual_caption_uses_same_language_automatic_track_before_asr(self):
        tracks = {"zh": [{"ext": "json3"}]}
        info = {"subtitles": tracks, "automatic_captions": tracks}
        attempts = []
        def download(url, language, folder, info, manual):
            attempts.append((language, manual, folder))
            path = folder / "video.zh.json3"
            events = [None] if manual else [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "可用字幕"}]}]
            path.write_text(json.dumps({"events": events}))
            return path
        with tempfile.TemporaryDirectory() as tmp, patch("video_summary._get_metadata", return_value=info), \
             patch("video_summary._download_caption", side_effect=download), patch("video_summary._download_audio") as audio:
            result = _get_video_material(URL, Path(tmp))
        self.assertEqual([(lang, manual) for lang, manual, _ in attempts], [("zh", True), ("zh", False)])
        self.assertNotEqual(attempts[0][2], attempts[1][2])
        self.assertEqual(result["source"], "平台自动字幕")
        self.assertEqual(result["transcript"], [{"start": 1, "end": 3, "text": "可用字幕"}])
        audio.assert_not_called()

    def test_supported_chinese_automatic_caption_keeps_language_priority(self):
        info = {
            "subtitles": {"en": [{"ext": "srt"}]},
            "automatic_captions": {"zh-Hans": [{"ext": "json3"}]},
        }
        self.assertEqual(_choose_caption(info), ("zh-Hans", False))

    def test_caption_reuses_metadata_without_formats_and_selects_language_file(self):
        info = {"id": "BV1it411g7CX", "formats": [], "subtitles": {"zh": [{"ext": "srt"}]}}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            expected = folder / "BV1it411g7CX.zh.srt"
            (folder / "other.en.vtt").write_text("unrelated", encoding="utf-8")
            (folder / "unrelated.json").write_text("{}", encoding="utf-8")
            (folder / "BV1it411g7CX.danmaku.xml").write_text("<i />", encoding="utf-8")
            cached_paths = []

            def run(arguments, timeout):
                cached_paths.append(self.cached_info(arguments, info))
                self.assertIn("--ignore-no-formats-error", arguments)
                self.assertIn("--skip-download", arguments)
                self.assertEqual(arguments[arguments.index("--sub-format") + 1], "vtt/srt/json3/json")
                expected.write_text(CAPTION, encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("video_summary._run_ytdlp", side_effect=run):
                self.assertEqual(_download_caption(URL, "zh", folder, info=info), expected)
            self.assertTrue(all(not cache.exists() for cache in cached_paths))

    def test_audio_reuses_original_format_list_without_extracting_url(self):
        info = {"id": "BV1it411g7CX", "formats": FORMATS}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            output = folder / "audio.m4a"
            cached_paths = []

            def run(arguments, timeout):
                cached_paths.append(self.cached_info(arguments, info))
                self.assertNotIn("--ignore-no-formats-error", arguments)
                self.assertEqual(arguments[arguments.index("-f") + 1], "bestaudio/best")
                output.write_bytes(b"fixture")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("video_summary._run_ytdlp", side_effect=run) as download:
                self.assertEqual(_download_audio(URL, folder, info=info), output)
                download.assert_called_once()
            self.assertTrue(all(not cache.exists() for cache in cached_paths))

    @patch("video_summary.resolve_tool", return_value="/fixture/ffmpeg")
    def test_frame_video_reuses_metadata_and_preserves_logger_argument(self, resolve):
        info = {"id": "BV1it411g7CX", "formats": FORMATS}
        logger = Mock()
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            output = folder / "video.mp4"
            cached_paths = []

            def run(arguments, timeout):
                cached_paths.append(self.cached_info(arguments, info))
                self.assertNotIn("--ignore-no-formats-error", arguments)
                output.write_bytes(b"fixture")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("video_summary._run_ytdlp", side_effect=run) as download:
                self.assertEqual(_download_video(URL, folder, logger, info=info), output)
                download.assert_called_once()
            logger.warning.assert_not_called()
            self.assertTrue(all(not cache.exists() for cache in cached_paths))

    @patch("video_summary.resolve_tool", return_value="/fixture/ffmpeg")
    @patch("video_summary._run_ytdlp")
    def test_no_formats_audio_fails_clearly_and_frames_degrade(self, run, resolve):
        info = {"id": "BV1it411g7CX", "formats": []}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            with self.assertRaisesRegex(RuntimeError, "平台未提供.*语音转录"):
                _download_audio(URL, folder, info=info)
            logger = Mock()
            self.assertIsNone(_download_video(URL, folder, logger, info=info))
            logger.warning.assert_called_once()
        run.assert_not_called()

    def test_caption_only_material_requires_no_audio_or_asr(self):
        info = {"id": "BV1it411g7CX", "formats": [], "subtitles": {"zh": [{"ext": "srt"}]}}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)

            def run(arguments, timeout):
                if "--dump-single-json" in arguments:
                    return subprocess.CompletedProcess([], 0, json.dumps(info), "")
                self.cached_info(arguments, info)
                template = arguments[arguments.index("-o") + 1]
                destination = Path(template % {"id": info["id"], "ext": "zh.srt"})
                destination.write_text(CAPTION, encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("video_summary._run_ytdlp", side_effect=run) as download, \
                    patch("video_summary._download_audio") as audio, \
                    patch("video_summary._transcribe_audio") as asr:
                material = _get_video_material(URL, folder)
                self.assertEqual(material["source"], "平台字幕")
                self.assertEqual(material["transcript"][0]["text"], "真实字幕内容")
                self.assertEqual(download.call_count, 2)
                audio.assert_not_called()
                asr.assert_not_called()

    def test_expired_media_error_does_not_retry_or_reextract_and_removes_cache(self):
        info = {"id": "BV1it411g7CX", "formats": FORMATS, "webpage_url": URL}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            cached_paths = []

            def run(arguments, timeout):
                cached_paths.append(self.cached_info(arguments, info))
                raise RuntimeError("HTTP Error 403: Forbidden")

            with patch("video_summary._run_ytdlp", side_effect=run) as download:
                with self.assertRaisesRegex(RuntimeError, "403"):
                    _download_audio(URL, folder, info=info)
                download.assert_called_once()
            self.assertTrue(all(not cache.exists() for cache in cached_paths))
            self.assertEqual(info["webpage_url"], URL)

    @unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "yt-dlp Python 包未安装")
    def test_ytdlp_info_file_download_error_cannot_fall_back_to_webpage(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        info = {
            "id": "BV1it411g7CX", "formats": FORMATS, "webpage_url": URL,
            "http_headers": {"Referer": URL},
        }
        with tempfile.TemporaryDirectory() as name:
            with _ytdlp_source(URL, Path(name), info) as arguments:
                cache = self.cached_info(arguments, info)
                with YoutubeDL({"quiet": True}) as downloader, \
                        patch.object(downloader, "process_ie_result", side_effect=DownloadError("HTTP Error 403")), \
                        patch.object(downloader, "download") as reextract:
                    with self.assertRaisesRegex(DownloadError, "403"):
                        downloader.download_with_info_file(str(cache))
                    reextract.assert_not_called()
        self.assertEqual(info["webpage_url"], URL)


if __name__ == "__main__":
    unittest.main()
