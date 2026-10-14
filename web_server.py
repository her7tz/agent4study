#!/usr/bin/env python3
"""Local browser interface for the shared Agent Harness.

Only the loopback interface is exposed. UI work is persistent, while API keys
remain local configuration and are never included in an API response.
"""

import copy
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from concurrent.futures import Executor, Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from capabilities import execute_capability, get_capability, list_capabilities, validate_parameters
from conversation import ChatService, ConversationError
from artifacts import ArtifactStore, ArtifactError
from runtime_control import CancellationToken, RunCancelled
from harness import DeepSeekClient, JobStore, Settings, asr_runtime_status, notify_local, resolve_tool
from report_exports import ExportError, build_report_archive
from scheduler import (
    POLL_SECONDS, Scheduler, SchedulerLease,
    add_capability_daily_job, add_capability_once_job,
)
from video_summary import summarize_video, validate_video_url


MAX_BODY_BYTES = 64 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MODEL_NAMES = ["deepseek-flash", "deepseek-v4-pro"]
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
WEB_DIR = Path(__file__).resolve().parent / "web"


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class SerialDaemonExecutor(Executor):
    """One worker, with queued work cancelled when the local service closes."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._work, name="agent-web-tasks", daemon=True
        )
        self._thread.start()

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("本地服务正在关闭。")
            self._queue.put((future, fn, args, kwargs))
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._closed = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            self._queue.put(None)
        if wait:
            self._thread.join()


class SecretSafeLogger:
    """Avoid exposing secrets even if an upstream error includes credentials."""

    def __init__(self, logger: Any, secret: str):
        self.logger = logger
        self.secret = secret

    def clean(self, value: Any) -> str:
        text = str(value)
        return text.replace(self.secret, "[已隐藏]") if self.secret else text

    def _log(self, level: int, message: Any, *args: Any, **kwargs: Any) -> None:
        try:
            text = str(message) % args if args else str(message)
        except (TypeError, ValueError):
            text = str(message)
        kwargs.pop("exc_info", None)
        self.logger.log(level, self.clean(text), **kwargs)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, message, *args, **kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, message, *args, **kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, message, *args, **kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, message, *args, **kwargs)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self.error(message, *args, **kwargs)
        error = sys.exc_info()[1]
        if error is not None:
            # Format first, then redact; standard exc_info would bypass cleaning.
            self.error("失败堆栈：\n%s", "".join(traceback.format_exception(
                type(error), error, error.__traceback__
            )))


class UITaskStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS ui_tasks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    include_frames INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    error TEXT,
                    output_file TEXT,
                    sections INTEGER NOT NULL DEFAULT 0,
                    frames INTEGER NOT NULL DEFAULT 0,
                    markdown TEXT
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(ui_tasks)")}
            if "capability" not in columns:
                db.execute("ALTER TABLE ui_tasks ADD COLUMN capability TEXT NOT NULL DEFAULT 'video_summary'")
            if "parameters" not in columns:
                db.execute("ALTER TABLE ui_tasks ADD COLUMN parameters TEXT")
            for row in db.execute("SELECT id,url,include_frames FROM ui_tasks WHERE parameters IS NULL"):
                db.execute("UPDATE ui_tasks SET parameters=? WHERE id=?", (
                    json.dumps({"url": row["url"], "include_frames": bool(row["include_frames"])}, ensure_ascii=False),
                    row["id"],
                ))
            db.execute(
                """UPDATE ui_tasks SET status='interrupted', stage='已中断',
                   finished_at=?, error='服务关闭或重启时任务未完成；请重新提交。'
                   WHERE status IN ('queued','running')""",
                (time.time(),),
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.database_path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _task(row: sqlite3.Row, with_markdown: bool = False) -> Dict[str, Any]:
        result = dict(row)
        result["include_frames"] = bool(result["include_frames"])
        try:
            result["parameters"] = json.loads(result.get("parameters") or "{}")
        except (TypeError, json.JSONDecodeError):
            result["parameters"] = {}
        result["capability"] = result.get("capability") or "video_summary"
        if not with_markdown:
            result.pop("markdown", None)
        return result

    def create(
        self, url: str, frames: bool, model: str,
        capability: str = "video_summary", parameters: Optional[Dict[str, Any]] = None,
        title: str = "视频总结",
    ) -> Dict[str, Any]:
        task_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                """INSERT INTO ui_tasks
                   (id,url,title,status,stage,created_at,include_frames,model,capability,parameters)
                   VALUES(?,?,?,'queued','等待执行',?,?,?,?,?)""",
                (task_id, url, title, time.time(), int(frames), model, capability,
                 json.dumps(parameters if parameters is not None else {"url": url, "include_frames": frames}, ensure_ascii=False)),
            )
        return self.get(task_id) or {}

    def list(self) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM ui_tasks ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            return [self._task(row) for row in rows]

    def get(self, task_id: str, with_markdown: bool = False) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM ui_tasks WHERE id=?", (task_id,)).fetchone()
            return self._task(row, with_markdown) if row is not None else None

    def update(self, task_id: str, **values: Any) -> None:
        fields = {
            "title", "status", "stage", "finished_at", "error", "output_file",
            "sections", "frames", "markdown",
        }
        if not values or not set(values).issubset(fields):
            raise ValueError("任务更新字段无效。")
        sql = "UPDATE ui_tasks SET {} WHERE id=? AND status NOT IN ('interrupted','cancelled','deleted')".format(
            ",".join(name + "=?" for name in values)
        )
        with self._connect() as db:
            db.execute(sql, list(values.values()) + [task_id])

    def interrupt_pending(self) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE ui_tasks SET status='interrupted',stage='已中断',
                   finished_at=?,error='服务已关闭；请重新提交未完成的任务。'
                   WHERE status IN ('queued','running')""",
                (time.time(),),
            )


