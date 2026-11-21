"""Check launchd configuration and CLI errors without touching user services."""
import contextlib
import errno
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import agent
from harness import Settings
from scheduler import install_macos_launch_agent, uninstall_macos_launch_agent


class LaunchAgentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / "临时用户"
        self.project = self.root / "项目 含空格"
        self.project.mkdir()
        self.script = self.project / "agent.py"
        self.script.write_text("# launchd acceptance fixture; never executed\n")
        self.plist = self.home / "Library/LaunchAgents/com.minimal-agent.local.plist"
        self.fake_key = "launchd-fixture-key-not-for-plist"
        self.path = "/fixture/媒体工具:/usr/bin:/bin"
        environment = {
            "MINIMAL_AGENT_HOME": str(self.root / "临时数据"),
            "MINIMAL_AGENT_OUTPUT": str(self.root / "临时内容"),
            "DEEPSEEK_API_KEY": self.fake_key,
            "DEEPSEEK_MODEL": "launchd-fixture-model",
            "PATH": self.path,
        }
        for mock in (
            patch.dict(os.environ, environment, clear=True),
            patch("scheduler.Path.home", return_value=self.home),
            patch("scheduler.sys.platform", "darwin"),
            patch("scheduler.os.getuid", return_value=501),
            patch("agent.configure_logging"),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        command = patch("scheduler.subprocess.run")
        self.run = command.start()
        self.addCleanup(command.stop)
        self.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.settings = Settings()

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = agent.main(["service", *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def make_plist(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({"Label": "com.minimal-agent.local"}))

    def test_install_generates_runtime_paths_login_restart_and_no_key(self):
        result = install_macos_launch_agent(self.settings, self.script)
        self.assertEqual(result, self.plist)
        payload = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(payload["Label"], "com.minimal-agent.local")
        self.assertEqual(payload["ProgramArguments"],
                         [sys.executable, str(self.script.resolve()), "service", "run"])
        self.assertEqual(payload["WorkingDirectory"], str(self.project.resolve()))
        self.assertEqual(payload["EnvironmentVariables"], {
            "MINIMAL_AGENT_HOME": str(self.settings.data_home.resolve()),
            "MINIMAL_AGENT_OUTPUT": str(self.settings.output_dir.resolve()),
            "PATH": self.path,
        })
        self.assertIs(payload["RunAtLoad"], True)
        self.assertIs(payload["KeepAlive"], True)
        self.assertEqual(payload["StandardOutPath"], str(self.settings.data_home / "service.out.log"))
        self.assertEqual(payload["StandardErrorPath"], str(self.settings.data_home / "service.err.log"))
        self.assertTrue(self.settings.data_home.is_dir())
        self.assertTrue(self.settings.output_dir.is_dir())
        self.assertNotIn(self.fake_key.encode(), self.plist.read_bytes())
        self.assertNotIn(b"DEEPSEEK_API_KEY", self.plist.read_bytes())
        self.assertEqual(self.run.call_args_list, [
            call(["launchctl", "bootout", "gui/501", str(self.plist)], check=False,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            call(["launchctl", "bootstrap", "gui/501", str(self.plist)],
                 capture_output=True, text=True),
        ])

    def test_install_uses_system_tool_path_when_path_is_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            install_macos_launch_agent(self.settings, self.script)
        payload = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(payload["EnvironmentVariables"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_install_cli_reports_bootstrap_failure_without_success_message(self):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 3, "", "No such process"),
            subprocess.CompletedProcess([], 5, "", "Bootstrap failed: fixture access denied\n"),
        ]
        result, stdout, stderr = self.cli("install")
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("launchd 安装失败：Bootstrap failed: fixture access denied", stderr)
        self.assertNotIn("已安装并启动", stdout + stderr)

    def test_install_cli_success_names_the_generated_plist(self):
        result, stdout, stderr = self.cli("install")
        self.assertEqual(result, 0)
        self.assertIn("后台服务已安装并启动：" + str(self.plist), stdout)
        self.assertEqual(stderr, "")
        payload = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(payload["ProgramArguments"][1], str(Path(agent.__file__).resolve()))

    def test_uninstall_boots_out_and_removes_only_the_temporary_plist(self):
        self.make_plist()
        other = self.plist.parent / "other-fixture.plist"
        other.write_text("retained")
        result, stdout, stderr = self.cli("uninstall")
        self.assertEqual(result, 0)
        self.assertIn("后台服务已停止并移除", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse(self.plist.exists())
        self.assertEqual(other.read_text(), "retained")
        self.run.assert_called_once_with(
            ["launchctl", "bootout", "gui/501/com.minimal-agent.local"],
            capture_output=True, text=True)

    def test_uninstall_missing_service_is_idempotent(self):
        self.run.return_value = subprocess.CompletedProcess([], errno.ESRCH, "", "服务不存在（合成文本）")
        self.assertIsNone(uninstall_macos_launch_agent())
        self.assertFalse(self.plist.exists())

    def test_uninstall_absent_service_removes_its_remaining_plist(self):
        self.make_plist()
        self.run.return_value = subprocess.CompletedProcess([], errno.ESRCH, "", "unparsed fixture message")
        result, stdout, stderr = self.cli("uninstall")
        self.assertEqual(result, 0)
        self.assertIn("后台服务已停止并移除", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse(self.plist.exists())

    def test_uninstall_missing_plist_still_stops_the_service_label(self):
        self.assertIsNone(uninstall_macos_launch_agent())
        self.run.assert_called_once_with(
            ["launchctl", "bootout", "gui/501/com.minimal-agent.local"],
            capture_output=True, text=True)

    def test_uninstall_cli_reports_launchctl_execution_error(self):
        self.make_plist()
        self.run.side_effect = FileNotFoundError("fixture launchctl unavailable")
        result, stdout, stderr = self.cli("uninstall")
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("fixture launchctl unavailable", stderr)
        self.assertTrue(self.plist.exists())

    def test_uninstall_cli_does_not_claim_stopped_when_bootout_is_denied(self):
        self.make_plist()
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "Operation not permitted")
        result, stdout, stderr = self.cli("uninstall")
        self.assertEqual((result, "已停止并移除" in stdout, self.plist.exists()), (1, False, True))
        self.assertIn("Operation not permitted", stderr)

    def test_uninstall_unknown_error_or_misleading_text_preserves_plist(self):
        self.make_plist()
        for code, detail in [(errno.EACCES, "No such process"), (errno.EIO, "")]:
            with self.subTest(code=code):
                self.run.return_value = subprocess.CompletedProcess([], code, "", detail)
                result, stdout, stderr = self.cli("uninstall")
                self.assertEqual((result, "已停止并移除" in stdout, self.plist.exists()), (1, False, True))
                self.assertIn("launchd 卸载失败", stderr)
                self.assertIn("退出码 {}".format(code), stderr)

    def test_non_macos_install_and_uninstall_do_not_write_or_run_launchctl(self):
        with patch("scheduler.sys.platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "只支持 macOS"):
                install_macos_launch_agent(self.settings, self.script)
            with self.assertRaisesRegex(RuntimeError, "只支持 macOS"):
                uninstall_macos_launch_agent()
        self.assertFalse(self.plist.exists())
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
