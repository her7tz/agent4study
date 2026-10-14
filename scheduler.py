#!/usr/bin/env python3
"""Persistent local scheduler for registered Agent capabilities."""

import os
import queue
import plistlib
import subprocess
import sys
import time
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness import JobStore, Settings, notify_local
from capabilities import get_capability, validate_parameters
from runtime_control import CancellationToken, RunCancelled
from paper_summary import MaterialRequired


RUN_GRACE_SECONDS = 60
POLL_SECONDS = 15


class SchedulerLease:
    """Share one scheduling loop between the CLI service and the local web UI."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> bool:
        if self.stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write("0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def _parse_clock(value: str) -> tuple:
    if not isinstance(value, str):
        raise ValueError("每日时间请使用 24 小时制 HH:MM。")
    try:
        parsed = time.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("每日时间请使用 24 小时制 HH:MM。") from exc
    return parsed.tm_hour, parsed.tm_min


def _local_epoch(day: date, hour: int, minute: int) -> float:
    return time.mktime((day.year, day.month, day.day, hour, minute, 0, -1, -1, -1))


def _next_daily_after(epoch: float, daily_time: str) -> float:
    hour, minute = _parse_clock(daily_time)
    local = time.localtime(epoch)
    day = date(local.tm_year, local.tm_mon, local.tm_mday) + timedelta(days=1)
    return _local_epoch(day, hour, minute)


def add_capability_daily_job(store: JobStore, capability: str, parameters: Dict[str, Any], clock: str) -> str:
    normalized = validate_parameters(capability, parameters)
    hour, minute = _parse_clock(clock)
    now = time.time()
    local = time.localtime(now)
    due = _local_epoch(date(local.tm_year, local.tm_mon, local.tm_mday), hour, minute)
    if due <= now:
        due = _next_daily_after(now, clock)
    job_id = uuid.uuid4().hex[:8]
    store.add(job_id, normalized.get("url", ""), "daily", clock, due, capability=capability, parameters=normalized)
    return job_id


def add_capability_once_job(store: JobStore, capability: str, parameters: Dict[str, Any], local_datetime: str) -> str:
    normalized = validate_parameters(capability, parameters)
    if not isinstance(local_datetime, str):
        raise ValueError("单次任务时间请使用 YYYY-MM-DD HH:MM。")
    try:
        parsed = time.strptime(local_datetime, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("单次任务时间请使用 YYYY-MM-DD HH:MM。") from exc
    due = time.mktime(parsed)
    if due <= time.time():
        raise ValueError("单次任务时间必须在未来。")
    job_id = uuid.uuid4().hex[:8]
    store.add(job_id, normalized.get("url", ""), "once", None, due, capability=capability, parameters=normalized)
    return job_id


def add_daily_job(store: JobStore, url: str, clock: str) -> str:
    """Backward-compatible shorthand for scheduling video summaries."""
    return add_capability_daily_job(store, "video_summary", {"url": url}, clock)


def add_once_job(store: JobStore, url: str, local_datetime: str) -> str:
    """Backward-compatible shorthand for scheduling video summaries."""
    return add_capability_once_job(store, "video_summary", {"url": url}, local_datetime)


def format_local_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class Scheduler:
    def __init__(
        self,
        store: JobStore,
        settings: Settings,
        runner: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        logger: Any,
        controlled_runner: Optional[Callable] = None,
    ):
        self.store = store
        self.settings = settings
        self.runner = runner
        self.logger = logger
        self.controlled_runner = controlled_runner
        # Research now belongs to the main conversation, outside business scheduling.
        retired=[job for job in store.get_jobs() if job.get("capability")=="web_research" and job.get("enabled")]
        for job in retired:
            store.remove(job["id"])
            store.record_run(job["id"],job["due_at"],"cancelled","联网研究已改为主 Agent 内置能力，旧独立定时计划已停用。")
            logger.info("旧联网研究计划 %s 已停用，历史与成果保留",job["id"])
        if retired:notify_local("联网研究计划已停用","联网研究现在通过 Agent 对话使用；历史报告仍保留。",logger)
        self.tokens = {}
        self.tokens_lock = threading.Lock()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self._work, name="scheduled-capabilities", daemon=True)
        self.worker.start()

    def recover_pending(self) -> None:
        with self.store._connect() as db:
            db.execute("UPDATE job_runs SET status='interrupted',detail='服务退出或重启时未完成，请重新安排。',finished_at=? WHERE status IN ('queued','running')", (time.time(),))

    def _dispatch(self, job: Dict[str, Any], scheduled_at: float) -> None:
        self.store.record_run(job["id"], scheduled_at, "queued", "已到计划时间，等待后台执行。")
        with self.tokens_lock:
            self.tokens[(job["id"], scheduled_at)] = CancellationToken()
        self.pending.put((job, scheduled_at))

    def cancel_run(self, run_id):
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("没有找到这次定时执行。")
        if row["status"] not in {"queued", "running"}:
            return {"run_id": run_id, "status": row["status"]}
        with self.tokens_lock:
            token = self.tokens.get((row["job_id"], row["scheduled_at"]))
        if not token:
            raise ValueError("这次执行由其他服务管理，请在对应服务中停止。")
        token.cancel()
        self.store.record_run(row["job_id"], row["scheduled_at"], "cancelled", "用户取消本次执行；后续计划保持原安排。")
        return {"run_id": run_id, "status": "cancelled"}

    def _work(self) -> None:
        while not self.stopping.is_set():
            try:
                item = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.stopping.is_set():
                self.pending.task_done()
                break
            try:
                self._execute(*item)
            except Exception as exc:
                self.logger.error("调度工作线程处理失败：%s", exc)
            finally:
                self.pending.task_done()

    def close(self) -> None:
        self.stopping.set()
        with self.tokens_lock:
            tokens = list(self.tokens.values())
        for token in tokens:
            token.cancel()
        while True:
            try:
                job, scheduled_at = self.pending.get_nowait()
            except queue.Empty:
                break
            self.store.record_run(job["id"], scheduled_at, "interrupted", "服务退出时任务仍在队列中，请重新安排。")
            self.pending.task_done()

    def _execute(self, job: Dict[str, Any], scheduled_at: float) -> None:
        started = time.time()
        capability = job.get("capability", "video_summary")
        key = (job["id"], scheduled_at)
        with self.tokens_lock:
            token = self.tokens.get(key) or CancellationToken()
        try:
            token.check()
            self.store.record_run(job["id"], scheduled_at, "running", "正在执行已注册功能。", started_at=started)
            self.logger.info("开始执行定时任务 %s，功能：%s", job["id"], capability)
            definition = get_capability(capability)
            parameters = job.get("parameters")
            # Callers supplying a pre-migration job dictionary remain usable.
            if "parameters" not in job and capability == "video_summary":
                parameters = {"url": job.get("url", ""), "include_frames": True}
            normalized = validate_parameters(capability, parameters)
            def progress(stage):
                token.check()
                self.store.record_run(job["id"], scheduled_at, "running", stage, started_at=started)
            progress.job_id=job["id"]
            progress.scheduled_at=scheduled_at
            result = self.controlled_runner(capability, normalized, token, progress) if self.controlled_runner else self.runner(capability, normalized)
            token.check()
            detail = "{}已完成：{}".format(definition.title, result.get("title", definition.title))
            output_path = result.get("path")
            self.store.record_run(
                job["id"], scheduled_at, "completed", detail, output_path, started
            )
            self.logger.info("定时任务 %s 完成：%s", job["id"], output_path)
            notify_local("定时任务已完成", detail, self.logger)
        except MaterialRequired as exc:
            self.store.record_run(job["id"],scheduled_at,"waiting_user",str(exc),None,started)
            self.logger.info("定时任务 %s 等待论文材料",job["id"])
        except RunCancelled:
            with self.store._connect() as db:
                row = db.execute("SELECT status FROM job_runs WHERE job_id=? AND scheduled_at=? ORDER BY id DESC LIMIT 1", (job["id"], scheduled_at)).fetchone()
            if row and row["status"] in {"queued", "running"}:
                self.store.record_run(job["id"], scheduled_at, "interrupted" if self.stopping.is_set() else "cancelled", "服务已退出。" if self.stopping.is_set() else "用户已取消本次执行。", started_at=started)
        except Exception as exc:
            detail = str(exc)
            self.store.record_run(job["id"], scheduled_at, "failed", detail, None, started)
            self.logger.exception("定时任务 %s 执行失败", job["id"])
            notify_local("定时任务执行失败", detail[:180], self.logger)
        finally:
            with self.tokens_lock:
                self.tokens.pop(key, None)

    def tick(self, now: Optional[float] = None) -> None:
        current = time.time() if now is None else now
        for job in self.store.get_due_jobs(current):
            current = time.time() if now is None else now
            due = float(job["due_at"])
            if job["cadence"] == "once":
                if current - due > RUN_GRACE_SECONDS:
                    detail = "电脑关机、睡眠或服务未运行期间错过；按设置跳过，不补跑。"
                    self.store.record_run(job["id"], due, "missed", detail)
                    self.store.set_due(job["id"], due, enabled=False)
                    self.logger.error("单次任务 %s 错过：%s", job["id"], detail)
                    notify_local("定时任务错过", "任务 {} 已跳过。".format(job["id"]), self.logger)
                else:
                    self._dispatch(job, due)
                    self.store.set_due(job["id"], due, enabled=False)
                continue

            missed = 0
            next_due = due
            ran = False
            while next_due <= current:
                if current - next_due <= RUN_GRACE_SECONDS and not ran:
                    self._dispatch(job, next_due)
                    ran = True
                else:
                    detail = "电脑关机、睡眠或服务未运行期间错过；按设置跳过，不补跑。"
                    self.store.record_run(job["id"], next_due, "missed", detail)
                    self.logger.error(
                        "每日任务 %s 错过计划时间 %s",
                        job["id"],
                        format_local_time(next_due),
                    )
                    missed += 1
                next_due = _next_daily_after(next_due, job["daily_time"])
            self.store.set_due(job["id"], next_due, enabled=True)
            if missed:
                notify_local(
                    "定时任务错过",
                    "任务 {} 有 {} 次执行已跳过。".format(job["id"], missed),
                    self.logger,
                )

    def run_forever(self) -> None:
        lease = SchedulerLease(self.settings.data_home / "scheduler.lock")
        if not lease.acquire():
            raise RuntimeError("已有本地调度服务运行，当前进程不重复启动。")
        self.logger.info("本地 Harness 调度服务已启动。")
        self.recover_pending()
        try:
            while True:
                self.tick()
                time.sleep(POLL_SECONDS)
        finally:
            self.close()
            lease.release()


def install_macos_launch_agent(settings: Settings, script_path: Path) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("自动安装后台服务目前只支持 macOS；其他系统可直接运行 service run。")
    settings.prepare()
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "com.minimal-agent.local.plist"
    payload = {
        "Label": "com.minimal-agent.local",
        "ProgramArguments": [sys.executable, str(script_path.resolve()), "service", "run"],
        "WorkingDirectory": str(script_path.resolve().parent),
        "EnvironmentVariables": {
            "MINIMAL_AGENT_HOME": str(settings.data_home.resolve()),
            "MINIMAL_AGENT_OUTPUT": str(settings.output_dir.resolve()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(settings.data_home / "service.out.log"),
        "StandardErrorPath": str(settings.data_home / "service.err.log"),
    }
    with plist_path.open("wb") as stream:
        plistlib.dump(payload, stream)
    domain = "gui/{}".format(os.getuid())
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("launchd 安装失败：{}".format(result.stderr.strip()))
    return plist_path


def uninstall_macos_launch_agent() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("自动卸载后台服务目前只支持 macOS。")
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.minimal-agent.local.plist"
    domain = "gui/{}".format(os.getuid())
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if plist_path.exists():
        plist_path.unlink()
