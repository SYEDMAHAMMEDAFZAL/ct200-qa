"""PDF -> section tree parser for the CT-200 manual.

The CT-200 PDFs carry a real text layer (embedded Nimbus Sans fonts), so the
primary path reads text spans with their font metadata from PyMuPDF. OCR
(Tesseract via PyMuPDF) is used for two things only:
  * a fallback for any page that has no usable text layer, and
  * an optional cross-check that OCRs every page and reports words the text
    layer did not give us (see ``ocr_crosscheck``).

Hierarchy is derived from the section *numbering*, not from font size: the
manual renders "3.2" and "2.1.1.1" at body size (11pt bold) while sibling
headings use 12.9pt, so a size-based level detector mis-parents them.

Every structural surprise is recorded as an ``anomaly`` on the node it affects
instead of being silently repaired.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
LIST_RE = re.compile(r"^(?:(\d+)[.)]|[••\-–*])\s+(\S.*)$")
XREF_RE = re.compile(r"\b(?:see|section|sections)\s+((?:\d+(?:\.\d+)+)(?:\s*(?:,|and)\s*\d+(?:\.\d+)+)*)", re.I)
XREF_NUM_RE = re.compile(r"\d+(?:\.\d+)+")

TITLE_MIN_SIZE = 18.0
PARAGRAPH_GAP_FACTOR = 1.6  # vertical gap (in line heights) that starts a new paragraph
MIN_TEXT_CHARS_PER_PAGE = 20  # below this a page is treated as having no text layer


# --------------------------------------------------------------------------- text utils

def normalize_text(s: str) -> str:
    """Unicode clean-up that must be identical for every version we compare.

    NFKC folds the ligatures (U+FB01 "fi", U+FB00 "ff") the PDF uses; the
    non-breaking hyphen U+2011 (rendered from a fallback serif font) becomes '-'.
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("‐", "-").replace("‑", "-").replace(" ", " ")
    return s


def collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def content_hash(text: str) -> str:
    """Hash of whitespace-insensitive text, so a reflowed line break is not a change."""
    return hashlib.sha256(collapse_ws(text).encode("utf-8")).hexdigest()


def number_tuple(number: str) -> tuple[int, ...]:
    return tuple(int(p) for p in number.split("."))


# --------------------------------------------------------------------------- data classes

@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool
    size: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Block:
    kind: str  # paragraph | list_item | table | figure
    text: str
    page: int
    rows: list[list[str]] | None = None  # tables only, first row is the header
    marker: str | None = None  # list items: "1", "2", "•" ...

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "text": self.text, "page": self.page}
        if self.rows is not None:
            d["rows"] = self.rows
        if self.marker is not None:
            d["marker"] = self.marker
        return d


@dataclass
class Section:
    key: str  # unique within a parsed document; normally == number
    number: str | None
    heading: str
    level: int
    order: int
    page: int
    parent_key: str | None = None
    blocks: list[Block] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)
    references: list[str] = field(default_factory=list)

    @property
    def body_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text)

    @property
    def content_hash(self) -> str:
        return content_hash(self.body_text)


@dataclass
class ParsedDocument:
    title: str
    sections: list[Section]
    page_count: int
    warnings: list[dict] = field(default_factory=list)

    def by_key(self) -> dict[str, Section]:
        return {s.key: s for s in self.sections}

    def children(self, key: str | None) -> list[Section]:
        return [s for s in self.sections if s.parent_key == key]


# --------------------------------------------------------------------------- extraction

def _merge_spans(spans: list[dict]) -> tuple[str, bool, float]:
    text = "".join(s["text"] for s in spans)
    # A line is bold when its "real" characters are bold; ignore 1-char fallback-font spans
    # such as the serif non-breaking hyphen that the PDF drops into body text.
    weighted = [s for s in spans if len(s["text"].strip()) > 1] or spans
    bold = all(("Bold" in s["font"]) or (s["flags"] & 16) for s in weighted)
    size = max(s["size"] for s in weighted)
    return text, bold, size


def _inside(bbox, rect) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return rect[0] - 1 <= cx <= rect[2] + 1 and rect[1] - 1 <= cy <= rect[3] + 1


