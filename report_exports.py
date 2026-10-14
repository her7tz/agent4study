"""Export a report together with its locally referenced images.

Archive paths mirror the output directory so relative Markdown image links
continue to work after extraction, including reports stored in subdirectories.
"""

import html
import os
import posixpath
import re
import stat
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Dict, Iterator, Optional


MAX_EXPORT_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_EXPORT_FILES = 256
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class ExportError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class ReportArchive:
    stream: BinaryIO
    filename: str
    size: int

    def __enter__(self) -> "ReportArchive":
        return self

    def __exit__(self, *_args: object) -> None:
        self.stream.close()


def _relative_path(value: str) -> str:
    if (
        not value or value.startswith("/") or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ExportError(403, "此文件路径不允许导出。")
    normalized = posixpath.normpath(value)
    parts = PurePosixPath(normalized).parts
    if not parts or any(part.startswith(".") for part in parts):
        raise ExportError(403, "导出文件必须位于总结目录内，且不能是隐藏文件。")
    return normalized


def _read_file(root_fd: int, relative: str) -> bytes:
    """Open every component without following symlinks, then read a regular file."""
    parts = PurePosixPath(relative).parts
    directory_fd = os.dup(root_fd)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        with os.fdopen(file_fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ExportError(403, "导出只允许普通文件。")
            if info.st_size > MAX_EXPORT_FILE_BYTES:
                raise ExportError(413, "导出中的单个文件不能超过 32 MB。")
            contents = source.read(MAX_EXPORT_FILE_BYTES + 1)
            if len(contents) > MAX_EXPORT_FILE_BYTES:
                raise ExportError(413, "导出中的单个文件不能超过 32 MB。")
            return contents
    except FileNotFoundError as exc:
        raise ExportError(404, "报告或引用的图片已不存在，无法导出完整文档。") from exc
    except OSError as exc:
        raise ExportError(403, "无法读取导出文件；请确认文件存在且没有符号链接。") from exc
    finally:
        os.close(directory_fd)


def _destination(text: str, start: int) -> Optional[str]:
    """Read a Markdown link destination, preserving balanced parentheses."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text):
        return None
    if text[start] == "<":
        end = start + 1
        while end < len(text):
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == ">":
                return text[start + 1:end]
            if text[end] in "\r\n":
                return None
            end += 1
        return None
    end = start
    depth = 0
    while end < len(text):
        char = text[end]
        if char == "\\":
            end += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char.isspace() and depth == 0:
            break
        end += 1
    return text[start:end] or None


def _label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _bracket_end(text: str, start: int) -> Optional[int]:
    """Locate a label's closing bracket, allowing nested and escaped brackets."""
    depth = 1
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _image_destinations(markdown: str) -> Iterator[str]:
    # Code examples are not image references in the rendered report.
    text = re.sub(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[ \t]*$", "", markdown)
    text = re.sub(r"(`+)[^`]*?\1", "", text)
    references: Dict[str, str] = {}
    for match in re.finditer(r"(?m)^ {0,3}\[([^\]\r\n]+)\]:[ \t]*", text):
        destination = _destination(text, match.end())
        if destination:
            references.setdefault(_label(match.group(1)), destination)
    for match in re.finditer(r"!\[", text):
        closing = _bracket_end(text, match.end() - 1)
        if closing is None:
            continue
        alt = text[match.end():closing]
        end = closing + 1
        if text[end:end + 1] == "(":
            destination = _destination(text, end + 1)
        elif text[end:end + 1] == "[":
            reference_end = _bracket_end(text, end)
            reference = text[end + 1:reference_end] if reference_end is not None else ""
            destination = references.get(_label(reference or alt)) if reference_end is not None else None
        else:
            destination = references.get(_label(alt))
        if destination:
            yield destination
    for match in re.finditer(
        r"<img\b[^>]*\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        text, re.IGNORECASE,
    ):
        yield next(value for value in match.groups() if value is not None)


def _image_path(destination: str, report: str) -> Optional[str]:
    destination = html.unescape(re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])", r"\1", destination))
    try:
        url = urllib.parse.urlsplit(destination)
    except ValueError as exc:
        raise ExportError(400, "报告中的图片地址格式无效。") from exc
    # Remote and inline data images are retained in Markdown without fetching.
    if url.scheme or url.netloc:
        return None
    path = urllib.parse.unquote(url.path)
    if path.startswith("/"):
        raise ExportError(400, "报告图片需要使用相对地址才能随文档导出。")
    path = posixpath.join(posixpath.dirname(report), path)
    relative = _relative_path(path)
    if PurePosixPath(relative).suffix.lower() not in IMAGE_SUFFIXES:
        raise ExportError(400, "导出图片仅支持 PNG、JPEG、WebP 和 GIF。")
    return relative


def _is_image(contents: bytes, suffix: str) -> bool:
    if suffix in {".jpg", ".jpeg"}:
        return contents.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return contents.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return contents.startswith((b"GIF87a", b"GIF89a"))
    return contents.startswith(b"RIFF") and contents[8:12] == b"WEBP"


def build_report_archive(output_dir: Path, report_relative: str) -> ReportArchive:
    """Build a bounded ZIP containing one report and only its referenced images."""
    report = _relative_path(report_relative)
    if PurePosixPath(report).suffix.lower() != ".md":
        raise ExportError(400, "请选择 Markdown 总结文件进行导出。")
    root = output_dir.resolve()
    try:
        root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ExportError(404, "总结目录不存在或无法读取。") from exc
    stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        report_data = _read_file(root_fd, report)
        try:
            markdown = report_data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExportError(400, "总结文件不是有效的 UTF-8 文档。") from exc
        total = len(report_data)
        seen = {report}
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(report, report_data)
            for destination in _image_destinations(markdown):
                image = _image_path(destination, report)
                if image is None or image in seen:
                    continue
                if len(seen) >= MAX_EXPORT_FILES:
                    raise ExportError(413, "一次导出最多包含 256 个文件。")
                data = _read_file(root_fd, image)
                if not _is_image(data, PurePosixPath(image).suffix.lower()):
                    raise ExportError(400, "报告引用的图片格式无效，无法导出完整文档。")
                total += len(data)
                if total > MAX_EXPORT_BYTES:
                    raise ExportError(413, "报告与图片的总大小不能超过 128 MB。")
                archive.writestr(image, data)
                seen.add(image)
            # A paper original is a fixed companion asset, never an arbitrary linked file.
            pdf_reference=PurePosixPath(report).stem+"_assets/original.pdf"
            if "("+pdf_reference+")" in markdown:
                pdf=posixpath.join(posixpath.dirname(report),pdf_reference)
                data=_read_file(root_fd,_relative_path(pdf))
                if not data[:1024].lstrip().startswith(b"%PDF-"):raise ExportError(400,"原始论文文件不是有效 PDF。")
                total+=len(data)
                if total>MAX_EXPORT_BYTES or len(seen)>=MAX_EXPORT_FILES:raise ExportError(413,"导出材料超过大小或文件数限制。")
                archive.writestr(pdf,data)
        size = stream.tell()
        if size > MAX_EXPORT_BYTES:
            raise ExportError(413, "导出文件不能超过 128 MB。")
        stream.seek(0)
        return ReportArchive(stream, PurePosixPath(report).stem + ".zip", size)
    except BaseException:
        stream.close()
        raise
    finally:
        os.close(root_fd)
