"""Extra column evidence for simple aligned tables in PDF layout text."""

from collections import Counter
import json
import re


_CAPTION = re.compile(r"^\s*((?:Table\s+|表\s*)[A-Za-z]*\d[\w.-]*)\s*[:：]", re.I)
_CELL = re.compile(r"\S+(?: \S+)*")
_HEADER = re.compile(r"[#\w][#\w/%().^*-]{0,15}\Z")
_NUMBER = re.compile(r"[+−-]?(?:\d+(?:[.,]\d+)?|\.\d+)(?:[eE][+−-]?\d+|[A-Za-z%]{1,2})?\Z")
_DECIMAL = re.compile(r"(?<!\S)[+−-]?\d+\.\d+(?!\S)")
_SCIENTIFIC = re.compile(r"(?<!\S)\d+(?:\.\d+)?\s*[·×]\s*10\s*\^?\s*[+−-]?\d+(?!\S)")
_MAX_HINT = 1200


def _center(cell):
    return (cell.start() + cell.end()) / 2


def _reference_row(headers, line):
    cells = list(_CELL.finditer(line))
    if not cells:
        return None
    has_label = not _NUMBER.fullmatch(cells[0].group())
    label = cells.pop(0) if has_label else None
    explicit_label_header = label is not None and len(headers) == len(cells) + 1
    row_header = headers[0].group() if explicit_label_header else "row"
    fields = headers[1:] if explicit_label_header else headers
    if len(fields) < 5 or len(fields) != len(cells) or not all(_NUMBER.fullmatch(c.group()) for c in cells):
        return None
    anchors = [_center(cell) for cell in cells]
    if any(b - a < 6 for a, b in zip(anchors, anchors[1:])):
        return None
    header_centers = [_center(field) for field in fields]
    for header, anchor in zip(header_centers, anchors):
        distance = abs(header - anchor)
        if distance > 6 or any(abs(other - anchor) < distance for other in header_centers):
            return None
    if label is not None and label.end() >= anchors[0] - 3:
        return None
    return [row_header, *(field.group() for field in fields)], anchors


def _aligned_row(line, anchors):
    cells = list(_CELL.finditer(line))
    label = ""
    if cells and not _NUMBER.fullmatch(cells[0].group()) and cells[0].end() < anchors[0] - 3:
        label = cells.pop(0).group()
    if not cells or "|" in label:
        return None
    assigned = {}
    for cell in cells:
        value = cell.group()
        if not _NUMBER.fullmatch(value):
            return None
        columns = [index for index, x in enumerate(anchors) if abs(_center(cell) - x) <= 3]
        if len(columns) != 1 or columns[0] in assigned:
            return None
        assigned[columns[0]] = value
    return "|".join([label or ".", *(assigned.get(i, ".") for i in range(len(anchors)))])


def table_layout_hint(text):
    """Return one short, partial column grid, preserving raw text elsewhere."""
    lines = text.splitlines()
    for caption_index, line in enumerate(lines):
        caption = _CAPTION.match(line)
        if not caption:
            continue
        for header_index in range(caption_index + 1, min(caption_index + 8, len(lines))):
            headers = list(_CELL.finditer(lines[header_index]))
            names = [cell.group() for cell in headers]
            if (not 5 <= len(names) <= 16 or len({name.casefold() for name in names}) != len(names)
                    or any(not _HEADER.fullmatch(name) or _NUMBER.fullmatch(name) for name in names)):
                continue
            for row_index in range(header_index + 1, min(header_index + 3, len(lines))):
                reference = _reference_row(headers, lines[row_index])
                if not reference:
                    continue
                names, anchors = reference
                data = []
                omitted = 0
                for row in lines[row_index:]:
                    if not row.strip():
                        break
                    aligned = _aligned_row(row, anchors)
                    if aligned is None:
                        omitted += 1
                    else:
                        data.append(aligned)
                if len(data) < 3:
                    continue
                heading = [caption.group(1) + " (. = blank)", "|".join(names)]
                while len(data) >= 3:
                    footer = [f"Omitted unaligned/over-budget rows: {omitted}"] if omitted else []
                    hint = "\n".join(heading + data + footer)
                    if len(json.dumps(hint, ensure_ascii=True)) <= _MAX_HINT:
                        return hint
                    data.pop()
                    omitted += 1
    return ""


def table_column_uncertainty(text):
    """Locate values between two recurrent columns in a captioned text table."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        caption = _CAPTION.match(line)
        if not caption:
            continue
        # Bound the candidate to the contiguous table below the caption.
        start = next((j for j in range(i + 1, min(i + 8, len(lines))) if not lines[j].strip()), None)
        if start is None:
            continue
        rows, anchors = [], Counter()
        for row in lines[start + 1:]:
            if not row.strip():
                break
            values = list(_SCIENTIFIC.finditer(row))
            if len(values) not in (1, 2):
                continue
            prefix = row[:values[0].start()]
            decimals = list(_DECIMAL.finditer(prefix))
            if len(decimals) < 2 or row[values[-1].end():].strip():
                continue
            label = prefix[:decimals[0].start()].strip()
            if not label or len(label) > 100:
                continue
            offsets = tuple(cell.start() - decimals[-1].end() for cell in values)
            rows.append((len(decimals), label, values[0].group(), offsets))
            if len(values) == 2:
                anchors[(len(decimals),) + offsets] += 1
        if not anchors:
            continue
        (width, left, right), count = anchors.most_common(1)[0]
        if count < 3 or right - left < 8:
            continue
        # A value can fall between the aligned anchors when printed in a
        # shared cell. Retain the value without assigning a single column.
        uncertain = [f"{label}: {value}" for columns, label, value, offsets in rows
                     if columns == width and len(offsets) == 1 and left + 3 < offsets[0] < right - 3]
        if uncertain:
            hint = caption.group(1) + " values between columns (individual column not established):\n" + "\n".join(uncertain[:6])
            if len(json.dumps(hint, ensure_ascii=True)) <= _MAX_HINT:
                return hint
    return ""