def _page_lines(page: pymupdf.Page, pno: int, textpage=None) -> list[Line]:
    d = page.get_text("dict", textpage=textpage) if textpage else page.get_text("dict")
    lines: list[Line] = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"]]
            if not spans:
                continue
            text, bold, size = _merge_spans(spans)
            text = normalize_text(text)
            if not text.strip():
                continue
            x0, y0, x1, y1 = ln["bbox"]
            lines.append(Line(pno, x0, y0, x1, y1, text.strip(), bold, size))
    return lines


def _table_rows(table, lines: list[Line]) -> list[list[str]]:
    """Rebuild cell text from our own lines.

    ``Table.extract()`` mangles multi-line cells in this PDF (it returned
    "Aborts measurement displays E1\\n," for "Aborts measurement, displays E1"),
    so cell text is rebuilt by assigning each text line to the cell box that
    contains it.
    """
    rows: list[list[str]] = []
    for row in table.rows:
        out_row = []
        for cell in row.cells:
            if cell is None:
                out_row.append("")
                continue
            parts = [ln.text for ln in lines if _inside((ln.x0, ln.y0, ln.x1, ln.y1), cell)]
            out_row.append(collapse_ws(" ".join(parts)))
        rows.append(out_row)
    return rows


def _table_text(rows: list[list[str]]) -> str:
    return "\n".join(" | ".join(r) for r in rows)


@dataclass
class _Item:
    """A positioned thing on a page: a text line, a table or a figure."""
    page: int
    y: float
    x: float
    line: Line | None = None
    table_rows: list[list[str]] | None = None
    figure_bbox: tuple | None = None


def _extract_items(doc: pymupdf.Document, warnings: list[dict]) -> list[_Item]:
    items: list[_Item] = []
    for pno, page in enumerate(doc):
        textpage = None
        raw = page.get_text("text")
        if len(raw.strip()) < MIN_TEXT_CHARS_PER_PAGE:
            try:
                textpage = page.get_textpage_ocr(full=True, dpi=300)
                warnings.append({"type": "ocr_page", "page": pno + 1,
                                 "detail": "page has no text layer; OCR text used, bold/size cues unavailable"})
            except Exception as exc:  # tesseract missing etc. -> expose, don't hide
                warnings.append({"type": "unreadable_page", "page": pno + 1,
                                 "detail": f"no text layer and OCR failed: {exc}"})
                continue
        lines = _page_lines(page, pno, textpage)
        tables = list(page.find_tables().tables) if textpage is None else []
        table_rects = [t.bbox for t in tables]
        for t in tables:
            rows = _table_rows(t, lines)
            items.append(_Item(pno, t.bbox[1], t.bbox[0], table_rows=rows))
        for ln in lines:
            if any(_inside((ln.x0, ln.y0, ln.x1, ln.y1), r) for r in table_rects):
                continue
            items.append(_Item(pno, ln.y0, ln.x0, line=ln))
        for img in page.get_images(full=True):
            for rect in page.get_image_rects(img[0]):
                items.append(_Item(pno, rect.y0, rect.x0, figure_bbox=tuple(rect)))
    items.sort(key=lambda it: (it.page, round(it.y, 1), it.x))
    return items


# --------------------------------------------------------------------------- structuring