class WebRuntime:
    def __init__(self, settings: Settings, logger: Any):
        settings.prepare()
        self.settings = settings
        self.logger = logger
        self.settings_lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.store = JobStore(settings.database_path)
        self.tasks = UITaskStore(settings.database_path)
        self.executor = SerialDaemonExecutor()
        self.stop_event = threading.Event()
        self.task_tokens = {}
        self.task_tokens_lock = threading.Lock()
        self.scheduler = None
        self.chat = ChatService(settings.database_path, self.snapshot, self.create_capability_task, self.logger, task_provider=self.tasks.list, cancel_task=self.cancel_task, cancel_scheduled_run=self.cancel_scheduled_run)
        self.lease = SchedulerLease(settings.data_home / "scheduler.lock")
        self.scheduler_owned = self.lease.acquire()
        self.scheduler_thread: Optional[threading.Thread] = None
        if self.scheduler_owned:
            self.scheduler_thread = threading.Thread(
                target=self._schedule_loop, name="agent-web-scheduler", daemon=True
            )
            self.scheduler_thread.start()

    def snapshot(self) -> Settings:
        with self.settings_lock:
            return copy.copy(self.settings)

    def artifacts(self) -> ArtifactStore:
        settings = self.snapshot()
        return ArtifactStore(settings.output_dir, settings.database_path, settings.data_home)

    def status(self) -> Dict[str, Any]:
        settings = self.snapshot()
        asr = asr_runtime_status()
        return {
            "app_name": "本地 Agent",
            "api_key_configured": bool(settings.api_key),
            "provider": "deepseek",
            "model": settings.model,
            "base_url": settings.base_url,
            "models": MODEL_NAMES,
            "dependencies": {
                "yt_dlp": bool(resolve_tool("yt-dlp")),
                "ffmpeg": bool(resolve_tool("ffmpeg")),
                "asr": asr["ready"],
                "javascript_runtime": bool(resolve_tool("deno") or resolve_tool("node")),
            },
            "dependency_errors": {"asr": asr["error"]},
            "scheduler_running": not self.stop_event.is_set(),
            "scheduler_owner": "web" if self.scheduler_owned else "external",
            "output_dir": str(settings.output_dir.resolve()),
            "automatic_save": True,
            "timezone": "Asia/Shanghai",
            "csrf_token": self.token,
        }

    def configure(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not body or not set(body).issubset({"api_key", "model"}):
            raise APIError(400, "请提供 api_key 或 model 配置。")
        api_key = body.get("api_key")
        model = body.get("model")
        if api_key is not None:
            if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
                raise APIError(400, "API Key 不能为空，且必须是字符串。")
            if any(ord(char) < 32 or char in "\x85\u2028\u2029" for char in api_key) or any(char in api_key for char in "\"'"):
                raise APIError(400, "API Key 包含不支持的控制字符或引号。")
            api_key = api_key.strip()
        if model is not None:
            if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model):
                raise APIError(400, "模型名称格式无效。")
        with self.settings_lock:
            values = {
                "DEEPSEEK_API_KEY": api_key if api_key is not None else self.settings.api_key,
                "DEEPSEEK_MODEL": model if model is not None else self.settings.model,
            }
            env_file = self.settings.data_home / ".env"
            lines: List[str] = []
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    name = line.partition("=")[0].strip()
                    if name not in values:
                        lines.append(line)
            lines.extend(name + "=" + value for name, value in values.items())
            temporary: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(self.settings.data_home),
                    prefix=".env-", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    os.chmod(str(temporary), 0o600)
                    stream.write("\n".join(lines) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(temporary), str(env_file))
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            self.settings.api_key = values["DEEPSEEK_API_KEY"]
            self.settings.model = values["DEEPSEEK_MODEL"]
        self.logger.info("本地页面已更新模型配置。")
        return self.status()

    def create_summary(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(body.get("url"), str):
            raise APIError(400, "请输入 B 站或 YouTube 视频链接。")
        frames = body.get("include_frames", True)
        if not isinstance(frames, bool):
            raise APIError(400, "include_frames 必须是布尔值。")
        return self.create_capability_task({"capability": "video_summary", "parameters": {
            "url": body["url"], "include_frames": frames,
        }})

    def create_capability_task(self, body: Dict[str, Any]) -> Dict[str, Any]:
        capability = body.get("capability")
        parameters = body.get("parameters")
        if not isinstance(capability, str) or not isinstance(parameters, dict):
            raise APIError(400, "请选择功能并填写输入参数。")
        try:
            metadata = get_capability(capability)
            parameters = validate_parameters(capability, parameters)
        except ValueError as exc:
            raise APIError(400, str(exc)) from exc
        settings = self.snapshot()
        if not settings.api_key:
            raise APIError(400, "请先在设置中保存 DeepSeek API Key。")
        if capability == "video_summary" and not resolve_tool("yt-dlp"):
            raise APIError(400, "尚未安装视频读取工具 yt-dlp，请按使用说明安装。")
        title = metadata.title if hasattr(metadata, "title") else metadata.get("title", capability)
        task = self.tasks.create(
            str(parameters.get("url", "")), bool(parameters.get("include_frames", False)),
            settings.model, capability=capability, parameters=parameters, title=title,
        )
        with self.task_tokens_lock:
            self.task_tokens[task["id"]] = CancellationToken()
        try:
            self.executor.submit(self._run_capability, task["id"], settings)
        except RuntimeError as exc:
            with self.task_tokens_lock:
                self.task_tokens.pop(task["id"], None)
            self.tasks.update(
                task["id"], status="failed", stage="提交失败",
                error=str(exc), finished_at=time.time(),
            )
            raise APIError(503, str(exc)) from exc
        return task

    def cancel_task(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            raise APIError(404, "没有找到该任务。")
        if task["status"] not in {"queued", "running"}:
            return {"task_id": task_id, "status": task["status"], "message": "任务已经结束。"}
        with self.task_tokens_lock:
            token = self.task_tokens.get(task_id)
        if token:
            token.cancel()
        self.tasks.update(task_id, status="cancelled", stage="已取消", finished_at=time.time(), error=None)
        self.logger.info("用户已取消后台任务 %s", task_id)
        return {"task_id": task_id, "status": "cancelled"}

    def _run_summary(self, task_id: str, settings: Settings) -> None:
        """Compatibility entry point for callers of the previous video worker."""
        self._run_capability(task_id, settings)

    def _run_capability(self, task_id: str, settings: Settings) -> None:
        if self.stop_event.is_set():
            return
        task = self.tasks.get(task_id)
        if task is None or task["status"] != "queued":
            with self.task_tokens_lock:
                self.task_tokens.pop(task_id, None)
            return
        with self.task_tokens_lock:
            token = self.task_tokens.get(task_id) or CancellationToken()
        safe_logger = SecretSafeLogger(self.logger, settings.api_key)
        capability = task.get("capability") or "video_summary"
        self.tasks.update(task_id, status="running", stage="开始执行" + task["title"])

        def progress(stage: str) -> None:
            token.check()
            if not self.stop_event.is_set():
                self.tasks.update(task_id, stage=stage)

        try:
            result = execute_capability(capability, task["parameters"], settings, logger=safe_logger, progress=progress, cancellation=token)
            relative = None
            if result.get("path"):
                output = Path(result["path"]).resolve()
                relative = output.relative_to(settings.output_dir.resolve()).as_posix()
                if not output.is_file():
                    raise RuntimeError("任务没有生成可读取的结果文件。")
            if self.stop_event.is_set():
                return
            token.check()
            title = str(result.get("title") or task["title"])
            self.tasks.update(
                task_id, title=title, status="completed", stage="已完成",
                finished_at=time.time(), output_file=relative,
                sections=int(result.get("sections", 0)),
                frames=int(result.get("frames", 0)), markdown=result.get("markdown", result.get("text")),
                error=None,
            )
            if (self.tasks.get(task_id) or {}).get("status") == "completed":
                notify_local("任务已完成", title[:180], safe_logger)
        except RunCancelled:
            self.tasks.update(task_id, status="cancelled", stage="已取消", finished_at=time.time(), error=None)
        except Exception as exc:
            message = safe_logger.clean(exc)[:2000]
            failed_stage = (self.tasks.get(task_id) or {}).get("stage", "执行中")
            self.tasks.update(
                task_id, status="failed", stage="失败于：" + failed_stage, error=message,
                finished_at=time.time(),
            )
            safe_logger.exception("页面任务 %s 执行失败（%s）：%s", task_id, failed_stage, message)
            if not self.stop_event.is_set():
                notify_local("任务执行失败", message[:180], safe_logger)
        finally:
            with self.task_tokens_lock:
                self.task_tokens.pop(task_id, None)

    def _schedule_loop(self) -> None:
        def runner(capability: str, parameters: Dict[str, Any], cancellation=None, progress=None) -> Dict[str, Any]:
            settings = self.snapshot()
            safe_logger = SecretSafeLogger(self.logger, settings.api_key)
            try:
                return execute_capability(capability, parameters, settings, logger=safe_logger, cancellation=cancellation, progress=progress)
            except RunCancelled:
                raise
            except Exception as exc:
                # Scheduler records the exception in its own persistent history.
                safe_logger.exception("定时功能 %s 执行失败", capability)
                raise RuntimeError(safe_logger.clean(exc)) from None

        scheduler = Scheduler(self.store, self.settings, runner, self.logger, controlled_runner=runner)
        self.scheduler = scheduler
        scheduler.recover_pending()
        try:
            while not self.stop_event.is_set():
                try:
                    scheduler.tick()
                except Exception as exc:
                    safe_logger = SecretSafeLogger(self.logger, self.snapshot().api_key)
                    safe_logger.error("调度轮询失败：%s", exc)
                self.stop_event.wait(POLL_SECONDS)
        finally:
            scheduler.close()

    def cancel_scheduled_run(self, run_id):
        if self.scheduler is None:
            raise APIError(409, "定时任务由其他服务管理。")
        try:
            return self.scheduler.cancel_run(run_id)
        except ValueError as exc:
            raise APIError(400, str(exc)) from exc

    def schedules(self) -> Dict[str, Any]:
        """Expose readable metadata while keeping scheduling independent of inputs."""
        metadata = {item["id"]: item for item in list_capabilities()}
        jobs = self.store.get_jobs()
        by_id = {item["id"]: item for item in jobs}
        for job in jobs:
            capability = job.get("capability") or "video_summary"
            job["capability"] = capability
            job["capability_title"] = metadata.get(capability, {}).get("title", capability)
        runs = self.store.recent_runs()
        for run in runs:
            job = by_id.get(run["job_id"], {})
            capability = run.get("capability") or job.get("capability") or "video_summary"
            run["capability"] = capability
            run["capability_title"] = metadata.get(capability, {}).get("title", capability)
            run.setdefault("parameters", job.get("parameters", {}))
        return {"jobs": jobs, "runs": runs}

    def create_schedule(self, body: Dict[str, Any]) -> str:
        if not isinstance(body.get("time"), str):
            raise APIError(400, "请选择执行时间。")
        if not set(body).issubset({"capability", "parameters", "url", "include_frames", "cadence", "time"}):
            raise APIError(400, "定时任务包含不支持的字段。")
        # Older pages and clients submitted a video URL at the top level.
        capability = body.get("capability", "video_summary")
        if not isinstance(capability, str) or not capability:
            raise APIError(400, "请选择需要执行的功能。")
        parameters = body.get("parameters")
        if parameters is None and "url" in body and capability == "video_summary":
            parameters = {"url": body["url"], "include_frames": body.get("include_frames", True)}
        if not isinstance(parameters, dict):
            raise APIError(400, "请填写所选功能的输入参数。")
        if "parameters" in body and ("url" in body or "include_frames" in body):
            raise APIError(400, "请使用功能参数填写输入，不要同时提交旧版视频字段。")
        try:
            # Validate registration before accepting any new persisted job.
            get_capability(capability)
            cadence = body.get("cadence")
            if cadence == "daily":
                return add_capability_daily_job(self.store, capability, parameters, body["time"])
            if cadence == "once":
                return add_capability_once_job(self.store, capability, parameters, body["time"].replace("T", " "))
            raise APIError(400, "任务频率必须是 once 或 daily。")
        except ValueError as exc:
            raise APIError(400, str(exc)) from exc

    def reports(self) -> List[Dict[str, Any]]:
        root = self.snapshot().output_dir.resolve()
        reports: List[Dict[str, Any]] = []
        for path in root.rglob("*.md"):
            try:
                resolved = path.resolve()
                name = resolved.relative_to(root).as_posix()
                if not resolved.is_file():
                    continue
                stat = resolved.stat()
                with resolved.open("r", encoding="utf-8") as stream:
                    heading = next(
                        (line.strip().lstrip("#").strip() for line in stream
                         if line.startswith("# ")), path.stem,
                    )
                reports.append({
                    "name": name, "title": heading,
                    "modified_at": stat.st_mtime, "size": stat.st_size,
                    "url": "/outputs/" + urllib.parse.quote(name, safe="/"),
                })
            except (OSError, UnicodeError, ValueError):
                continue
        return sorted(reports, key=lambda item: item["modified_at"], reverse=True)

    def close(self) -> None:
        self.stop_event.set()
        self.chat.close()
        with self.task_tokens_lock:
            tokens = list(self.task_tokens.values())
        for token in tokens:
            token.cancel()
        self.tasks.interrupt_pending()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.scheduler_thread is not None and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=1)
        # A running media operation remains a daemon worker until process exit.
        # Hold the process lease while it is still alive to prevent duplicate runs.
        if self.scheduler_thread is None or not self.scheduler_thread.is_alive():
            self.lease.release()


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], runtime: WebRuntime):
        self.runtime = runtime
        super().__init__(address, AgentRequestHandler)


