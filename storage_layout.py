"""Capability-owned output folders under the configured content library."""

import os
import re
import tempfile
import json
import hashlib
import sqlite3
from pathlib import Path

FOLDERS = {
    "knowledge_archives": "知识归档", "video_summary": "视频总结",
    "paper_summary": "论文总结", "web_research": "联网研究", "notes": "普通笔记",
}


def feature_directory(root, feature):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", feature):
        raise ValueError("无效的功能目录。")
    root = Path(root).resolve()
    path = root / feature
    if path.is_symlink():
        raise ValueError("功能目录不使用符号链接。")
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_library(root):
    for feature in FOLDERS:
        feature_directory(root, feature)


def atomic_text(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("文档不使用符号链接。")
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent), delete=False) as stream:
            pending = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending:
            pending.unlink(missing_ok=True)


def output_aliases(data_home):
    path = Path(data_home) / "output-aliases.json"
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("文档映射文件不使用符号链接。")
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def resolve_output_alias(relative, data_home):
    aliases = output_aliases(data_home)
    if relative in aliases:
        return aliases[relative]
    for source, target in aliases.items():
        if relative.startswith(source + "/"):
            return target + relative[len(source):]
    return relative


def migrate_legacy(settings):
    """Move root documents and companion assets together, retaining old links."""
    root = settings.output_dir.resolve()
    mapping = output_aliases(settings.data_home)
    plans = {}
    with sqlite3.connect(settings.database_path) as db:
        for path in root.iterdir():
            if path.name.startswith(".") or path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".html"}:
                continue
            row = db.execute("SELECT capability FROM ui_tasks WHERE output_file=? LIMIT 1", (path.name,)).fetchone()
            category = row[0] if row else ("video_summary" if path.name.startswith(("BV", "av", "youtube_")) else "web_research" if path.name.startswith("web_research_") else "notes")
            directory = feature_directory(root, category)
            family = [p for p in (path.with_suffix(ext) for ext in (".md", ".txt", ".html", ".zip")) if p.is_file()]
            assets = path.with_name(path.stem + "_assets")
            if assets.exists():
                family.append(assets)
            for source in family:
                target = directory / source.name
                if source.is_symlink() or (source.is_dir() and any(child.is_symlink() for child in source.rglob("*"))) or target.exists():
                    raise ValueError("早期文档迁移存在路径冲突，已保留原文件。")
                plans[source] = target
        moved = []
        previous = dict(mapping)
        try:
            for source, target in plans.items():
                mapping[source.name] = target.relative_to(root).as_posix()
            # Persist the migration plan first so an interrupted restart can resolve old URLs.
            atomic_text(settings.data_home / "output-aliases.json", json.dumps(mapping, ensure_ascii=False))
            for source, target in plans.items():
                source.rename(target)
                moved.append((source, target))
                db.execute("UPDATE ui_tasks SET output_file=? WHERE output_file=?", (mapping[source.name], source.name))
                db.execute("UPDATE job_runs SET output_path=? WHERE output_path=?", (str(target), str(source)))
                db.execute("UPDATE job_runs SET output_path=? WHERE output_path=?", (mapping[source.name], source.name))
            # Repair references after an interrupted migration with already-moved files.
            for original, relative in mapping.items():
                target = root / relative
                if target.exists():
                    db.execute("UPDATE ui_tasks SET output_file=? WHERE output_file=?", (relative, original))
                    db.execute("UPDATE job_runs SET output_path=? WHERE output_path=?", (str(target), str(root / original)))
                    db.execute("UPDATE job_runs SET output_path=? WHERE output_path=?", (relative, original))
        except Exception:
            for source, target in reversed(moved):
                target.rename(source)
            atomic_text(settings.data_home / "output-aliases.json", json.dumps(previous, ensure_ascii=False))
            raise


def artifact_alias(artifact_id, data_home):
    for source, target in output_aliases(data_home).items():
        if hashlib.sha256(source.encode()).hexdigest()[:16] == artifact_id:
            return hashlib.sha256(target.encode()).hexdigest()[:16]
    return artifact_id
