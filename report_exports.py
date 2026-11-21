"""Export a report together with its locally referenced images.

Archive paths mirror the output directory so relative Markdown image links
continue to work after extraction, including reports stored in subdirectories.
"""

import os
import posixpath
import stat
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator, Optional


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


class _HTMLImages(HTMLParser):
    """Keep the legacy support for image tags without rendering document HTML."""

    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            source = next((value for name, value in attrs if name == "src"), None)
            if source:
                self.sources.append(source)


def _markdown_resources(markdown: str) -> Iterator[tuple[str, str]]:
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise ExportError(503, "Markdown 导出组件未安装，请运行配置运行环境后重试。") from exc

    parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    for block in parser.parse(markdown):
        for token in block.children or (block,):
            if token.type == "image":
                yield "image", token.attrGet("src")
            elif token.type == "link_open":
                yield "link", token.attrGet("href")
            elif token.type in {"html_inline", "html_block"}:
                html_images = _HTMLImages()
                html_images.feed(token.content)
                for source in html_images.sources:
                    yield "image", source


def _markdown_destinations(markdown: str, *, images: bool) -> Iterator[str]:
    kind = "image" if images else "link"
    return (destination for resource, destination in _markdown_resources(markdown) if resource == kind)


def _image_destinations(markdown: str) -> Iterator[str]:
    return _markdown_destinations(markdown, images=True)


def _image_path(destination: str, report: str) -> Optional[str]:
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


def _report_companions(links: Iterator[str], report: str) -> Iterator[str]:
    assets = PurePosixPath(report).stem + "_assets"
    allowed = {assets + "/original.pdf", assets + "/source-evidence.json",
               assets + "/presentation.pptx", assets + "/presentation-plan.json"}
    for destination in links:
        try:
            url = urllib.parse.urlsplit(destination)
        except ValueError:
            continue
        if url.scheme or url.netloc:
            continue
        relative = posixpath.normpath(urllib.parse.unquote(url.path))
        if relative in allowed:
            yield posixpath.join(posixpath.dirname(report), relative)


def _is_image(contents: bytes, suffix: str) -> bool:
    if suffix in {".jpg", ".jpeg"}:
        return contents.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return contents.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return contents.startswith((b"GIF87a", b"GIF89a"))
    return contents.startswith(b"RIFF") and contents[8:12] == b"WEBP"


def build_report_archive(output_dir: Path, report_relative: str) -> ReportArchive:
    """Build a bounded ZIP containing a report and its referenced local resources."""
    report = _relative_path(report_relative)
    if PurePosixPath(report).suffix.lower() != ".md":
        raise ExportError(400, "请选择 Markdown 总结文件进行导出。")
    root = output_dir.resolve()
    try:
        root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ExportError(404, "总结目录不存在或无法读取。") from exc
    # Python 3.9's spooled file lacks the BinaryIO seekable method used by
    # ZipFile readers. A temporary binary file supports the same export API.
    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        report_data = _read_file(root_fd, report)
        try:
            markdown = report_data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExportError(400, "总结文件不是有效的 UTF-8 文档。") from exc
        images, links = [], []
        for kind, destination in _markdown_resources(markdown):
            (images if kind == "image" else links).append(destination)
        total = len(report_data)
        seen = {report}
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(report, report_data)
            for destination in images:
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
            for companion in _report_companions(iter(links), report):
                if companion in seen:
                    continue
                data = _read_file(root_fd, _relative_path(companion))
                if companion.endswith("/original.pdf") and not data[:1024].lstrip().startswith(b"%PDF-"):
                    raise ExportError(400, "原始论文文件不是有效 PDF。")
                total += len(data)
                if total > MAX_EXPORT_BYTES or len(seen) >= MAX_EXPORT_FILES:
                    raise ExportError(413, "导出材料超过大小或文件数限制。")
                archive.writestr(companion, data)
                seen.add(companion)
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
