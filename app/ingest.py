"""Ingest a parsed PDF as a new version and match its nodes to the previous version.

Matching strategy (in order, each step only sees still-unmatched nodes):

1. **Section number + heading check** - same number and heading similarity
   >= 0.6. Numbers are the manual's own identifiers and survive rewording,
   which a title-only match would not, and they disambiguate the duplicate
   "Error Codes" headings (4.2 vs 7.1).
2. **Heading path** - identical normalised heading *and* identical parent
   heading (catches a section that was renumbered, e.g. 3.4 -> 3.3).
3. **Fuzzy** - 0.6 * heading similarity + 0.4 * body similarity >= 0.75, with
   the best candidate required to beat the runner-up by 0.05 (ambiguity means
   "new", never a guess).

Anything left is ``new``; previous nodes left over are reported as removed.
A matched node is ``unchanged`` when its body hash is identical, otherwise
``modified``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.diffing import diff_summary, similarity
from app.models import Document, DocumentVersion, LogicalNode, Node
from app.parser import ParsedDocument, collapse_ws, ocr_crosscheck, parse_pdf

HEADING_MIN_SIM = 0.6
FUZZY_MIN = 0.75
FUZZY_MARGIN = 0.05


@dataclass
class IngestResult:
    version: DocumentVersion
    created: bool


def node_id(slug: str, version_number: int, key: str) -> str:
    return f"{slug}:v{version_number}:{key}"


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ingest_pdf(db: Session, path: str | Path, slug: str = "ct200",
               source_name: str | None = None, ocr_check: bool = False) -> IngestResult:
    path = Path(path)
    sha = file_sha256(path)
    doc = db.scalar(select(Document).where(Document.slug == slug))
    if doc is None:
        doc = Document(slug=slug)
        db.add(doc)
        db.flush()

    latest = latest_version(db, doc.id)
    if latest is not None and latest.file_sha256 == sha:
        # Re-uploading the same bytes is a no-op, not a new version.
        return IngestResult(latest, created=False)

    parsed = parse_pdf(path)
    if ocr_check:
        # Words OCR sees on the rendered page but the text layer does not have
        # (text in images, dropped spans). Reported, never merged in automatically.
        for page in ocr_crosscheck(path):
            if page["coverage"] < 0.98:
                parsed.warnings.append({"type": "ocr_mismatch", "page": page["page"],
                                        "detail": f"text layer covers {page['coverage']:.0%} of OCR words",
                                        "ocr_only_words": page["ocr_only_words"]})
    return IngestResult(store_parsed(db, doc, parsed, source_name or path.name, sha, latest), created=True)


def latest_version(db: Session, document_id: int) -> DocumentVersion | None:
    return db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document_id)
                     .order_by(DocumentVersion.version_number.desc()).limit(1))


def store_parsed(db: Session, doc: Document, parsed: ParsedDocument, source_name: str, sha: str,
                 prev_version: DocumentVersion | None) -> DocumentVersion:
    number = (prev_version.version_number + 1) if prev_version else 1
    version = DocumentVersion(document_id=doc.id, version_number=number, source_filename=source_name,
                              file_sha256=sha, title=parsed.title, page_count=parsed.page_count,
                              parse_warnings=parsed.warnings)
    db.add(version)
    db.flush()
    doc.title = parsed.title or doc.title

    prev_nodes: list[Node] = []
    if prev_version is not None:
        prev_nodes = list(db.scalars(select(Node).where(Node.version_id == prev_version.id)
                                     .order_by(Node.order_index)))
    matches = match_sections(parsed, prev_nodes)

    summary = {"added": [], "modified": [], "unchanged": [], "removed": [], "renumbered": []}
    key_to_id = {s.key: node_id(doc.slug, number, s.key) for s in parsed.sections}
    for s in parsed.sections:
        prev, method = matches.get(s.key, (None, None))
        if prev is None:
            logical = LogicalNode(document_id=doc.id, first_version_number=number, first_section_key=s.key)
            db.add(logical)
            db.flush()
            logical_id, status = logical.id, "new"
        else:
            logical_id = prev.logical_id
            status = "unchanged" if prev.content_hash == s.content_hash else "modified"
        nid = key_to_id[s.key]
        db.add(Node(
            id=nid, version_id=version.id, logical_id=logical_id,
            parent_id=key_to_id.get(s.parent_key) if s.parent_key else None,
            key=s.key, section_number=s.number, heading=s.heading, level=s.level, order_index=s.order,
            page=s.page, body_text=s.body_text, blocks=[b.to_dict() for b in s.blocks],
            content_hash=s.content_hash, anomalies=s.anomalies, references=s.references,
            change_status=status, match_method=method, prev_node_id=prev.id if prev else None,
        ))
        if prev_version is not None:
            summary["added" if status == "new" else status].append(nid)
            if prev is not None and prev.key != s.key:
                summary["renumbered"].append({"from": prev.id, "to": nid})
    matched_prev = {p.id for p, _ in matches.values() if p is not None}
    summary["removed"] = [p.id for p in prev_nodes if p.id not in matched_prev]
    version.change_summary = summary if prev_version is not None else {}
    db.flush()
    return version


def _parent_heading(sec_parent_key: str | None, headings: dict[str, str]) -> str:
    return collapse_ws(headings.get(sec_parent_key or "", "")).lower()


def match_sections(parsed: ParsedDocument, prev_nodes: list[Node]) -> dict[str, tuple[Node | None, str | None]]:
    result: dict[str, tuple[Node | None, str | None]] = {}
    if not prev_nodes:
        return result
    unmatched_prev = {p.id: p for p in prev_nodes}
    prev_by_key = {p.key: p for p in prev_nodes}
    prev_headings = {p.id: p.heading for p in prev_nodes}
    new_headings = {s.key: s.heading for s in parsed.sections}

    # 1. same number, heading still plausibly the same
    for s in parsed.sections:
        p = prev_by_key.get(s.key)
        if p is not None and p.id in unmatched_prev and similarity(p.heading, s.heading) >= HEADING_MIN_SIM:
            result[s.key] = (p, "section_number")
            del unmatched_prev[p.id]

    # 2. identical heading under an identically named parent
    for s in parsed.sections:
        if s.key in result:
            continue
        want = (collapse_ws(s.heading).lower(), _parent_heading(s.parent_key, new_headings))
        cands = [p for p in unmatched_prev.values()
                 if (collapse_ws(p.heading).lower(), collapse_ws(prev_headings.get(p.parent_id or "", "")).lower())
                 == want]
        if len(cands) == 1:
            result[s.key] = (cands[0], "heading_path")
            del unmatched_prev[cands[0].id]

    # 3. fuzzy, only if unambiguous
    for s in parsed.sections:
        if s.key in result:
            continue
        scored = sorted(((0.6 * similarity(p.heading, s.heading) + 0.4 * similarity(p.body_text, s.body_text), p)
                         for p in unmatched_prev.values()), key=lambda t: t[0], reverse=True)
        if scored and scored[0][0] >= FUZZY_MIN and (len(scored) == 1 or scored[0][0] - scored[1][0] >= FUZZY_MARGIN):
            result[s.key] = (scored[0][1], "fuzzy")
            del unmatched_prev[scored[0][1].id]
        else:
            result[s.key] = (None, None)
    return result


def node_history(db: Session, node: Node) -> list[Node]:
    """All versions of the same logical node, oldest first."""
    return list(db.scalars(select(Node).join(DocumentVersion, Node.version_id == DocumentVersion.id)
                           .where(Node.logical_id == node.logical_id)
                           .order_by(DocumentVersion.version_number)))


def change_report(db: Session, node: Node) -> dict:
    history = node_history(db, node)
    versions = {n.id: n.version.version_number for n in history}
    steps = []
    for older, newer in zip(history, history[1:]):
        d = diff_summary(older.body_text, newer.body_text)
        steps.append({
            "from_node_id": older.id, "to_node_id": newer.id,
            "from_version": versions[older.id], "to_version": versions[newer.id],
            "heading_changed": older.heading != newer.heading,
            "renumbered": older.key != newer.key,
            "content_changed": older.content_hash != newer.content_hash,
            "match_method": newer.match_method,
            "diff": d,
        })
    latest_doc_version = latest_version(db, node.version.document_id)
    last = history[-1]
    return {
        "node_id": node.id,
        "logical_id": node.logical_id,
        "present_in_versions": [versions[n.id] for n in history],
        "latest_document_version": latest_doc_version.version_number if latest_doc_version else None,
        "removed_in_latest": last.version_id != (latest_doc_version.id if latest_doc_version else None),
        "changed_across_versions": any(st["content_changed"] or st["heading_changed"] for st in steps),
        "steps": steps,
    }
