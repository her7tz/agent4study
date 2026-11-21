"""Paper material acquisition and grounded knowledge summaries."""

import io
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from harness import load_skill_instructions
from paper_table_layout import table_column_uncertainty, table_layout_hint
from runtime_control import CancellationToken, RunCancelled
from storage_layout import atomic_text
from summary_engine import SourceBlock, SummaryEngine
from web_research import _PageText, fetch_public, public_url, PublicFetchError

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT = 300000
MAX_PAGES = 200
MAX_PAGE_CONTENT_BYTES = 8 * 1024 * 1024


PAPER_SKILL_COMPATIBILITY = """
本项目的论文 Skill 适配规则，优先于第三方 Skill 中的宿主约定：
- 默认目标是中文知识总结：概述、问题、贡献、方法、实验依据、局限与可复用知识。采用评审 Skill 的证据核对与方法分析，不强制套用整篇审稿模板。
- 本轮系统指定的处理阶段、输出格式与字数优先。提取材料片段时只整理该片段证据，综合阶段才组织最终总结。
- 只根据实际提供的文字判断知识内容。材料读取范围由读取器的 scope 字段和成稿附录统一说明。分块内未看到章节、图注或例子时，保留作者指向该内容的表述，省略未核实细节，不作整份材料是否包含该内容的判断。
- 涉及表格时分别核对原表列名、单位、空白和表注，区分显式给出的数值与按表注继承的值。跨列或位置不清的单元格仅写能确认的口径。单一变量对比同时保留共同基准、变更量和相应各项指标。
- PDF 原文块若带有 locator.table_layout，竖线分隔对齐的列，单独的点表示该格在原文中空白。结合表注理解空格，只使用对得上的行列；位置提示省略的行仍可查原始文字。若多级表头或合并值的语言、条件等归属无法确认，不擅自分配给某一列。
- locator.table_column_uncertainty 标出位置介于两条重复数值列之间的原值。保留这些原值及行名，注明单列归属未确定，不能把该值写成其中一列的值或据此说另一列空白。
- locator.text_order 为 content_stream 时，文字顺序取自 PDF 内容流。仅引用清楚的语句，不根据顺序推测分栏、表头或数值对应关系。
- 不为满足模板数量而凑出三项优点或缺点；不默认给分、录用建议或对领域影响作无依据评级。区分论文自述局限与总结者推断。
- 当前论文执行器不提供 web_fetch、present_files、deep-research 或任意文件写入工具。第三方 Skill 中这些步骤与 /mnt 路径是原宿主的约定，不执行也不假装已执行。
- 材料读取、成果保存、来源记录与结束检查由已有 Harness 处理；业务 Agent 使用已注册的 produce_result、inspect_result、finish_task 工具。
""".strip()


class PaperError(ValueError):
    status = 400
    retryable = False


class MaterialRequired(PaperError):
    pass


def _fetch_paper(url, token):
    try:
        return fetch_public(url, token, timeout=30, binary=True, max_bytes=MAX_PDF_BYTES)
    except PublicFetchError as exc:
        if exc.retryable:
            raise
        detail = "返回 HTTP 202，当前游客读取器未取得论文文字，页面可能要求访问验证或异步加载" if exc.http_status == 202 else str(exc)
        raise MaterialRequired("论文链接" + detail + "。请上传可读取的论文 PDF、粘贴原文，或提供无需验证的公开全文链接。") from exc


class PaperUploads:
    def __init__(self, data_home):
        self.root = Path(data_home).resolve() / "uploads" / "papers"

    def get(self, upload_id):
        if not isinstance(upload_id, str) or not re.fullmatch(r"[a-f0-9]{32}", upload_id):
            raise PaperError("论文附件编号无效。")
        directory = self.root / upload_id
        path = directory / "source.pdf"
        if any(p.is_symlink() for p in (self.root.parent, self.root, directory, path, directory / "metadata.json")) or not path.is_file():
            raise PaperError("论文附件不存在，请重新上传 PDF。")
        try:
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PaperError("论文附件信息不完整，请重新上传。") from exc
        return path, metadata

    def save(self, data, filename):
        if not data or len(data) > MAX_PDF_BYTES or not data[:1024].lstrip().startswith(b"%PDF-"):
            raise PaperError("请上传有效 PDF 文件，大小不超过 20 MB。")
        if any(p.is_symlink() for p in (self.root.parent, self.root)):
            raise PaperError("论文附件目录不使用符号链接。")
        upload_id = uuid.uuid4().hex
        directory = self.root / upload_id
        directory.mkdir(parents=True, mode=0o700)
        filename = re.sub(r"[\x00-\x1f/\\]", "_", str(filename))[:200] or "论文.pdf"
        path = directory / "source.pdf"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = {"upload_id": upload_id, "filename": filename, "size": len(data), "created_at": time.time()}
        atomic_text(directory / "metadata.json", json.dumps(metadata, ensure_ascii=False))
        return metadata

    def discard_unattached(self, upload_id):
        path, _ = self.get(upload_id)
        shutil.rmtree(path.parent)


