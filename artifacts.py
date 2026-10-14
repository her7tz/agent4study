"""Automatically saved local notes with reversible deletion."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote


class ArtifactError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ArtifactStore:
    _lock = threading.RLock()

    def __init__(self, output_dir: Path, database_path: Path, data_home: Path):
        self.root = output_dir.resolve()
        self.database = database_path
        self.trash = data_home.resolve() / "trash" / "notes"
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, relative: str) -> Path:
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p.startswith(".") for p in parts) or "\\" in relative:
            raise ArtifactError(403, "内容路径不允许访问。")
        path = self.root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ArtifactError(403, "内容库不操作符号链接。")
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ArtifactError(403, "内容必须位于内容库目录内。") from exc
        return path

    def _metadata(self, path: Path) -> Dict[str, Any]:
        relative = path.relative_to(self.root).as_posix()
        with path.open(encoding="utf-8", errors="replace") as source:
            text = source.read(600)
        title = next((line.lstrip("#").strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        stat = path.stat()
        return {"id": hashlib.sha256(relative.encode()).hexdigest()[:16], "title": title,
                "name": relative, "url": "/outputs/" + quote(relative, safe="/"),
                "modified_at": stat.st_mtime, "size": stat.st_size}

    def save(self, title, markdown):
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ArtifactError(400, "笔记标题必须为 1 到 200 字。")
        if not isinstance(markdown, str) or not markdown.strip() or len(markdown) > 80000:
            raise ArtifactError(400, "笔记正文必须为 1 到 80000 字。")
        with self._lock:
            path = self.root / ("note_" + time.strftime("%Y%m%d-%H%M%S") + "_" + uuid.uuid4().hex[:8] + ".md")
            pending = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(self.root), prefix=".note-", delete=False) as stream:
                    pending = Path(stream.name)
                    stream.write("# " + title.strip().replace("\n", " ") + "\n\n" + markdown.strip() + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(pending), str(path))
            finally:
                if pending:
                    pending.unlink(missing_ok=True)
            return self._metadata(path)

    def list(self, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            candidates = {}
            priority = {".md": 0, ".txt": 1, ".html": 2}
            for path in self.root.rglob("*"):
                relative = path.relative_to(self.root).as_posix()
                if path.suffix.lower() not in priority or any(p.startswith(".") for p in path.relative_to(self.root).parts):
                    continue
                try:
                    path = self._safe_path(relative)
                    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                        continue
                    key = path.with_suffix("")
                    if key not in candidates or priority[path.suffix.lower()] < priority[candidates[key].suffix.lower()]:
                        candidates[key] = path
                except (OSError, ArtifactError):
                    continue
            results = []
            for path in candidates.values():
                try:
                    item = self._metadata(path)
                    if query.casefold() in (item["title"] + " " + item["name"]).casefold():
                        results.append(item)
                except OSError:
                    continue
            ordered = sorted(results, key=lambda item: item["modified_at"], reverse=True)
            return ordered if limit == 0 else ordered[:max(1, min(limit, 200))]

    def _find(self, artifact_id: str) -> Dict[str, Any]:
        item = next((item for item in self.list(limit=0) if item["id"] == artifact_id), None)
        if item is None:
            raise ArtifactError(404, "没有找到这份内容，请重新查询内容库。")
        return item

    def read(self, artifact_id: str, max_chars: int = 16000) -> Dict[str, Any]:
        with self._lock:
            item = self._find(artifact_id)
            content = self._safe_path(item["name"]).read_text(encoding="utf-8", errors="replace")
            return {**item, "content": content[:max_chars], "truncated": len(content) > max_chars}

    def _connect(self):
        db = sqlite3.connect(str(self.database), timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def delete(self, artifact_ids: List[str]) -> Dict[str, Any]:
        if not isinstance(artifact_ids, list) or not artifact_ids or len(artifact_ids) > 50 or not all(isinstance(i, str) for i in artifact_ids):
            raise ArtifactError(400, "请指定要删除的内容，单次最多 50 份。")
        with self._lock:
            notes = [self._find(i) for i in dict.fromkeys(artifact_ids)]
            plans = []
            for note in notes:
                source = self._safe_path(note["name"])
                paths = [source]
                for suffix in (".md", ".txt", ".html", ".zip"):
                    sibling = source.with_suffix(suffix)
                    if sibling != source and sibling.exists():
                        self._safe_path(sibling.relative_to(self.root).as_posix())
                        if not sibling.is_file():
                            raise ArtifactError(400, "导出副本不是普通文件。")
                        paths.append(sibling)
                assets = source.with_name(source.stem + "_assets")
                if assets.exists():
                    self._safe_path(assets.relative_to(self.root).as_posix())
                    for child in assets.rglob("*"):
                        self._safe_path(child.relative_to(self.root).as_posix())
                    paths.append(assets)
                plans.append((note, paths, self.trash / uuid.uuid4().hex))
            moved = []
            created = []
            try:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for note, paths, directory in plans:
                        directory.mkdir(parents=True, mode=0o700)
                        created.append(directory)
                        names = [p.relative_to(self.root).as_posix() for p in paths if p.is_file()]
                        task_rows, run_rows = [], []
                        for name in names:
                            if "ui_tasks" in tables:
                                task_rows.extend(dict(r) for r in db.execute("SELECT * FROM ui_tasks WHERE output_file=?", (name,)))
                                db.execute("UPDATE ui_tasks SET status='deleted',stage='已移入回收站',markdown=NULL,output_file=NULL WHERE output_file=?", (name,))
                            if "job_runs" in tables:
                                absolute = str(self.root / name)
                                run_rows.extend(dict(r) for r in db.execute("SELECT * FROM job_runs WHERE output_path IN (?,?)", (name, absolute)))
                                db.execute("UPDATE job_runs SET output_path=NULL WHERE output_path IN (?,?)", (name, absolute))
                        manifest = {"note": note, "paths": [p.relative_to(self.root).as_posix() for p in paths],
                                    "tasks": task_rows, "runs": run_rows, "deleted_at": time.time()}
                        for path in paths:
                            target = directory / "files" / path.relative_to(self.root)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(path), str(target))
                            moved.append((path, target))
                        manifest_path = directory / "manifest.json"
                        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                        manifest_path.chmod(0o600)
                return {"deleted": [{"id": note["id"], "title": note["title"], "trash_id": directory.name} for note, _, directory in plans],
                        "message": "内容已移入本地回收站，可以恢复。"}
            except Exception:
                for original, target in reversed(moved):
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(original))
                for directory in created:
                    shutil.rmtree(directory, ignore_errors=True)
                raise

    def restore(self, trash_ids: List[str]) -> Dict[str, Any]:
        if not isinstance(trash_ids, list) or not trash_ids or len(trash_ids) > 50:
            raise ArtifactError(400, "请指定要恢复的回收站内容。")
        with self._lock:
            manifests = []
            for trash_id in dict.fromkeys(trash_ids):
                if not isinstance(trash_id, str) or len(trash_id) != 32 or any(c not in "0123456789abcdef" for c in trash_id):
                    raise ArtifactError(400, "回收站内容编号无效。")
                directory = self.trash / trash_id
                if directory.is_symlink() or (directory / "manifest.json").is_symlink():
                    raise ArtifactError(403, "回收站目录无效。")
                try:
                    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ArtifactError(404, "找不到这份回收站内容。") from exc
                for relative in manifest["paths"]:
                    target = self._safe_path(relative)
                    source = directory / "files" / relative
                    try:
                        source.resolve().relative_to(directory.resolve())
                    except ValueError as exc:
                        raise ArtifactError(403, "回收站文件路径无效。") from exc
                    if target.exists():
                        raise ArtifactError(409, "原位置已有同名内容，恢复会覆盖文件，因此已停止。")
                    if not source.exists() or source.is_symlink():
                        raise ArtifactError(404, "回收站中的文件不完整。")
                manifests.append((directory, manifest))
            moved = []
            try:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for directory, manifest in manifests:
                        for relative in manifest["paths"]:
                            target = self._safe_path(relative)
                            source = directory / "files" / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(source), str(target))
                            moved.append((source, target))
                        for row in manifest["tasks"]:
                            db.execute("UPDATE ui_tasks SET status=?,stage=?,markdown=?,output_file=? WHERE id=? AND status='deleted'",
                                       (row["status"], row["stage"], row["markdown"], row["output_file"], row["id"]))
                        for row in manifest["runs"]:
                            db.execute("UPDATE job_runs SET output_path=? WHERE id=? AND output_path IS NULL", (row["output_path"], row["id"]))
            except Exception:
                for source, target in reversed(moved):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(source))
                raise
            for directory, _ in manifests:
                shutil.rmtree(directory, ignore_errors=True)
            return {"restored": [self._find(m["note"]["id"]) for _, m in manifests]}

    def list_trash(self) -> List[Dict[str, Any]]:
        results = []
        if self.trash.exists():
            for path in self.trash.glob("*/manifest.json"):
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    results.append({"trash_id": path.parent.name, "title": manifest["note"]["title"], "deleted_at": manifest["deleted_at"]})
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(results, key=lambda i: i["deleted_at"], reverse=True)
