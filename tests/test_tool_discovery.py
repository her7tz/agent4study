"""Offline checks for local dependency discovery and subprocess arguments."""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import harness
from video_summary import _extract_frame, _run_ytdlp, _transcribe_audio
from web_server import WebRuntime


class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.project = self.root / "project"
        self.interpreter = self.root / "runtime" / "bin" / "python"
        self.project_patch = patch.object(harness, "PROJECT_DIR", self.project)
        self.interpreter_patch = patch.object(harness.sys, "executable", str(self.interpreter))
        self.path_patch = patch.object(harness.shutil, "which", return_value=None)
        self.project_patch.start()
        self.interpreter_patch.start()
        self.system_path = self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.interpreter_patch.stop()
        self.project_patch.stop()
        self.folder.cleanup()

    def executable(self, folder, name):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_local_tool_precedence_before_system_path(self):
        locations = [
            self.interpreter.parent,
            self.project / ".venv" / "bin",
            self.project / ".local" / "tools",
        ]
        binaries = [self.executable(folder, "yt-dlp") for folder in locations]
        self.system_path.return_value = "/system/bin/yt-dlp"
        for binary in binaries:
            self.assertEqual(harness.resolve_tool("yt-dlp"), str(binary))
            self.system_path.assert_not_called()
            binary.unlink()
        self.assertEqual(harness.resolve_tool("yt-dlp"), "/system/bin/yt-dlp")

    def test_non_executable_project_file_is_ignored(self):
        binary = self.executable(self.project / ".local" / "tools", "node")
        binary.chmod(0o644)
        self.assertIsNone(harness.resolve_tool("node"))

    def test_ffmpeg_wheel_executable_is_available_without_downloading(self):
        binary = self.executable(self.root / "package", "ffmpeg-arm64-v7.1")
        package = SimpleNamespace(get_ffmpeg_exe=Mock(return_value=str(binary)))
        with patch.object(harness.importlib, "import_module", return_value=package) as imported:
            self.assertEqual(harness.resolve_tool("ffmpeg"), str(binary))
        imported.assert_called_once_with("imageio_ffmpeg")
        package.get_ffmpeg_exe.assert_called_once_with()

    def test_invalid_package_ffmpeg_is_not_reported_as_ready(self):
        package = SimpleNamespace(get_ffmpeg_exe=Mock(return_value=str(self.root / "missing")))
        with patch.object(harness.importlib, "import_module", return_value=package):
            self.assertIsNone(harness.resolve_tool("ffmpeg"))

    def test_missing_optional_package_is_safe(self):
        with patch.object(harness.importlib, "import_module", side_effect=ImportError):
            self.assertIsNone(harness.resolve_tool("ffmpeg"))

    def test_missing_other_tool_does_not_probe_ffmpeg_package(self):
        with patch.object(harness.importlib, "import_module") as imported:
            self.assertIsNone(harness.resolve_tool("deno"))
        imported.assert_not_called()


class MediaCommandTests(unittest.TestCase):
    @patch("video_summary.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    def test_ytdlp_receives_exact_ffmpeg_and_preferred_deno_paths(self, run):
        tools = {
            "yt-dlp": "/project/.venv/bin/yt-dlp",
            "ffmpeg": "/package/ffmpeg-arm64-v7.1",
            "deno": "/project/.local/tools/deno",
            "node": "/system/bin/node",
        }
        with patch("video_summary.resolve_tool", side_effect=tools.get):
            _run_ytdlp(["--dump-single-json", "https://youtu.be/example"])
        command = run.call_args.args[0]
        self.assertEqual(command[0], tools["yt-dlp"])
        self.assertEqual(command[command.index("--ffmpeg-location") + 1], tools["ffmpeg"])
        self.assertEqual(command[command.index("--js-runtimes") + 1], "deno:" + tools["deno"])

    @patch("video_summary.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    def test_node_is_explicit_when_deno_is_missing(self, run):
        tools = {"yt-dlp": "/project/yt-dlp", "node": "/project/node"}
        with patch("video_summary.resolve_tool", side_effect=tools.get):
            _run_ytdlp(["https://youtu.be/example"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--js-runtimes") + 1], "node:/project/node")
        self.assertNotIn("--ffmpeg-location", command)

    @patch("video_summary.subprocess.run")
    @patch("video_summary.resolve_tool", return_value=None)
    def test_missing_downloader_fails_before_any_subprocess(self, resolve, run):
        with self.assertRaisesRegex(RuntimeError, "yt-dlp"):
            _run_ytdlp(["https://youtu.be/example"])
        run.assert_not_called()

    @patch("video_summary.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
    @patch("video_summary.resolve_tool", return_value="/package/ffmpeg-arm64-v7.1")
    def test_frame_extraction_uses_discovered_executable(self, resolve, run):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "frame.jpg"
            target.write_bytes(b"frame fixture")
            self.assertTrue(_extract_frame(Path("video.mp4"), 12, target))
        self.assertEqual(run.call_args.args[0][0], "/package/ffmpeg-arm64-v7.1")


class DependencyStatusTests(unittest.TestCase):
    @patch("web_server.asr_runtime_status", return_value={"ready": True, "error": None})
    def test_status_matches_shared_discovery_and_current_python_asr(self, probe):
        runtime = object.__new__(WebRuntime)
        runtime.snapshot = Mock(return_value=SimpleNamespace(
            api_key="fixture", model="fixture-model", base_url="https://example.invalid",
            output_dir=Path("/tmp/fixture-summary"),
        ))
        runtime.stop_event = threading.Event()
        runtime.scheduler_owned = True
        runtime.token = "fixture-token"
        tools = {"yt-dlp": "/project/yt-dlp", "ffmpeg": "/package/ffmpeg", "node": "/project/node"}
        with patch("web_server.resolve_tool", side_effect=tools.get), patch("web_server.importlib.util.find_spec", return_value=object()):
            status = runtime.status()
        self.assertEqual(status["dependencies"], {
            "yt_dlp": True, "ffmpeg": True, "asr": True, "javascript_runtime": True, "paper_pdf": True,
        })
        probe.assert_called_once_with()
        self.assertNotIn("api_key", status)


class LocalASRModelTests(unittest.TestCase):
    def test_cached_base_is_used_and_explicit_model_override_takes_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model_root = root / "models"
            cached = model_root / "whisper-base"
            cached.mkdir(parents=True)
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (cached / name).write_bytes(b"fixture")
            model = Mock()
            model.transcribe.return_value = (
                [SimpleNamespace(start=0, end=1, text="字幕")], SimpleNamespace(language="zh")
            )
            factory = Mock(return_value=model)
            with patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=factory)}):
                with patch("video_summary.resolve_data_home", return_value=root):
                    with patch.dict("video_summary.os.environ", {"WHISPER_MODEL": ""}):
                        _transcribe_audio(Path("fixture.wav"))
                    self.assertEqual(factory.call_args.args[0], str(cached))
                    self.assertEqual(factory.call_args.kwargs["download_root"], str(model_root))
                    with patch.dict("video_summary.os.environ", {"WHISPER_MODEL": "small"}):
                        _transcribe_audio(Path("fixture.wav"))
                    self.assertEqual(factory.call_args.args[0], "small")
                    (cached / "tokenizer.json").unlink()
                    with patch.dict("video_summary.os.environ", {"WHISPER_MODEL": ""}):
                        _transcribe_audio(Path("fixture.wav"))
                    self.assertEqual(factory.call_args.args[0], "base")


if __name__ == "__main__":
    unittest.main()