def validate_paper(parameters):
    presentation_keys = {"include_presentation", "presentation_minutes", "presentation_language"}
    result = {key: value.strip() for key, value in parameters.items()
              if key not in presentation_keys and value.strip()}
    result.update({key: parameters[key] for key in presentation_keys if key in parameters})
    sources = [key for key in ("paper_text", "url", "upload_id") if result.get(key)]
    if len(sources) != 1:
        raise PaperError("请选择一种论文来源：原文、公开链接或 PDF 附件。")
    if "source_url" in result:
        result["source_url"] = public_url(result["source_url"])
    if "url" in result:
        result["url"] = public_url(result["url"])
    if "paper_text" in result and len(result["paper_text"]) < 200:
        raise PaperError("原文太短，请提供至少 200 字的论文材料。")
    if "upload_id" in result and not re.fullmatch(r"[a-f0-9]{32}", result["upload_id"]):
        raise PaperError("论文附件编号无效。")
    return result


def _pdf_text_streams(content, resources, token):
    """Inspect text rendering and invoked forms in a page's content streams."""
    from pypdf.generic import ContentStream, DictionaryObject, StreamObject

    hidden_text = form_text = False
    remaining = MAX_PAGE_CONTENT_BYTES - len(content.get_data())
    pending = [(content, resources, 0, False)]
    streams = {}
    visited = set()
    while pending:
        token.check()
        stream, resource, rendering, in_form = pending.pop()
        resource = resource.get_object() if resource is not None else None
        xobjects = resource.get('/XObject') if isinstance(resource, DictionaryObject) else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        modes = [rendering]
        for operands, operator in stream.operations:
            if operator == b"q":
                modes.append(modes[-1])
            elif operator == b"Q" and len(modes) > 1:
                modes.pop()
            elif operator == b"Tr" and operands:
                modes[-1] = operands[0]
            elif operator in (b"Tj", b"TJ", b"'", b'"') and operands and operands[-1]:
                form_text = form_text or in_form
                hidden_text = hidden_text or modes[-1] == 3
            elif operator == b"Do" and operands and isinstance(xobjects, DictionaryObject) and operands[0] in xobjects:
                form = xobjects[operands[0]]
                if not isinstance(form, StreamObject) or form.get('/Subtype') != '/Form':
                    continue
                form_resources = form.get('/Resources', resource)
                key = (id(form), id(form_resources), int(modes[-1]))
                if key in visited:
                    continue
                visited.add(key)
                if id(form) not in streams:
                    remaining -= len(form.get_data())
                    if remaining < 0:
                        raise PaperError("PDF 页面内容过大，请粘贴可读取的论文原文。")
                    streams[id(form)] = ContentStream(form, content.pdf)
                pending.append((streams[id(form)], form_resources, modes[-1], True))
    return hidden_text, form_text


def _fragmented_scan_text(text):
    """Recognize dense OCR fragments without guessing the page language."""
    fragments = re.findall(r"[^\W_]+", text)
    return len(fragments) >= 12 and sum(len(fragment) >= 3 for fragment in fragments) < 2


