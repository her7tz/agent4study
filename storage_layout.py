"""Capability-owned output folders under the configured content library."""

import os
import re
import tempfile
import json
import hashlib
import sqlite3
import stat
from pathlib import Path

FOLDERS = {
    "knowledge_archives": "知识归档", "video_summary": "视频总结",
    "paper_summary": "论文总结", "web_research": "联网研究", "notes": "普通笔记",
    "study_report": "学习周报",
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


def publish_archive(path, text):
    """Publish one complete snapshot. Retrying may reuse it, never replace it."""
    path = Path(path)
    content = text.encode("utf-8")
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            pending = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(pending, path)
        except FileExistsError:
            # The previous run might have published just before exiting.
            # Never follow links or block reading a non-regular file.
            if path.is_symlink() or not path.is_file():
                raise ValueError("已有归档路径不是普通文件，已保留原内容。") from None
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as existing:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_size == len(content) and existing.read(len(content)+1) == content:
                    return
            raise ValueError("已有归档文档内容不同，已保留用户版本。请先移开已有文件，再重试。") from None
    finally:
        if pending:
            pending.unlink(missing_ok=True)


def output_aliases(data_home):
    path = Path(data_home) / "output-aliases.json"
    if path.is_symlink():
        raise ValueError("文档映射文件不使用符号链接。")
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("文档映射格式无效。")
    for source, target in value.items():
        if (not source or source.startswith(".") or "/" in source or not isinstance(target, str)
                or len(target.split("/")) != 2 or target.split("/")[1] != source
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", target.split("/")[0])):
            raise ValueError("文档映射包含无效路径，已停止迁移。")
    return value


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
    alias_path = settings.data_home / "output-aliases.json"
    with sqlite3.connect(settings.database_path, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        mapping = output_aliases(settings.data_home)
        had_map = alias_path.exists()
        plans = {}
        entries = {p.name:p for p in root.iterdir() if not p.name.startswith(".")}
        extensions = {".md", ".txt", ".html", ".zip"}
        stems = {p.stem for p in entries.values() if p.suffix.lower() in extensions - {".zip"}
                 and p.is_file() and not p.is_symlink()}
        # The map is persisted before moving any member. It may be the only
        # remaining evidence that a root-level resource folder belongs to a
        # document already moved to its category by the interrupted process.
        def stem_of(name):
            return name[:-7] if name.endswith("_assets") else Path(name).stem

        families = {}
        for name in entries.keys() | mapping.keys():
            if name.endswith("_assets") or Path(name).suffix.lower() in extensions:
                families.setdefault(stem_of(name), set()).add(name)
        for original, relative in mapping.items():
            if (root/relative).parent.is_symlink() or (root/relative).is_symlink():
                raise ValueError("早期文档映射目标不使用符号链接。")
            if original in entries:
                stems.add(stem_of(original))
        for stem in sorted(stems):
            family = sorted(families.get(stem, ()))
            categories = {mapping[name].split("/")[0] for name in family if name in mapping}
            for name in family:
                for row in db.execute("SELECT DISTINCT capability FROM ui_tasks WHERE output_file=?", (name,)):
                    categories.add(row[0])
            if len(categories) > 1:
                raise ValueError("同一组早期文档的分类冲突，已保留原文件。")
            fallback = ("video_summary" if stem.startswith(("BV", "av", "youtube_"))
                        else "web_research" if stem.startswith("web_research_") else "notes")
            directory = feature_directory(root, next(iter(categories), fallback))
            for name in family:
                source = entries.get(name)
                if source is None:
                    continue
                target = directory/name
                if (source.is_symlink() or target.is_symlink() or target.exists()
                        or (not source.is_dir() if name.endswith("_assets") else not source.is_file())
                        or (source.is_dir() and any(child.is_symlink() for child in source.rglob("*")))):
                    raise ValueError("早期文档迁移存在路径冲突，已保留原文件。")
                plans[source] = target
        moved = []
        previous = dict(mapping)
        try:
            for source, target in plans.items():
                mapping[source.name] = target.relative_to(root).as_posix()
            # Persist the migration plan first so an interrupted restart can resolve old URLs.
            if mapping != previous:
                atomic_text(alias_path, json.dumps(mapping, ensure_ascii=False))
            for source, target in plans.items():
                source.rename(target)
                moved.append((source, target))
            # Repair references after an interrupted migration with already-moved files.
            for original, relative in mapping.items():
                target = root / relative
                if target.exists():
                    db.execute("UPDATE ui_tasks SET output_file=? WHERE output_file=?", (relative, original))
                    db.execute("UPDATE job_runs SET output_path=? WHERE output_path IN (?,?)",
                               (str(target), str(root / original), str(settings.output_dir.absolute() / original)))
                    db.execute("UPDATE job_runs SET output_path=? WHERE output_path=?", (relative, original))
            db.commit()
        except Exception:
            db.rollback()
            for source, target in reversed(moved):
                target.rename(source)
            if mapping != previous:
                if had_map:
                    atomic_text(alias_path, json.dumps(previous, ensure_ascii=False))
                else:
                    alias_path.unlink(missing_ok=True)
            raise


def artifact_alias(artifact_id, data_home):
    for source, target in output_aliases(data_home).items():
        if hashlib.sha256(source.encode()).hexdigest()[:16] == artifact_id:
            return hashlib.sha256(target.encode()).hexdigest()[:16]
    return artifact_id
