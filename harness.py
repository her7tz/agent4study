#!/usr/bin/env python3
"""Reusable local runtime shared by agent capabilities."""

import importlib
import contextvars
from contextlib import contextmanager
import io
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_DATA_HOME = Path.home() / ".minimal-agent"
PROJECT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = PROJECT_DIR / "skills"
_SKILL_CONTEXT = contextvars.ContextVar("skill_snapshot", default={})

@contextmanager
def skill_snapshot(settings):
    role = getattr(settings, "_profile_role", None)
    snapshot={role:settings.skill_instructions} if role else {}
    if role and role not in {'global','conversation'}:
        from agent_registry import registry
        snapshot.update({name:settings.skill_instructions for name in registry.get(role).operations})
    marker = _SKILL_CONTEXT.set(snapshot)
    try:
        yield
    finally:
        _SKILL_CONTEXT.reset(marker)



def load_skill_instructions(capability: str) -> str:
    """Load capability-specific instructions from local, provider-neutral files.

    Call once at the beginning of a task so edits apply to the next task and
    all stages within the current task use the same instruction snapshot.
    """
    snapshot = _SKILL_CONTEXT.get()
    if capability in snapshot:
        return snapshot[capability]
    config_path = SKILLS_DIR / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法读取 skills/config.json，请检查 Skill 配置。") from exc
    if not isinstance(config, dict) or config.get("version") != 1:
        raise RuntimeError("Skill 配置必须是 version 为 1 的 JSON 对象。")
    capabilities = config.get("capabilities")
    if not isinstance(capabilities, dict):
        raise RuntimeError("Skill 配置的 capabilities 必须是对象。")
    names = capabilities.get(capability, [])
    if not isinstance(names, list):
        raise RuntimeError("每项能力的 Skill 配置必须是名称列表。")
    instructions: List[str] = []
    seen = set()
    root = SKILLS_DIR.resolve()
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise RuntimeError("Skill 名称只能使用小写字母、数字和连字符。")
        if name in seen:
            continue
        seen.add(name)
        path = (SKILLS_DIR / name / "SKILL.md").resolve()
        if root not in path.parents:
            raise RuntimeError("Skill 文件必须位于项目 skills 目录内。")
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("无法读取 Skill：" + name) from exc
        if not content:
            raise RuntimeError("Skill 内容为空：" + name)
        instructions.append("<skill name=\"{}\">\n{}\n</skill>".format(name, content))
    return "\n\n".join(instructions)


@lru_cache(maxsize=1)
def asr_runtime_status() -> Dict[str, Any]:
    """Check actual local audio decoding once per process, without downloading."""
    try:
        from faster_whisper.audio import decode_audio
    except ImportError:
        return {"ready": False, "error": "尚未安装本地语音转录依赖。"}
    try:
        audio = io.BytesIO()
        with wave.open(audio, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(bytes(32000))
        audio.seek(0)
        if decode_audio(audio).shape != (16000,):
            raise RuntimeError("音频样本数异常")
    except Exception:
        return {"ready": False, "error": "本地音频解码检查失败，请重新配置运行环境。"}
    return {"ready": True, "error": None}


def resolve_data_home() -> Path:
    """Read the local storage location without loading model credentials."""
    return Path(
        os.environ.get("MINIMAL_AGENT_HOME", str(DEFAULT_DATA_HOME))
    ).expanduser()


def resolve_tool(name: str) -> Optional[str]:
    """Locate a local executable without changing PATH or downloading anything.

    The running interpreter and project installations take precedence over the
    system PATH. FFmpeg may also come from the installed imageio-ffmpeg wheel;
    its resolver only inspects local executables.
    """
    if not name or Path(name).name != name:
        raise ValueError("Tool name must be a simple executable name.")
    folders = (
        Path(sys.executable).absolute().parent,
        PROJECT_DIR / ".venv" / "bin",
        PROJECT_DIR / ".local" / "tools",
    )
    for folder in folders:
        candidate = folder / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate.absolute())
    executable = shutil.which(name)
    if executable:
        return executable
    if name == "ffmpeg":
        try:
            package = importlib.import_module("imageio_ffmpeg")
            candidate = Path(package.get_ffmpeg_exe())
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return str(candidate.absolute())
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
    return None