def _pdf(data, token, progress):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PaperError("缺少 PDF 读取依赖，请运行“配置运行环境.command”。") from exc
    if len(data) > MAX_PDF_BYTES:
        raise PaperError("PDF 超过 20 MB，请提供较小文件或粘贴原文。")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PaperError("PDF 有密码保护，请上传可读取的版本或粘贴原文。")
        pages = len(reader.pages)
        if pages > MAX_PAGES:
            raise PaperError("PDF 超过 200 页，请拆分后提供。")
        parts, page_texts, chars, empty = [], [], 0, []
        hidden_text_pages = []
        fragmented_scan_pages = []
        form_text_pages = []
        unread_form_pages = []
        readable_chars = 0
        for index, page in enumerate(reader.pages, 1):
            token.check()
            progress("读取论文 PDF（{}/{} 页）".format(index, pages))
            content = page.get_contents()
            content_bytes = content.get_data() if content is not None else b""
            if len(content_bytes) > MAX_PAGE_CONTENT_BYTES:
                raise PaperError("PDF 页面内容过大，请粘贴可读取的论文原文。")
            # A valid blank page may omit /Contents. Some pypdf versions raise
            # in layout mode for that case; keep it as an empty source page.
            text = (page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False) or "").strip() if content is not None else ""
            page_chars = len(re.sub(r"\s", "", text))
            hidden_text = form_text = False
            if b"Tr" in content_bytes or b"Do" in content_bytes:
                hidden_text, form_text = _pdf_text_streams(content, page.get('/Resources'), token)
            text_order = None
            if form_text:
                stream_text = (page.extract_text() or "").strip()
                stream_chars = len(re.sub(r"\s", "", stream_text))
                if stream_chars > page_chars:
                    text, page_chars = stream_text, stream_chars
                    text_order = "content_stream"
                else:
                    unread_form_pages.append(index)
            if hidden_text:
                hidden_text_pages.append(index)
            if page_chars < 20:
                empty.append(index)
                if hidden_text:
                    continue
            elif hidden_text and _fragmented_scan_text(text):
                fragmented_scan_pages.append(index)
                continue
            else:
                # Sparse page numbers/footers and our generated page labels
                # cannot establish that a scanned document has readable text.
                readable_chars += page_chars
            if text:
                block = "[第 {} 页]\n{}".format(index, text)
                chars += len(block)
                if chars > MAX_TEXT:
                    raise PaperError("论文文字超过 30 万字，请拆分后提供。")
                parts.append(block)
                page_text = {"page": index, "text": block}
                if text_order:
                    form_text_pages.append(index)
                    page_text["text_order"] = text_order
                page_texts.append(page_text)
        metadata = reader.metadata or {}
        title = str(metadata.get("/Title") or "").strip()[:200]
    except (PaperError, RunCancelled):
        raise
    except Exception as exc:
        raise PaperError("无法解析 PDF，请上传未损坏的可选中文字版本。") from exc
    text = "\n\n".join(parts)
    if readable_chars < 200:
        raise PaperError("PDF 未提取到足够文字，可能是扫描版。本版暂不支持 OCR，请粘贴原文或上传可选中文字的 PDF。")
    notice = ("已检查 PDF 的全部 {} 个物理页，其中 {} 页有足量可提取文字。"
              "旋转文字的排版与阅读顺序可能不准确。图像、公式及复杂表格未做视觉识别。"
              .format(pages, pages - len(empty) - len(fragmented_scan_pages)))
    if empty:
        notice += " 第 {} 页文字不足，可能包含扫描内容或仅有图表。".format("、".join(map(str, empty)))
    if hidden_text_pages:
        notice += (" 检测到 {} 页包含 PDF 内嵌的不可见文字层。该层可能来自扫描识别，"
                   "本次未与页面图像核对。只有少量内嵌文字的页未用作总结依据。".format(len(hidden_text_pages)))
    if fragmented_scan_pages:
        notice += " 第 {} 页内嵌文字多为短字符碎片，未用作总结依据。".format("、".join(map(str, fragmented_scan_pages)))
    if form_text_pages:
        notice += " 第 {} 页按内容流顺序提取文字，段落、分栏和表格的排版可能与原页不同。".format("、".join(map(str, form_text_pages)))
    if unread_form_pages:
        notice += " 第 {} 页检测到嵌入内容流中的文字，未能补读。".format("、".join(map(str, unread_form_pages)))
    return {"text": text, "title": title, "pages": pages, "scope": notice, "kind": "pdf", "empty_pages": empty,
            "hidden_text_pages": hidden_text_pages, "fragmented_scan_pages": fragmented_scan_pages,
            "form_text_pages": form_text_pages, "unread_form_pages": unread_form_pages, "_page_texts": page_texts}


def _pdf_material(data, settings, token, progress):
    material=_pdf(data,token,progress)
    upload=PaperUploads(settings.data_home).save(data,"original.pdf")
    path,_=PaperUploads(settings.data_home).get(upload["upload_id"])
    return {**material,"_pdf_path":str(path)}


class _PaperPage(_PageText):
    def __init__(self):
        super().__init__()
        self.citations = {"citation_authors": []}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name == "citation_author":
                self.citations["citation_authors"].append(values.get("content", ""))
            elif name in {"citation_pdf_url", "citation_title", "citation_abstract", "description", "og:description",
                          "citation_conference_title", "citation_journal_title"}:
                self.citations[name] = values.get("content", "")
        super().handle_starttag(tag, attrs)