def parse_pdf(path: str | Path) -> ParsedDocument:
    doc = pymupdf.open(str(path))
    warnings: list[dict] = []
    items = _extract_items(doc, warnings)
    page_count = doc.page_count
    doc.close()

    body_lines = [it.line for it in items if it.line and not it.line.bold]
    body_size = _mode([round(l.size, 1) for l in body_lines]) if body_lines else 11.0
    left_margin = min((l.x0 for l in body_lines), default=0.0)

    title_parts: list[str] = []
    sections: list[Section] = []
    current: Section | None = None
    prev_line: Line | None = None  # previous body line in the current section
    seen_heading = False

    def new_block(kind: str, text: str, page: int, **kw) -> Block:
        b = Block(kind, text, page, **kw)
        target().blocks.append(b)
        return b

    def target() -> Section:
        nonlocal current
        if current is None:
            # Body text before the first numbered heading: keep it, but say so.
            current = Section(key="front-matter", number=None, heading="(front matter)", level=0,
                              order=len(sections), page=0)
            current.anomalies.append({"type": "front_matter",
                                      "detail": "text before the first numbered heading"})
            sections.append(current)
        return current

    open_block: Block | None = None

    for it in items:
        if it.table_rows is not None:
            new_block("table", _table_text(it.table_rows), it.page + 1, rows=it.table_rows)
            open_block, prev_line = None, None
            continue
        if it.figure_bbox is not None:
            new_block("figure", "[figure]", it.page + 1)
            target().anomalies.append({"type": "figure_without_caption_support",
                                       "detail": f"image on page {it.page + 1}; captions are not extracted"})
            open_block, prev_line = None, None
            continue

        ln = it.line
        assert ln is not None
        m = HEADING_RE.match(ln.text)

        if not seen_heading and ln.page == 0 and ln.bold and ln.size >= TITLE_MIN_SIZE:
            title_parts.append(ln.text)
            continue

        if ln.bold and m:
            seen_heading = True
            number, heading = m.group(1), collapse_ws(m.group(2))
            current = Section(key=number, number=number, heading=heading,
                              level=len(number.split(".")), order=len(sections), page=ln.page + 1)
            sections.append(current)
            open_block, prev_line = None, None
            continue

        if ln.bold and not m:
            if current is not None and prev_line is None and not current.blocks:
                # wrapped heading: a second bold line straight after the heading
                current.heading = collapse_ws(current.heading + " " + ln.text)
                continue
            target().anomalies.append({"type": "unnumbered_bold_line", "page": ln.page + 1,
                                       "detail": f"bold text without a section number kept as body: {ln.text!r}"})

        if not ln.bold and m and ln.x0 <= left_margin + 2 and "." in m.group(1):
            # e.g. a stray "2.3 Something" in regular weight at the margin
            target().anomalies.append({"type": "numbered_regular_line", "page": ln.page + 1,
                                       "detail": f"looks like a heading but is not bold; kept as body: {ln.text!r}"})

        lm = LIST_RE.match(ln.text)
        indented = ln.x0 > left_margin + 5
        if lm and indented:
            open_block = new_block("list_item", collapse_ws(lm.group(2)), ln.page + 1,
                                   marker=lm.group(1) or ln.text[0])
            prev_line = ln
            continue

        if open_block is not None and prev_line is not None:
            if open_block.kind == "list_item" and ln.x0 > prev_line.x0 - 1 and ln.x0 > left_margin + 5 \
                    and ln.page == prev_line.page and _gap(prev_line, ln) < PARAGRAPH_GAP_FACTOR:
                open_block.text = collapse_ws(open_block.text + " " + ln.text)
                prev_line = ln
                continue
            same_para = open_block.kind == "paragraph" and (
                ln.page != prev_line.page  # page break alone never ends a paragraph
                or _gap(prev_line, ln) < PARAGRAPH_GAP_FACTOR)
            if same_para:
                open_block.text = collapse_ws(open_block.text + " " + ln.text)
                prev_line = ln
                continue

        open_block = new_block("paragraph", ln.text, ln.page + 1)
        prev_line = ln

    _resolve_hierarchy(sections)
    _detect_references(sections)
    if not title_parts:
        warnings.append({"type": "no_title", "detail": "no large bold title found on page 1"})
    if body_size and abs(body_size - 11.0) > 2:
        warnings.append({"type": "unexpected_body_size", "detail": f"body font size {body_size}"})
    return ParsedDocument(title=collapse_ws(" ".join(title_parts)), sections=sections,
                          page_count=page_count, warnings=warnings)


def _gap(a: Line, b: Line) -> float:
    h = a.height or 1.0
    return (b.y0 - a.y1) / h




def _mode(values: list[float]) -> float:
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