class Settings:
    """Runtime configuration. Secrets are read from the process environment."""

    def __init__(self, model: Optional[str] = None):
        self.data_home = resolve_data_home()
        file_values: Dict[str, str] = {}
        env_file = self.data_home / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                clean = line.strip()
                if clean and not clean.startswith("#") and "=" in clean:
                    name, value = clean.split("=", 1)
                    file_values[name.strip()] = value.strip().strip('"').strip("'")
        self.api_key = os.environ.get(
            "DEEPSEEK_API_KEY", file_values.get("DEEPSEEK_API_KEY", "")
        ).strip()
        self.model = model or os.environ.get(
            "DEEPSEEK_MODEL", file_values.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        ).strip()
        self.base_url = DEFAULT_BASE_URL
        self.max_parallel_tasks=int(os.environ.get('MINIMAL_AGENT_MAX_PARALLEL','4'))
        if not 1<=self.max_parallel_tasks<=16:raise ValueError('MINIMAL_AGENT_MAX_PARALLEL 必须为 1 至 16。')
        self.output_dir = Path(
            os.environ.get("MINIMAL_AGENT_OUTPUT", str(Path.cwd() / "summaries"))
        ).expanduser()
        self.log_path = self.data_home / "agent.log"
        self.database_path = self.data_home / "harness.sqlite3"

    def prepare(self) -> None:
        self.data_home.mkdir(parents=True, exist_ok=True)
        from storage_layout import prepare_library
        prepare_library(getattr(self, "library_root", self.output_dir))
        self.output_dir.mkdir(parents=True, exist_ok=True)


def configure_logging(settings: Settings) -> logging.Logger:
    settings.prepare()
    logger = logging.getLogger("minimal_agent")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_path, maxBytes=2_000_000, backupCount=4, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


