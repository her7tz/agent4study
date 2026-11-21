"""Explicit bibliographic fields for the presentation cover."""

import re
from urllib.parse import urlsplit


_PROCEEDINGS = re.compile(
    r"^Proceedings of (?P<venue>.{4,160}?),\s*(?:pages?\s+|pp?\.\s*)\d", re.I
)
_ARXIV = re.compile(r"(?:https?://arxiv\.org/abs/|arXiv:\s*)(\d{4}\.\d{4,5}(?:v\d+)?)", re.I)


def _text(value, limit=250):
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if re.search(r"[\x00-\x1f\ufffe\uffff]", text):
        return ""
    return text if len(text) <= limit else ""


def publisher_bibliography(citations, url):
    fields = {}
    authors = [_text(author, 140) for author in citations.get("citation_authors", [])]
    joined = "; ".join(dict.fromkeys(author for author in authors if author))
    if joined and len(joined) <= 1200:
        fields["authors"] = {"text": joined, "source": {"kind": "publisher_metadata",
                              "field": "citation_author", "url": url}}
    venues = {text: field for field in ("citation_conference_title", "citation_journal_title")
              if (text := _text(citations.get(field)))}
    if len(venues) == 1:
        venue, field = next(iter(venues.items()))
        fields["venue"] = {"text": venue, "source": {"kind": "publisher_metadata", "field": field, "url": url}}
    return fields


def _pdf_bibliography(pdf, cancellation):
    from pypdf import PdfReader

    cancellation.check()
    reader = PdfReader(pdf)
    metadata = reader.metadata or {}
    cancellation.check()
    fields = {}
    authors = _text(metadata.get("/Author"), 1200)
    if authors:
        authors = re.sub(r"\s*;\s*", "; ", authors)
        fields["authors"] = {"text": authors, "source": {"kind": "pdf_metadata", "field": "/Author"}}
    identifier = _ARXIV.fullmatch(_text(metadata.get("/arXivID")))
    if identifier:
        fields["preprint"] = {"text": "arXiv:" + identifier[1],
                              "source": {"kind": "pdf_metadata", "field": "/arXivID"}}
    try:
        raw = reader.pages[0].extract_text() or ""
    except Exception:
        raw = ""
    lines = [line for raw_line in raw.splitlines() if (line := _text(raw_line))]
    proceedings = {hit["venue"]: line for line in lines[:12] + lines[-12:]
                   if (hit := _PROCEEDINGS.match(line))}
    if len(proceedings) == 1:
        venue, excerpt = next(iter(proceedings.items()))
        fields["venue"] = {"text": venue, "source": {"kind": "pdf_first_page", "page": 1, "quote": excerpt}}
    cancellation.check()
    return fields


def cover_bibliography(material, pdf, cancellation):
    fields = dict(material.get("bibliography") or {})
    if pdf:
        from pypdf.errors import PyPdfError
        from runtime_control import RunCancelled

        try:
            fields.update(_pdf_bibliography(pdf, cancellation))
        except RunCancelled:
            raise
        except (OSError, ValueError, RuntimeError, KeyError, IndexError, PyPdfError):
            pass
    if not fields.get("venue") and material.get("kind") == "abstract":
        source = material.get("url") or ""
        if urlsplit(source).hostname == "conf.researchr.org":
            parts = re.findall(r"\(([^()]{1,100} - (?:Research|Industry|Journal)[^()]{0,30})\)", material.get("title", ""), re.I)
            if len(parts) == 1:
                fields["venue"] = {"text": parts[0], "source": {"kind": "publisher_title", "url": source}}
    return fields