def _resolve_hierarchy(sections: list[Section]) -> None:
    """Parent each numbered section by its number, in two passes.

    * duplicate section numbers get a distinct key ("3.3~2") and an anomaly;
    * a missing intermediate level (2.1.1.1 with no 2.1.1) attaches to the
      nearest existing ancestor and is flagged, rather than inventing 2.1.1;
    * siblings that appear out of numeric order (3.4 before 3.3) keep their
      document order and are flagged.
    """
    seen: dict[str, Section] = {}
    for s in sections:
        if s.number is None:
            continue
        if s.number in seen:
            n = 2
            while f"{s.number}~{n}" in seen:
                n += 1
            s.key = f"{s.number}~{n}"
            s.anomalies.append({"type": "duplicate_number",
                                "detail": f"section number {s.number} already used on page {seen[s.number].page}"})
        seen[s.key] = s

    first_by_number = {}
    for s in sections:
        if s.number is not None:
            first_by_number.setdefault(s.number, s)

    for s in sections:
        if s.number is None:
            continue
        parts = s.number.split(".")
        parent = None
        for depth in range(len(parts) - 1, 0, -1):
            cand = ".".join(parts[:depth])
            if cand in first_by_number:
                parent = first_by_number[cand]
                break
        if len(parts) > 1:
            expected = ".".join(parts[:-1])
            if parent is None:
                s.anomalies.append({"type": "orphan",
                                    "detail": f"no ancestor of {s.number} exists; attached to document root"})
            elif parent.number != expected:
                s.anomalies.append({"type": "skipped_level",
                                    "detail": f"expected parent {expected} does not exist; "
                                              f"attached to nearest ancestor {parent.number}"})
        s.parent_key = parent.key if parent else None

    # out-of-order siblings
    by_parent: dict[str | None, list[Section]] = {}
    for s in sections:
        if s.number is not None:
            by_parent.setdefault(s.parent_key, []).append(s)
    for sibs in by_parent.values():
        for prev, cur in zip(sibs, sibs[1:]):
            if number_tuple(cur.number) < number_tuple(prev.number):
                cur.anomalies.append({"type": "out_of_order",
                                      "detail": f"{cur.number} appears after {prev.number} in the document"})
                prev.anomalies.append({"type": "out_of_order",
                                       "detail": f"{prev.number} appears before {cur.number} in the document"})

    # duplicate headings are legal (4.2 and 7.1 are both "Error Codes"); note them for reviewers
    by_heading: dict[str, list[Section]] = {}
    for s in sections:
        by_heading.setdefault(s.heading.lower(), []).append(s)
    for group in by_heading.values():
        if len(group) > 1:
            for s in group:
                others = ", ".join(o.number or o.key for o in group if o is not s)
                s.anomalies.append({"type": "duplicate_heading",
                                    "detail": f"heading also used by section {others}"})


def _detect_references(sections: list[Section]) -> None:
    numbers = {s.number for s in sections if s.number}
    for s in sections:
        refs: list[str] = []
        for m in XREF_RE.finditer(s.body_text):
            refs.extend(XREF_NUM_RE.findall(m.group(1)))
        s.references = sorted(set(refs), key=number_tuple)
        for r in s.references:
            if r not in numbers:
                s.anomalies.append({"type": "dangling_reference", "detail": f"references missing section {r}"})


# --------------------------------------------------------------------------- OCR cross-check

def ocr_crosscheck(path: str | Path, dpi: int = 200) -> list[dict]:
    """OCR every page and report words the text layer does not contain.

    Used as a validation step: if the PDF had text baked into images (or our
    extraction dropped something) the OCR words would show up here.
    """
    doc = pymupdf.open(str(path))
    report = []
    for pno, page in enumerate(doc):
        layer = _words(normalize_text(page.get_text("text")))
        tp = page.get_textpage_ocr(full=True, dpi=dpi)
        ocr = _words(normalize_text(page.get_text("text", textpage=tp)))
        missing = sorted(w for w in ocr - layer if len(w) > 3)
        coverage = len(ocr & layer) / max(len(ocr), 1)
        report.append({"page": pno + 1, "ocr_words": len(ocr), "coverage": round(coverage, 3),
                       "ocr_only_words": missing[:30]})
    doc.close()
    return report


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)}