class DeepSeekClient:
    """One provider adapter; model choice is independent of the fixed API URL."""

    def __init__(self, settings: Settings, timeout: int = 180):
        self.settings = settings
        self.timeout = timeout

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        cancellation: Any = None,
    ) -> Dict[str, Any]:
        if not self.settings.api_key:
            raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY。")
        body: Dict[str, Any] = {
            "model": model or self.settings.model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if response_format:
            body["response_format"] = response_format
        request = urllib.request.Request(
            f"{self.settings.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            if cancellation:
                cancellation.check()
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                from runtime_control import interrupt_socket
                remove = cancellation.register(lambda: interrupt_socket(response)) if cancellation else lambda: None
                try:
                    payload = json.loads(response.read().decode("utf-8"))
                finally:
                    remove()
                if cancellation:
                    cancellation.check()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"DeepSeek API 返回 HTTP {exc.code}: {detail[:1200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 DeepSeek API：{exc.reason}") from exc
        try:
            if payload["choices"][0].get("finish_reason") == "length":
                raise RuntimeError("模型输出超过长度限制，内容未完整生成，请缩小材料或重试。")
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DeepSeek API 返回了无法识别的响应。") from exc

    def stream_chat(self, messages, tools=None, on_delta=None, cancellation=None):
        """Assemble SSE deltas, including fragmented function call arguments."""
        from runtime_control import interrupt_socket
        if not self.settings.api_key:
            raise RuntimeError("请先配置 API Key。")
        if cancellation:
            cancellation.check()
        body = {"model": self.settings.model, "messages": messages, "stream": True}
        if tools:
            body.update(tools=tools, tool_choice="auto")
        request = urllib.request.Request(self.settings.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.settings.api_key,
                     "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
        content, reasoning, calls = [], [], {}
        self.last_reasoning = ""
        finished = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                remove = cancellation.register(lambda: interrupt_socket(response)) if cancellation else lambda: None
                try:
                    for line in response:
                        if cancellation:
                            cancellation.check()
                        if not line.startswith(b"data:"):
                            continue
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            finished = True
                            break
                        payload = json.loads(data.decode("utf-8"))
                        if payload.get("error"):
                            raise RuntimeError("模型流式回复失败：" + str(payload["error"])[:800])
                        for choice in payload.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            if choice.get("finish_reason"):
                                reason = choice["finish_reason"]
                                if reason not in {"stop", "tool_calls"}:
                                    raise RuntimeError("模型回复未完成（" + str(reason) + "），请重试或缩小问题。")
                                finished = True
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                content.append(delta["content"])
                                if on_delta:
                                    on_delta(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning.append(delta["reasoning_content"])
                                self.last_reasoning = "".join(reasoning)
                            for call in delta.get("tool_calls") or []:
                                index = call.get("index", 0)
                                if not isinstance(index, int) or index < 0 or index >= 12:
                                    raise RuntimeError("工具调用数量超过限制。")
                                target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if call.get("id"):
                                    target["id"] += call["id"]
                                function = call.get("function") or {}
                                for key in ("name", "arguments"):
                                    target["function"][key] += function.get(key) or ""
                finally:
                    remove()
        except Exception as exc:
            if cancellation:
                cancellation.check()
            if isinstance(exc, urllib.error.HTTPError):
                detail = exc.read(1200).decode("utf-8", errors="replace")
                raise RuntimeError("DeepSeek API 返回 HTTP {}：{}".format(exc.code, detail)) from exc
            raise
        if cancellation:
            cancellation.check()
        if not finished:
            raise RuntimeError("模型连接提前断开，回复未完成。")
        result = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            result["reasoning_content"] = "".join(reasoning)
        if calls:
            result["tool_calls"] = [calls[index] for index in sorted(calls)]
        return result

    def json_chat(
        self, system: str, user_content: Any, model: Optional[str] = None, cancellation: Any = None
    ) -> Dict[str, Any]:
        reply = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            model=model,
            cancellation=cancellation,
        )
        content = reply.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("模型没有返回 JSON 内容。")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"模型返回的 JSON 无法解析：{exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError("模型返回的 JSON 顶层必须是对象。")
        return result


def notify_local(title: str, message: str, logger: Optional[logging.Logger] = None) -> None:
    """Show an OS notification when available; logging remains the durable record."""
    if logger:
        logger.info("本地通知：%s — %s", title, message)
    try:
        if platform.system() == "Darwin" and shutil.which("osascript"):
            safe_title = title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            safe_message = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'display notification "{}" with title "{}"'.format(
                        safe_message, safe_title
                    ),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        elif platform.system() == "Linux" and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError):
        if logger:
            logger.exception("显示本地通知失败")


class JobStore:
    """SQLite-backed scheduled work and execution history."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            # Serialize schema inspection and migrations across desktop/web/CLI
            # startups. ALTER TABLE must not race another initializer.
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    cadence TEXT NOT NULL CHECK(cadence IN ('once', 'daily')),
                    daily_time TEXT,
                    due_at REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    capability TEXT NOT NULL DEFAULT 'video_summary',
                    parameters TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    scheduled_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    output_path TEXT,
                    capability TEXT,
                    parameters TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )"""
            )

            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "capability" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN capability TEXT NOT NULL DEFAULT 'video_summary'")
            if "parameters" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN parameters TEXT NOT NULL DEFAULT '{}'")
            for row in db.execute("SELECT id,url,capability,parameters FROM jobs").fetchall():
                # Only fill legacy empty video parameters. Other registered
                # capabilities may intentionally have no inputs.
                if row["capability"] == "video_summary" and row["parameters"] in {"{}", "", None}:
                    parameters = json.dumps({"url": row["url"], "include_frames": True}, ensure_ascii=False)
                    db.execute("UPDATE jobs SET parameters = ? WHERE id = ?", (parameters, row["id"]))

            run_columns = {row["name"] for row in db.execute("PRAGMA table_info(job_runs)")}
            if "capability" not in run_columns:
                db.execute("ALTER TABLE job_runs ADD COLUMN capability TEXT")
            if "parameters" not in run_columns:
                db.execute("ALTER TABLE job_runs ADD COLUMN parameters TEXT")
            db.execute(
                """UPDATE job_runs SET
                    capability = COALESCE(capability, (SELECT capability FROM jobs WHERE jobs.id = job_runs.job_id), 'video_summary'),
                    parameters = COALESCE(parameters, (SELECT parameters FROM jobs WHERE jobs.id = job_runs.job_id), '{}')
                    WHERE capability IS NULL OR parameters IS NULL"""
            )

    def add(
        self,
        job_id: str,
        url: str,
        cadence: str,
        daily_time: Optional[str],
        due_at: float,
        *,
        capability: str = "video_summary",
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        import time

        values = parameters if parameters is not None else {"url": url, "include_frames": True}
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs(id,url,cadence,daily_time,due_at,enabled,created_at,capability,parameters) VALUES(?,?,?,?,?,1,?,?,?)",
                (job_id, url, cadence, daily_time, due_at, time.time(), capability, json.dumps(values, ensure_ascii=False, allow_nan=False)),
            )

    @staticmethod
    def _decode_parameters(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        try:
            parameters = json.loads(result.get("parameters") or "{}")
        except (TypeError, json.JSONDecodeError):
            # Let normal capability validation report the malformed task as a
            # failed run rather than prevent every other job from being read.
            parameters = None
        result["parameters"] = parameters
        return result

    def remove(self, job_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET enabled = 0 WHERE id = ? AND enabled = 1", (job_id,)
            )
            return cursor.rowcount > 0

    def get_jobs(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self._connect() as db:
            sql = "SELECT * FROM jobs"
            if enabled_only:
                sql += " WHERE enabled = 1"
            sql += " ORDER BY due_at"
            return [self._decode_parameters(row) for row in db.execute(sql).fetchall()]

    def get_due_jobs(self, now: float) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE enabled = 1 AND due_at <= ? ORDER BY due_at",
                (now,),
            ).fetchall()
            return [self._decode_parameters(row) for row in rows]

    def set_due(self, job_id: str, due_at: float, enabled: bool = True) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET due_at = ?, enabled = ? WHERE id = ?",
                (due_at, 1 if enabled else 0, job_id),
            )

    def record_run(
        self,
        job_id: str,
        scheduled_at: float,
        status: str,
        detail: str,
        output_path: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> None:
        import time

        with self._connect() as db:
            job = db.execute("SELECT capability,parameters FROM jobs WHERE id = ?", (job_id,)).fetchone()
            pending = db.execute("SELECT id,status FROM job_runs WHERE job_id=? AND scheduled_at=? ORDER BY id DESC LIMIT 1", (job_id, scheduled_at)).fetchone()
            if pending:
                if pending["status"] not in {"queued", "running"}:
                    return
                db.execute("UPDATE job_runs SET status=?,detail=?,output_path=?,started_at=COALESCE(?,started_at),finished_at=? WHERE id=?",
                           (status, detail, output_path, started_at, time.time(), pending["id"]))
                return
            db.execute(
                """INSERT INTO job_runs
                   (job_id,scheduled_at,started_at,finished_at,status,detail,output_path,capability,parameters)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    scheduled_at,
                    started_at,
                    time.time(),
                    status,
                    detail,
                    output_path,
                    job["capability"] if job else None,
                    job["parameters"] if job else None,
                ),
            )

    def recent_runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM job_runs ORDER BY finished_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._decode_parameters(row) for row in rows]