class AgentRequestHandler(BaseHTTPRequestHandler):
    server: LocalHTTPServer
    protocol_version = "HTTP/1.1"

    @property
    def runtime(self) -> WebRuntime:
        return self.server.runtime

    def log_message(self, format: str, *args: Any) -> None:
        # Do not record user URLs, request bodies, tokens or key values.
        return

    def _check_source(self, mutation: bool = False) -> None:
        port = self.server.server_port
        hosts = {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        host_headers = self.headers.get_all("Host", [])
        if len(host_headers) != 1 or host_headers[0].lower() not in hosts:
            raise APIError(403, "只允许从本机地址访问此服务。")
        origins = {"http://" + host for host in hosts}
        origin_headers = self.headers.get_all("Origin", [])
        if len(origin_headers) > 1 or (origin_headers and origin_headers[0] not in origins):
            raise APIError(403, "此服务只接受本地页面的请求。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError(403, "已拒绝跨站请求。")
        if mutation:
            tokens = self.headers.get_all("X-Agent-Token", [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.runtime.token):
                raise APIError(403, "页面会话已失效，请刷新页面后重试。")

    def _json_body(self) -> Dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "不支持分块请求，请发送普通 JSON。")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "请求内容必须是 JSON。")
        length_headers = self.headers.get_all("Content-Length", [])
        if len(length_headers) != 1:
            raise APIError(400, "请求缺少有效的 Content-Length。")
        try:
            size = int(length_headers[0])
        except ValueError as exc:
            raise APIError(400, "请求长度无效。") from exc
        if size < 0 or size > MAX_BODY_BYTES:
            raise APIError(413, "请求过大，最多允许 64 KB。")
        try:
            value = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(400, "请求不是有效的 JSON。") from exc
        if not isinstance(value, dict):
            raise APIError(400, "JSON 内容必须是对象。")
        return value

    def _headers(
        self, status: int, content_type: str, size: int,
        content_disposition: Optional[str] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if content_disposition is not None:
            self.send_header("Content-Disposition", content_disposition)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        ))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _json(self, status: int, value: Dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _file(self, root: Path, relative: str, output: bool = False) -> None:
        root = root.resolve()
        try:
            if "\x00" in relative or "\\" in relative:
                raise ValueError("非法路径")
            path = (root / relative).resolve()
            path.relative_to(root)
        except (ValueError, OSError) as exc:
            raise APIError(403, "此文件路径不允许访问。") from exc
        allowed = {".md", ".txt", ".html", ".png", ".jpg", ".jpeg", ".webp", ".gif"} if output else {
            ".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico",
        }
        if path.suffix.lower() not in allowed or not path.is_file():
            raise APIError(404, "没有找到此文件。")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise APIError(413, "文件过大，无法在页面中读取。")
        data = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix.lower() == ".md" or (output and path.suffix.lower() in {".txt", ".html"}):
            content_type = "text/plain; charset=utf-8"
        elif content_type.startswith("text/") or path.suffix.lower() == ".js":
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(data))
        if self.command != "HEAD":
            self.wfile.write(data)

    def _export(self, query: Dict[str, List[str]]) -> None:
        files = query.get("file", [])
        if len(files) != 1 or not files[0]:
            raise APIError(400, "请选择需要下载的总结文件。")
        try:
            with build_report_archive(self.runtime.snapshot().output_dir, files[0]) as archive:
                disposition = "attachment; filename=\"video-summary.zip\"; filename*=UTF-8''" + urllib.parse.quote(
                    archive.filename, safe=""
                )
                self._headers(200, "application/zip", archive.size, disposition)
                if self.command != "HEAD":
                    while True:
                        chunk = archive.stream.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except ExportError as exc:
            raise APIError(exc.status, str(exc)) from exc

    def _get(self, path: str, query: Optional[Dict[str, List[str]]] = None) -> None:
        if path == "/api/status":
            self._json(200, self.runtime.status())
        elif path == "/api/capabilities":
            self._json(200, {"capabilities": list_capabilities()})
        elif path == "/api/artifacts":
            self._json(200, {"artifacts": self.runtime.artifacts().list((query or {}).get("query", [""])[0]), "directory": str(self.runtime.snapshot().output_dir.resolve())})
        elif path == "/api/artifacts/trash":
            self._json(200, {"artifacts": self.runtime.artifacts().list_trash()})
        elif path.startswith("/api/artifacts/"):
            self._json(200, {"artifact": self.runtime.artifacts().read(path[len("/api/artifacts/"):])})
        elif path == "/api/conversations/current":
            self._json(200, self.runtime.chat.current())
        elif path == "/api/conversations":
            self._json(200, {"conversations": self.runtime.chat.list()})
        elif path.startswith("/api/conversations/"):
            self._json(200, self.runtime.chat._payload(path[len("/api/conversations/"):]))
        elif path.startswith("/api/chat/runs/"):
            after = (query or {}).get("after", ["0"])[0]
            if not after.isdigit():
                raise APIError(400, "事件序号无效。")
            self._json(200, self.runtime.chat.run(path[len("/api/chat/runs/"):], int(after)))
        elif path == "/api/tasks":
            self._json(200, {"tasks": self.runtime.tasks.list()})
        elif path.startswith("/api/tasks/"):
            task = self.runtime.tasks.get(path[len("/api/tasks/"):], with_markdown=True)
            if task is None:
                raise APIError(404, "没有找到该任务。")
            self._json(200, {"task": task})
        elif path == "/api/reports":
            self._json(200, {"reports": self.runtime.reports()})
        elif path == "/api/export":
            self._export(query or {})
        elif path == "/api/schedules":
            self._json(200, self.runtime.schedules())
        elif path.startswith("/outputs/"):
            self._file(self.runtime.snapshot().output_dir, path[len("/outputs/"):], output=True)
        elif path.startswith("/api/"):
            raise APIError(404, "没有找到该接口。")
        else:
            self._file(WEB_DIR, "index.html" if path == "/" else path.lstrip("/"))

    def _post(self, path: str, body: Dict[str, Any]) -> None:
        if path == "/api/config":
            self._json(200, self.runtime.configure(body))
        elif path == "/api/chat":
            if not set(body).issubset({"message", "conversation_id"}):
                raise APIError(400, "对话请求包含不支持的字段。")
            self._json(200, self.runtime.chat.send(body.get("message"), body.get("conversation_id")))
        elif path == "/api/chat/start":
            if not set(body).issubset({"message", "conversation_id", "retry", "web_enabled"}):
                raise APIError(400, "对话请求包含不支持的字段。")
            self._json(202, self.runtime.chat.start(body.get("message"), body.get("conversation_id"), body.get("retry", False), body.get("web_enabled", True)))
        elif path.startswith("/api/chat/runs/") and path.endswith("/stop"):
            self._json(200, self.runtime.chat.stop(path[len("/api/chat/runs/"):-len("/stop")]))
        elif path.startswith("/api/conversations/") and path.endswith("/select"):
            self._json(200, self.runtime.chat.select(path[len("/api/conversations/"):-len("/select")]))
        elif path.startswith("/api/conversations/") and path.endswith("/rename"):
            self._json(200, self.runtime.chat.rename(path[len("/api/conversations/"):-len("/rename")], body.get("title")))
        elif path == "/api/conversations/new":
            if body:
                raise APIError(400, "新对话请求不需要输入参数。")
            self._json(201, self.runtime.chat.new())
        elif path == "/api/artifacts/delete":
            self._json(200, self.runtime.artifacts().delete(body.get("artifact_ids")))
        elif path == "/api/artifacts/restore":
            self._json(200, self.runtime.artifacts().restore(body.get("trash_ids")))
        elif path == "/api/summarize":
            self._json(202, {"task": self.runtime.create_summary(body)})
        elif path == "/api/tasks":
            self._json(202, {"task": self.runtime.create_capability_task(body)})
        elif path.startswith("/api/tasks/") and path.endswith("/cancel"):
            self._json(200, self.runtime.cancel_task(path[len("/api/tasks/"):-len("/cancel")]))
        elif path == "/api/schedules":
            self._json(201, {"job_id": self.runtime.create_schedule(body)})
        elif path.startswith("/api/schedule-runs/") and path.endswith("/cancel"):
            self._json(200, self.runtime.cancel_scheduled_run(path[len("/api/schedule-runs/"):-len("/cancel")]))
        else:
            raise APIError(404, "没有找到该接口。")

    def _dispatch(self) -> None:
        try:
            self._check_source(mutation=self.command in {"POST", "DELETE"})
            request_url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(request_url.path)
            if self.command in {"GET", "HEAD"}:
                self._get(path, urllib.parse.parse_qs(request_url.query, keep_blank_values=True))
            elif self.command == "POST":
                self._post(path, self._json_body())
            elif self.command == "DELETE":
                # DELETE also carries JSON to keep all mutating requests uniform.
                self._json_body()
                if path.startswith("/api/conversations/"):
                    self._json(200, self.runtime.chat.delete(path[len("/api/conversations/"):]))
                    return
                if not path.startswith("/api/schedules/"):
                    raise APIError(404, "没有找到该接口。")
                job_id = path[len("/api/schedules/"):]
                if not self.runtime.store.remove(job_id):
                    raise APIError(404, "没有找到可删除的定时任务。")
                self._json(200, {"success": True})
        except APIError as exc:
            self._json(exc.status, {"error": str(exc)})
        except ConversationError as exc:
            self._json(exc.status, {"error": str(exc)})
        except ArtifactError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            safe_logger = SecretSafeLogger(self.runtime.logger, self.runtime.snapshot().api_key)
            safe_logger.error("本地页面请求失败：%s", exc)
            self._json(500, {"error": "本地服务处理请求失败，请查看日志后重试。"})

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()


def run_web(
    settings: Settings, logger: Any, port: int = 8765, open_browser: bool = False
) -> int:
    """Run the browser UI until interrupted; never expose a LAN listener."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        logger.error("页面端口必须在 1 到 65535 之间。")
        return 2
    runtime: Optional[WebRuntime] = None
    server: Optional[LocalHTTPServer] = None
    settings.prepare()
    web_lease = SchedulerLease(settings.data_home / "web.lock")
    if not web_lease.acquire():
        logger.error("本地页面服务已在运行，请打开现有页面。")
        return 1
    try:
        runtime = WebRuntime(settings, logger)
        server = LocalHTTPServer(("127.0.0.1", port), runtime)
        logger.info("本地 Agent 页面已启动：http://127.0.0.1:%s", server.server_port)
        if open_browser:
            threading.Thread(
                target=webbrowser.open,
                args=("http://127.0.0.1:{}".format(server.server_port),),
                name="agent-open-browser", daemon=True,
            ).start()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("本地页面服务已关闭。")
    except OSError as exc:
        logger.error("无法启动本地页面服务：%s", exc)
        return 1
    finally:
        if server is not None:
            server.server_close()
        if runtime is not None:
            runtime.close()
        web_lease.release()
    return 0