class _ResearchrPaperPage(_PaperPage):
    """Extract the Abstract field of a Researchr conference details page."""

    def __init__(self):
        super().__init__()
        self.abstracts = []
        self._label_start = None
        self._field_parent_depth = None
        self._abstract_field = None

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        depth = len(self.stack)
        if self._field_parent_depth is not None:
            if depth == self._field_parent_depth and tag == "div" and "col-sm-10" in classes:
                self._abstract_field = (depth + 1, len(self.parts))
            self._field_parent_depth = None
        if tag == "label" and "control-label" in classes:
            self._label_start = len(self.parts)
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "label" and self._label_start is not None:
            label = "".join(self.parts[self._label_start:]).strip().casefold()
            if label == "abstract":
                self._field_parent_depth = len(self.stack)
            self._label_start = None
        if self._abstract_field and len(self.stack) < self._abstract_field[0]:
            start = self._abstract_field[1]
            self.abstracts.append("\n".join(line.strip() for line in "".join(self.parts[start:]).splitlines() if line.strip()))
            self._abstract_field = None


def read_paper(parameters, settings, token, progress):
    if parameters.get("paper_text"):
        return {"text": parameters["paper_text"], "title": parameters.get("title", "论文原文总结"),
                "kind": "text", "scope": "仅基于用户提供的原文；未核实其是否包含论文全文。"}
    if parameters.get("upload_id"):
        path, metadata = PaperUploads(settings.data_home).get(parameters["upload_id"])
        if path.stat().st_size > MAX_PDF_BYTES:
            raise PaperError("PDF 超过20 MB，请重新上传较小文件。")
        material = _pdf(path.read_bytes(), token, progress)
        return {**material, "filename": metadata["filename"], "upload_id": parameters["upload_id"], "_pdf_path": str(path), "original_url": parameters.get("source_url"), "title": material["title"] or parameters.get("title") or metadata["filename"]}
    original = parameters["url"]
    parsed = urlsplit(original)
    target = original
    if parsed.hostname in {"arxiv.org", "www.arxiv.org"} and parsed.path.startswith("/abs/"):
        target = urlunsplit(("https", "arxiv.org", "/pdf/" + parsed.path[5:], "", ""))
    progress("获取公开论文材料…")
    fallback = None
    try:
        final, data, mime = _fetch_paper(target, token)
    except RunCancelled:
        raise
    except Exception:
        if target == original:
            raise
        final, data, mime = _fetch_paper(original, token)
        fallback = "论文全文链接暂不可访问，仅读取链接页面可见材料。"
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return {**_pdf_material(data, settings, token, progress), "url": final, "original_url": original}
    if "pdf" in mime:
        raise PaperError("链接声称是 PDF，但没有返回有效 PDF。请上传原文。")
    if not any(kind in mime for kind in ("text/", "html", "xml")):
        raise PaperError("链接不是可读取的 PDF 或论文文字页面。")
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", mime)
    try:
        text = data.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    title, scope = parameters.get("title", "论文链接总结"), "仅基于链接页面可见文字；不将摘要页面当作论文全文。"
    bibliography = {}
    if "html" in mime:
        location = urlsplit(final)
        conference_paper = location.hostname == "conf.researchr.org" and location.path.startswith("/details/")
        parser = _ResearchrPaperPage() if conference_paper else _PaperPage()
        parser.feed(text)
        from paper_bibliography import publisher_bibliography
        bibliography = publisher_bibliography(parser.citations, final)
        visible = "".join(parser.parts).strip()
        if len(visible) < 2000 and any(phrase in visible.casefold() for phrase in (
                "we need to verify that you're not a robot", "verify you are human",
                "enable javascript and cookies to continue", "checking your browser before accessing")):
            raise MaterialRequired("论文页面是访问验证页面，未取得论文内容。请上传 PDF、粘贴原文或提供公开全文链接。")
        title = parser.citations.get("citation_title") or "".join(parser.title).strip() or title
        pdf_url = parser.citations.get("citation_pdf_url")
        if pdf_url:
            try:
                pdf_final, pdf_data, _ = _fetch_paper(urljoin(final, pdf_url), token)
                if not pdf_data[:1024].lstrip().startswith(b"%PDF-"):
                    raise PaperError("全文链接没有返回 PDF。")
                return {**_pdf_material(pdf_data, settings, token, progress), "url": pdf_final, "original_url": original,
                        "title": title[:200], "bibliography": bibliography}
            except RunCancelled:
                raise
            except Exception:
                fallback = "页面提供的 PDF 全文未能读取，本总结仅使用页面文字。"
        if conference_paper:
            abstracts = [abstract for abstract in dict.fromkeys(parser.abstracts) if abstract]
            if len(abstracts) != 1 or len(abstracts[0]) < 200:
                raise MaterialRequired("会议页面未取得唯一、可读取的论文摘要。请上传 PDF、粘贴原文或提供公开全文链接。")
            if len(abstracts[0]) > MAX_TEXT:
                raise PaperError("会议摘要文字超过 30 万字，请提供论文正文或上传 PDF。")
            return {"text": abstracts[0], "title": title[:200], "url": final, "original_url": original,
                    "bibliography": bibliography,
                    "kind": "abstract", "scope": "仅基于会议公开摘要。论文全文、图表及附录未读取。"}
        parts = parser.main if len("".join(parser.main).strip()) > 500 else parser.parts
        text = "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())
        abstract = parser.citations.get("citation_abstract", "")
        if abstract and abstract not in text:
            text += "\n\n摘要：" + abstract
    if len(text) < 200:
        raise MaterialRequired("论文页面文字不足，可能需要登录或由脚本加载。请上传 PDF 或粘贴原文。")
    if len(text) > MAX_TEXT:
        raise PaperError("链接文字超过 30 万字，请提供论文正文或上传 PDF。")
    return {"text": text, "title": title[:200], "url": final, "original_url": original,
            "bibliography": bibliography, "kind": "page", "scope": scope + (" " + fallback if fallback else "")}


