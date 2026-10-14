"""Shared runtime services used by every business-specific Harness."""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from harness import DeepSeekClient
from agent_profiles import create_client, redact
from runtime_control import CancellationToken


class ControlledModel:
    def __init__(self, client, cancellation):
        self.client, self.cancellation = client, cancellation

    @property
    def settings(self):
        return self.client.settings

    @property
    def timeout(self):
        return self.client.timeout

    @timeout.setter
    def timeout(self, value):
        self.client.timeout = value

    @property
    def last_reasoning(self):
        return getattr(self.client, "last_reasoning", "")

    def stream_chat(self, *args, **kwargs):
        self.cancellation.check()
        kwargs["cancellation"] = self.cancellation
        return self.client.stream_chat(*args, **kwargs)

    def chat(self, *args, **kwargs):
        self.cancellation.check()
        kwargs["cancellation"] = self.cancellation
        return self.client.chat(*args, **kwargs)

    def json_chat(self, *args, **kwargs):
        self.cancellation.check()
        kwargs["cancellation"] = self.cancellation
        return self.client.json_chat(*args, **kwargs)


class SharedHarness:
    """Provider, cancellation, progress, trace, and filesystem primitives.

    The existing WebRuntime/Scheduler own queues and persistence. This object
    is the per-run view of those shared services, not a second scheduler.
    """
    def __init__(self, context, role):
        self.context, self.settings = context, context.settings
        self.role, self.run_id = role, uuid.uuid4().hex
        self.cancellation = context.cancellation or CancellationToken()
        self.model = ControlledModel(context.client or create_client(self.settings, timeout=180), self.cancellation)
        directory = self.settings.data_home / "subagent-runs"
        directory.mkdir(parents=True, exist_ok=True)
        self.trace_path = directory / (self.run_id + ".jsonl")
        self.trace("started", role=role)

    def trace(self, event, **fields):
        payload = {"at": time.time(), "run_id": self.run_id, "role": self.role, "event": event, **fields}
        text = redact(self.settings, json.dumps(payload, ensure_ascii=False))
        descriptor = os.open(str(self.trace_path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    def progress(self, stage):
        self.cancellation.check()
        self.trace("progress", stage=stage)
        if self.context.progress:
            self.context.progress(stage)

    def execute_tool(self, registry, name, arguments, context=None):
        self.cancellation.check()
        self.trace("tool_requested", name=name)
        result = registry.call(name, arguments, context)
        self.trace("tool_finished", name=name)
        return result

    def validate_output(self, result):
        self.cancellation.check()
        if not isinstance(result, dict) or not result.get("path"):
            raise RuntimeError("业务任务没有产生可验证的成果文件。")
        path = Path(result["path"]).expanduser().resolve()
        explicit = self.context.output_path
        if not explicit or path != Path(explicit).expanduser().resolve():
            try:
                path.relative_to(self.settings.output_dir.resolve())
            except ValueError as exc:
                raise RuntimeError("成果文件不在当前内容目录内。") from exc
        if not path.is_file() or not path.stat().st_size or path.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("成果文件不存在、为空或超过大小限制。")
        actual = path.read_text(encoding="utf-8")
        if result.get("markdown") != actual:
            raise RuntimeError("成果正文与保存文件不一致。")
        return {"path": str(path), "characters": len(actual), "verified": True}

    def save_markdown(self, title, markdown, prefix):
        self.cancellation.check()
        if not isinstance(markdown, str) or not markdown.strip():
            raise RuntimeError("业务任务没有生成正文。")
        directory = self.settings.output_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = self.context.output_path or directory / (prefix + "_" + time.strftime("%Y%m%d-%H%M%S") + "_" + self.run_id[:8] + ".md")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent), delete=False) as stream:
                pending = Path(stream.name)
                stream.write(markdown)
                stream.flush()
                os.fsync(stream.fileno())
            self.cancellation.check()
            os.replace(str(pending), str(path))
        finally:
            if pending:
                pending.unlink(missing_ok=True)
        return {"title": title, "markdown": markdown, "path": str(path)}
