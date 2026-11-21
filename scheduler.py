#!/usr/bin/env python3
"""Persistent local scheduler for registered Agent capabilities."""

import errno
import os
import queue
import sqlite3
import plistlib
import subprocess
import sys
import time
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_profiles import AgentProfiles, execution_profile, resolve_agent
from harness import JobStore, SecretSafeLogger, Settings, notify_local
from capabilities import capability_settings_role, get_capability, validate_parameters
from runtime_control import CancellationToken, RunCancelled
from paper_summary import MaterialRequired


RUN_GRACE_SECONDS = 60
POLL_SECONDS = 15
CANCELLATION_POLL_SECONDS = 1
STARTING_STAGE = "正在执行已注册功能。"


def failed_stage(stage: str) -> str:
    return "失败于：" + stage.rstrip("。")[:160]


def _storage_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    # Python 3.9 and 3.10 expose the SQLite message but not the result code.
    return str(error).startswith(("database is locked", "database table is locked", "database schema is locked"))


def scheduler_clock():
    """Describe the same system-local clock used by mktime/localtime.

    The offset describes now, not a promise about future daylight saving time.
    Existing jobs and their due_at values are not reinterpreted.
    """
    now = datetime.now().astimezone()
    offset = now.strftime('%z')
    offset = offset[:3] + ':' + offset[3:]
    return {'timezone': 'system-local', 'current_time': now.isoformat(timespec='seconds'),
            'timezone_label': '本机系统时间（{}，UTC{}）'.format(now.tzname(), offset)}


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
    wall_time = (day.year, day.month, day.day, hour, minute)
    candidates = [time.mktime((*wall_time, 0, -1, -1, dst)) for dst in (0, 1)]
    matches = [epoch for epoch in candidates if time.localtime(epoch)[:5] == wall_time]
    # A repeated clock time selects its first occurrence. A nonexistent time
    # gets a forward-normalized marker so daily polling can journal the skip.
    return min(matches) if matches else max(candidates)


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
    due = _local_epoch(date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday), parsed.tm_hour, parsed.tm_min)
    if time.localtime(due)[:5] != parsed[:5]:
        raise ValueError("该时间在本机时区不存在，可能处于夏令时跳时区间，请选择其他时间。")
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
        executor=None,
        settings_provider: Optional[Callable[[], Settings]] = None,
    ):
        self.store = store
        self.settings = settings
        self.runner = runner
        self.logger = logger
        self.controlled_runner = controlled_runner
        # A settings provider enables one shared role snapshot for the worker and its diagnostics.
        self.settings_provider = settings_provider
        # Research now belongs to the main conversation, outside business scheduling.
        retired=[job for job in store.get_jobs() if job.get("capability")=="web_research" and job.get("enabled")]
        for job in retired:
            store.remove(job["id"])
            store.record_run(job["id"],job["due_at"],"cancelled","联网研究已改为主 Agent 内置能力，旧独立定时计划已停用。")
            logger.info("旧联网研究计划 %s 已停用，历史与成果保留",job["id"])
        if retired:notify_local("联网研究计划已停用","联网研究现在通过 Agent 对话使用；历史报告仍保留。",logger)
        self.tokens = {}
        self.tokens_lock = threading.Lock()
        from runtime_pool import ParallelDaemonExecutor
        self.stopping=threading.Event()
        self.recovery_complete = threading.Event()
        self.executor=executor or ParallelDaemonExecutor(getattr(settings,"max_parallel_tasks",4))
        self.owns_executor=executor is None

    def recover_pending(self) -> None:
        with self.store._connect() as db:
            db.execute("""UPDATE job_runs SET status='interrupted',detail='服务退出或重启时未完成，请重新安排。',finished_at=?
                          WHERE status IN ('queued','running') AND continuation_task_id IS NULL""", (time.time(),))

    def recover_until_ready(self, stop_event, retry_seconds: float) -> bool:
        while not stop_event.is_set():
            try:
                self.recover_pending()
            except sqlite3.OperationalError as exc:
                if not _storage_busy(exc):
                    raise
                self.logger.warning("定时任务恢复正在等待数据库写锁，稍后重试。")
                stop_event.wait(retry_seconds)
            else:
                self.recovery_complete.set()
                return True
        return False

    def _dispatch(self, job: Dict[str, Any], scheduled_at: float) -> None:
        with self.tokens_lock:
            self.tokens[(job["id"], scheduled_at)] = CancellationToken()
        try:self.executor.submit(self._execute,job,scheduled_at)
        except RuntimeError:
            with self.tokens_lock:self.tokens.pop((job["id"],scheduled_at),None)
            self.store.record_run(job["id"],scheduled_at,"interrupted","服务正在关闭，未执行本次任务。")

    def cancel_run(self, run_id):
        row = self.store.cancel_run(run_id)
        if not row:
            raise ValueError("没有找到这次定时执行。")
        if row["status"] == "cancelled" and row["continuation_task_id"] is None:
            with self.tokens_lock:
                token = self.tokens.get((row["job_id"], row["scheduled_at"]))
            if token:
                token.cancel()
        return row

    def _signal_cancelled_runs(self):
        with self.tokens_lock:
            pending = {key: token for key, token in self.tokens.items() if not token.event.is_set()}
        if pending:
            for key in self.store.cancelled_occurrences(pending):
                pending[key].cancel()

    def close(self):
        self.stopping.set()
        self.recovery_complete.clear()
        with self.tokens_lock:tokens=list(self.tokens.items())
        for (job_id,scheduled_at),token in tokens:
            token.cancel()
            with self.store._connect() as db:
                row=db.execute("SELECT status FROM job_runs WHERE job_id=? AND scheduled_at=?",(job_id,scheduled_at)).fetchone()
            if row and row['status'] in {'queued','running'}:self.store.record_run(job_id,scheduled_at,'interrupted','服务退出时任务未完成。')
        if self.owns_executor:self.executor.shutdown(wait=False,cancel_futures=True)

    def join(self,timeout=None):
        if self.owns_executor:self.executor.join(timeout)

    def _record_run(self, token, job_id, scheduled_at, status, detail, **fields):
        while not self.stopping.is_set() and not token.event.is_set():
            try:
                return self.store.record_run(job_id, scheduled_at, status, detail, **fields)
            except sqlite3.OperationalError as exc:
                if not _storage_busy(exc):
                    self.logger.error("定时任务 %s 无法保存运行状态 %s：%s", job_id, status, type(exc).__name__)
                    raise
                self.logger.warning("定时任务 %s 的 %s 记录正在等待数据库写锁，稍后重试。", job_id, status)
                self.stopping.wait(CANCELLATION_POLL_SECONDS)
        return False

    def _execute(self, job: Dict[str, Any], scheduled_at: float) -> None:
        started = time.time()
        capability = job.get("capability", "video_summary")
        key = (job["id"], scheduled_at)
        with self.tokens_lock:
            token = self.tokens.get(key) or CancellationToken()
        safe_logger = SecretSafeLogger(self.logger, getattr(self.settings, "api_key", ""))
        stage = STARTING_STAGE
        try:
            token.check()
            definition = get_capability(capability)
            settings = self.settings_provider() if self.settings_provider else self.settings
            safe_logger = SecretSafeLogger(self.logger, getattr(settings, "api_key", ""))
            profile = None
            if self.settings_provider:
                settings = resolve_agent(settings, capability_settings_role(capability))
                safe_logger = SecretSafeLogger(self.logger, settings._profile_secrets)
                profile = {**execution_profile(settings), "started_at": started, "attempt": 1}
            else:
                safe_logger = SecretSafeLogger(self.logger, AgentProfiles(settings).secrets())
            token.check()
            if not self._record_run(token, job["id"], scheduled_at, "running", stage, started_at=started,
                                    execution_profile=profile):
                return
            safe_logger.info("开始执行定时任务 %s，功能：%s", job["id"], capability)
            parameters = job.get("parameters")
            # Callers supplying a pre-migration job dictionary remain usable.
            if "parameters" not in job and capability == "video_summary":
                parameters = {"url": job.get("url", ""), "include_frames": True}
            normalized = validate_parameters(capability, parameters)
            def progress(update):
                nonlocal stage
                token.check()
                stage = safe_logger.clean(update)
                if not self._record_run(token, job["id"], scheduled_at, "running", stage, started_at=started):
                    token.cancel()
                    token.check()
            progress.job_id=job["id"]
            progress.scheduled_at=scheduled_at
            if self.settings_provider:
                result = self.controlled_runner(capability, normalized, token, progress, settings, safe_logger)
            elif self.controlled_runner:
                result = self.controlled_runner(capability, normalized, token, progress)
            else:
                result = self.runner(capability, normalized)
            token.check()
            detail = safe_logger.clean("{}已完成：{}".format(definition.title, result.get("title", definition.title)))
            output_path = result.get("path")
            if not self._record_run(token, job["id"], scheduled_at, "completed", detail,
                                    output_path=output_path, started_at=started):
                return
            safe_logger.info("定时任务 %s 完成：%s", job["id"], output_path)
            notify_local("定时任务已完成", detail, safe_logger)
        except MaterialRequired as exc:
            detail = safe_logger.clean(exc)
            if self._record_run(token, job["id"], scheduled_at, "waiting_user", detail, started_at=started):
                safe_logger.info("定时任务 %s 等待论文材料", job["id"])
                notify_local("需要论文材料", detail[:180], safe_logger)
        except RunCancelled:
            with self.store._connect() as db:
                row = db.execute("SELECT status FROM job_runs WHERE job_id=? AND scheduled_at=? ORDER BY id DESC LIMIT 1", (job["id"], scheduled_at)).fetchone()
            if row and row["status"] in {"queued", "running"}:
                self.store.record_run(job["id"], scheduled_at, "interrupted" if self.stopping.is_set() else "cancelled", "服务已退出。" if self.stopping.is_set() else "用户已取消本次执行。", started_at=started)
        except Exception as exc:
            detail = failed_stage(stage) + "\n" + safe_logger.clean(exc)[:2000]
            if not self._record_run(token, job["id"], scheduled_at, "failed", detail, started_at=started):
                return
            safe_logger.exception("定时任务 %s 执行失败", job["id"])
            notify_local("定时任务执行失败", detail[:180], safe_logger)
        finally:
            with self.tokens_lock:
                self.tokens.pop(key, None)

    def tick(self, now: Optional[float] = None) -> None:
        # Use the time this poll observed. Writing a backlog of missed
        # occurrences must not consume the grace period of work already due.
        current = time.time() if now is None else now
        cutoff = current - RUN_GRACE_SECONDS
        jobs = self.store.get_due_jobs(current)
        jobs.sort(key=lambda job: float(job["due_at"]) < cutoff)
        missed_jobs = {}
        try:
            for job in jobs:
                job_id = job["id"]
                due = float(job["due_at"])
                while due <= current:
                    late = due < cutoff
                    missed_detail = None
                    if job["cadence"] == "daily":
                        local = time.localtime(due)
                        day = date(local.tm_year, local.tm_mon, local.tm_mday)
                        hour, minute = _parse_clock(job["daily_time"])
                        expected = (day.year, day.month, day.day, hour, minute)
                        if time.localtime(_local_epoch(day, hour, minute))[:5] != expected:
                            late = True
                            missed_detail = "{} {} 在本机时区不存在，已跳过本次执行。".format(day.isoformat(), job["daily_time"])
                    following = _next_daily_after(due, job["daily_time"]) if job["cadence"] == "daily" else None
                    claim = self.store.claim_due(job_id, due, following, late, missed_detail)
                    if claim == "stale":
                        break
                    if claim == "new":
                        if late:
                            missed_jobs[job_id] = missed_jobs.get(job_id, 0) + 1
                            self.logger.error("定时任务 %s 错过计划时间 %s，已跳过。", job_id, format_local_time(due))
                        else:
                            self._dispatch(job, due)
                    if following is None:
                        break
                    due = following
        finally:
            # Each claim commits independently, even if the rest of the poll fails.
            if missed_jobs:
                if len(missed_jobs) == 1:
                    job_id, count = next(iter(missed_jobs.items()))
                    message = "任务 {} 有 {} 次执行已跳过。".format(job_id, count)
                else:
                    message = "{} 个计划共有 {} 次执行已跳过，请查看运行记录。".format(
                        len(missed_jobs), sum(missed_jobs.values()))
                # A slow OS notification must not delay the next due check.
                threading.Thread(target=notify_local, args=("定时任务错过", message, self.logger),
                                 name="agent-schedule-notice", daemon=True).start()

    def run_forever(self) -> None:
        lease = SchedulerLease(self.settings.data_home / "scheduler.lock")
        if not lease.acquire():
            raise RuntimeError("已有本地调度服务运行，当前进程不重复启动。")
        self.logger.info("本地 Harness 调度服务已启动。")
        try:
            if not self.recover_until_ready(self.stopping, POLL_SECONDS):
                return
            next_tick = time.monotonic()
            while not self.stopping.is_set():
                due = time.monotonic() >= next_tick
                try:
                    self._signal_cancelled_runs()
                    if due:
                        self.tick()
                except sqlite3.OperationalError as exc:
                    if not _storage_busy(exc):
                        raise
                    self.logger.warning("调度轮询正在等待数据库锁，下一轮继续检查。")
                if due:
                    next_tick = time.monotonic() + POLL_SECONDS
                wait = max(0, next_tick - time.monotonic())
                with self.tokens_lock:
                    if self.tokens:
                        wait = min(wait, CANCELLATION_POLL_SECONDS)
                self.stopping.wait(wait)
        finally:
            try:
                self.close()
            finally:
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
    # A service target reports ESRCH when already absent. Booting out by plist
    # path instead can conflate that state with configuration-file errors.
    result = subprocess.run(
        ["launchctl", "bootout", domain + "/com.minimal-agent.local"],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, errno.ESRCH):
        raise RuntimeError("launchd 卸载失败（退出码 {}）：{}".format(
            result.returncode, result.stderr.strip() or "系统未提供详细原因。"))
    if plist_path.exists():
        plist_path.unlink()