def _paper_source_blocks(text, material, page_texts=None):
    """Keep the PDF's real page boundaries available during consolidation."""
    locator = {"kind": material["kind"]}
    for key in ("url", "original_url", "filename"):
        if material.get(key):
            locator[key] = material[key]
    if material["kind"] != "pdf" or not page_texts:
        return [SourceBlock("paper-text", text, locator)]
    # Page labels printed inside a paper are data, never trusted boundaries.
    # The reader records these page numbers directly from the PDF page list.
    blocks = []
    for item in page_texts:
        location = {**locator, "page": item["page"]}
        if item.get("text_order") == "content_stream":
            location["text_order"] = item["text_order"]
        else:
            hint = table_layout_hint(item["text"])
            if hint and len(json.dumps({**location, "table_layout": hint}, ensure_ascii=False)) <= 2000:
                location["table_layout"] = hint
            if not hint:
                uncertain = table_column_uncertainty(item["text"])
                if uncertain and len(json.dumps({**location, "table_column_uncertainty": uncertain}, ensure_ascii=False)) <= 2000:
                    location["table_column_uncertainty"] = uncertain
        blocks.append(SourceBlock("paper-page-{}".format(item["page"]), item["text"], location))
    return blocks


def _paper_prose(markdown):
    # Mask examples and comments without changing the offsets of prose.
    visible = markdown
    for fence in ('`', '~'):
        pattern = r'(?ms)^ {0,3}(' + fence + r'{3,})[^\n]*\n.*?(?:^ {0,3}\1' + fence + r'*[ \t]*$|\Z)'
        visible = re.sub(pattern, lambda match: ' ' * len(match.group()), visible)
    visible = re.sub(r'(?s)(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)',
                     lambda match: ' ' * len(match.group()), visible)
    return re.sub(r'(?s)<!--.*?(?:-->|\Z)', lambda match: ' ' * len(match.group()), visible)


def _render_paper_citations(markdown, evidence):
    matches = list(re.finditer(r'(?<!\\)\[\[(k\d+)\]\]', _paper_prose(markdown)))
    units = {unit['id']: unit for unit in evidence['units']}
    if {match.group(1) for match in matches} != set(units):
        raise PaperError('论文正文的依据编号缺失或引用了未知条目，请重试。')
    for match in reversed(matches):
        unit_id = match.group(1)
        sources = [evidence['sources'][source_id] for source_id in units[unit_id]['source_ids']]
        pages = sorted({source['page'] for source in sources if source.get('kind') == 'pdf' and 'page' in source})
        location = '，PDF 第 {} 页'.format('、'.join(map(str, pages))) if pages else ''
        citation = '（依据 {}{}）'.format(unit_id, location)
        markdown = markdown[:match.start()] + citation + markdown[match.end():]
    return markdown


def _paper_heading(text):
    return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", " ".join(text.split()))


_WINDOW = r"(?:本|该|此|当前)(?:处理批次|材料窗口|分块|片段|块)"
_WINDOW_ASIDE = re.compile(
    r"(?<=[^\s。！？；，])[，](?:但|而|且)?(?:"
    + _WINDOW + r"(?:尚未|没有|未)(?:提供|给出|展开|展示)[^\s，。！？；\n“”‘’「」『』`|\[\]]*"
    + r"|不是" + _WINDOW + r"(?:已给出的验证结果|单独实验验证的因果结论))"
    + r"(?=[。！？；\n]|$)(?![。！？；][”’」』])"
)


def _remove_paper_window_asides(evidence):
    # A clause about the current processing window is not a finding about the
    # paper. Only remove subordinate asides, leaving citations and the rest of
    # the sentence intact. Paper-level limitations come from the text or scope.
    count = 0
    for item, field in [(evidence, "overview")] + [(unit, "content") for unit in evidence["units"]]:
        text = item[field]
        matches = list(_WINDOW_ASIDE.finditer(_paper_prose(text)))
        for match in reversed(matches):
            text = text[:match.start()] + text[match.end():]
        item[field] = text
        count += len(matches)
    return count


def _assemble_paper(title, evidence):
    sections = ["# " + _paper_heading(title)]
    for unit in evidence["units"]:
        sections.extend(("## " + _paper_heading(unit["title"]), unit["content"], "[[{}]]".format(unit["id"])))
    return _render_paper_citations("\n\n".join(sections), evidence)


def summarize_paper(parameters, context):
    token = context.cancellation or CancellationToken()
    progress = context.progress or (lambda message: None)
    material = read_paper(parameters, context.settings, token, progress)
    token.check()
    skill = load_skill_instructions("paper_summary") + "\n\n" + PAPER_SKILL_COMPATIBILITY
    text = material.pop("text")
    pdf_path = material.pop("_pdf_path", None)
    page_texts = material.pop("_page_texts", None)
    blocks = _paper_source_blocks(text, material, page_texts)
    source_urls = list(dict.fromkeys(material[key] for key in ("original_url", "url") if material.get(key)))
    evidence = SummaryEngine(context.client, skill_instructions=skill, progress=progress, cancellation=token).summarize(
        blocks, kind="paper", title=material["title"], scope=material["scope"], source_urls=source_urls,
        review_full_material=True).as_dict()
    if not evidence["units"]:
        raise PaperError("论文材料未提取到可用于总结的知识，请检查原文和阅读范围。")
    removed_asides = _remove_paper_window_asides(evidence)
    token.check()
    progress("整理论文概述与关键知识…")
    title = " ".join(material["title"].split())[:200] or "论文概述与关键知识"
    markdown = _assemble_paper(title, evidence)
    if len(markdown) > 80000:
        raise PaperError("论文成稿展开依据位置后超过长度限制，请缩小材料后重试。")
    known_urls = {material.get("url"), material.get("original_url")} - {None}
    markdown = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", lambda match: match.group(0) if match.group(2) in known_urls else match.group(1) + "（引用未核实）", markdown)
    appendix = ["## 材料与阅读范围", "", material["scope"]]
    for label, key in (("输入链接", "original_url"), ("实际读取", "url")):
        if material.get(key):
            appendix.extend(["", "- [{}]({})".format(label, material[key])])
    if material.get("filename"):
        appendix.extend(["", "- 上传文件：" + re.sub(r"[\[\]`*<>]", "", material["filename"])])
    appendix.extend(["", "- 已读取 {} 字，来自 {} 个材料来源单元。".format(len(text), len(blocks)), "- 本总结由模型生成，来源编号检查不等于事实核验；关键结论请结合原文核对。"])
    markdown += "\n\n" + "\n".join(appendix) + "\n"
    token.check()
    return {"title": title, "markdown": markdown,
            "sources": [material], "source_scope": material["scope"], "material_chars": len(text), "_pdf_path": pdf_path,
            "_source_evidence": {"version": 1, "material": material, "summary": evidence,
                                 "source_blocks": [{"id": block.id, "text": block.text,
                                                    "locator": block.locator} for block in blocks]},
            "summary_processing": {**evidence.get("processing", {}), "rendered_units": len(evidence["units"]),
                                   "processing_window_asides_removed": removed_asides,
                                   "rendering_check": "assembled_reviewed_units_with_reader_citations"},
            "sections": len(re.findall(r"^##\s", markdown, re.M))}
