"""Read-side queries over the stored tree, plus selection creation."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.diffing import diff_summary
from app.ingest import latest_version
from app.models import Document, DocumentVersion, Node, Selection, SelectionItem
from app.parser import content_hash


def get_document(db: Session, slug: str) -> Document:
    doc = db.scalar(select(Document).where(Document.slug == slug))
    if doc is None:
        raise HTTPException(404, f"document {slug!r} not found")
    return doc


def resolve_version(db: Session, doc: Document, version: str | int | None) -> DocumentVersion:
    if version in (None, "", "latest"):
        v = latest_version(db, doc.id)
    else:
        try:
            num = int(version)
        except (TypeError, ValueError):
            raise HTTPException(422, "version must be an integer or 'latest'")
        v = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                                    DocumentVersion.version_number == num))
    if v is None:
        raise HTTPException(404, f"version {version!r} of {doc.slug!r} not found")
    return v


def get_node(db: Session, node_id: str) -> Node:
    node = db.get(Node, node_id)
    if node is None:
        raise HTTPException(404, f"node {node_id!r} not found")
    return node


def child_counts(db: Session, version_id: int) -> dict[str, int]:
    rows = db.execute(select(Node.parent_id, func.count()).where(Node.version_id == version_id)
                      .group_by(Node.parent_id))
    return {pid: n for pid, n in rows if pid}


def summary(node: Node, counts: dict[str, int] | None = None) -> dict:
    return {
        "id": node.id, "section_number": node.section_number, "heading": node.heading, "level": node.level,
        "page": node.page, "content_hash": node.content_hash, "change_status": node.change_status,
        "child_count": (counts or {}).get(node.id, 0), "anomalies": node.anomalies,
    }


def top_level(db: Session, version: DocumentVersion) -> list[dict]:
    counts = child_counts(db, version.id)
    nodes = db.scalars(select(Node).where(Node.version_id == version.id, Node.parent_id.is_(None))
                       .order_by(Node.order_index))
    return [summary(n, counts) for n in nodes]


def children(db: Session, node: Node) -> list[Node]:
    return list(db.scalars(select(Node).where(Node.parent_id == node.id).order_by(Node.order_index)))


def breadcrumb(db: Session, node: Node) -> list[str]:
    trail, cur = [], node
    while cur.parent_id:
        cur = db.get(Node, cur.parent_id)
        trail.append(f"{cur.section_number or ''} {cur.heading}".strip())
    return list(reversed(trail))


def node_detail(db: Session, node: Node) -> dict:
    counts = child_counts(db, node.version_id)
    d = summary(node, counts)
    d.update({
        "document": node.version.document.slug, "version": node.version.version_number,
        "parent_id": node.parent_id, "breadcrumb": breadcrumb(db, node), "body_text": node.body_text,
        "blocks": node.blocks, "references": node.references, "logical_id": node.logical_id,
        "match_method": node.match_method, "prev_node_id": node.prev_node_id,
        "children": [summary(c, counts) for c in children(db, node)],
    })
    return d


def tree(db: Session, version: DocumentVersion) -> list[dict]:
    nodes = list(db.scalars(select(Node).where(Node.version_id == version.id).order_by(Node.order_index)))
    by_parent: dict[str | None, list[Node]] = {}
    for n in nodes:
        by_parent.setdefault(n.parent_id, []).append(n)

    def build(n: Node) -> dict:
        d = summary(n, {k: len(v) for k, v in by_parent.items() if k})
        d["children"] = [build(c) for c in by_parent.get(n.id, [])]
        return d

    return [build(n) for n in by_parent.get(None, [])]


def search(db: Session, doc: Document, version: DocumentVersion | None, q: str, field: str,
           limit: int) -> list[dict]:
    like = f"%{q}%"
    conds = []
    if field in ("heading", "both"):
        conds.append(Node.heading.ilike(like))
    if field in ("text", "both"):
        conds.append(Node.body_text.ilike(like))
    stmt = (select(Node).join(DocumentVersion, Node.version_id == DocumentVersion.id)
            .where(DocumentVersion.document_id == doc.id, or_(*conds)))
    if version is not None:
        stmt = stmt.where(Node.version_id == version.id)
    stmt = stmt.order_by(DocumentVersion.version_number.desc(), Node.order_index).limit(limit)
    hits = []
    ql = q.lower()
    for n in db.scalars(stmt):
        matched = [f for f, txt in (("heading", n.heading), ("text", n.body_text)) if ql in txt.lower()]
        i = n.body_text.lower().find(ql)
        snippet = n.body_text[max(0, i - 60): i + len(q) + 60] if i >= 0 else n.body_text[:120]
        d = summary(n)
        d.update({"version": n.version.version_number, "matched_in": matched, "snippet": snippet})
        hits.append(d)
    return hits


def latest_counterpart(db: Session, node: Node) -> tuple[Node | None, DocumentVersion]:
    latest = latest_version(db, node.version.document_id)
    other = db.scalar(select(Node).where(Node.version_id == latest.id, Node.logical_id == node.logical_id))
    return other, latest


def latest_status(db: Session, node: Node) -> dict:
    """How the pinned node compares with the same logical section in the latest version."""
    other, latest = latest_counterpart(db, node)
    base = {"latest_version": latest.version_number}
    if other is None:
        return {**base, "status": "removed", "node_id": None, "severity": "removed"}
    if other.id == node.id:
        return {**base, "status": "current", "node_id": other.id, "severity": "none"}
    if other.content_hash == node.content_hash:
        return {**base, "status": "unchanged", "node_id": other.id, "severity": "none",
                "heading_changed": other.heading != node.heading}
    d = diff_summary(node.body_text, other.body_text)
    return {**base, "status": "changed", "node_id": other.id, "severity": d["severity"],
            "heading_changed": other.heading != node.heading, "diff": d}


# ------------------------------------------------------------------ selections

def _subtree(db: Session, node: Node) -> list[Node]:
    out = [node]
    for c in children(db, node):
        out.extend(_subtree(db, c))
    return out


def create_selection(db: Session, name: str, node_ids: list[str], include_descendants: bool) -> tuple[Selection, bool]:
    """Create a version-pinned selection.

    Policy: the same name + same pinned node set returns the existing
    selection (idempotent create); a different name is a different selection.
    """
    nodes: list[Node] = []
    seen = set()
    for nid in node_ids:
        n = get_node(db, nid)
        for m in (_subtree(db, n) if include_descendants else [n]):
            if m.id not in seen:
                seen.add(m.id)
                nodes.append(m)
    docs = {n.version.document_id for n in nodes}
    if len(docs) > 1:
        raise HTTPException(422, "a selection must reference a single document")
    fingerprint = hashlib.sha256(("\n".join([name] + sorted(seen))).encode()).hexdigest()
    existing = db.scalar(select(Selection).where(Selection.fingerprint == fingerprint))
    if existing is not None:
        return existing, False
    sel = Selection(id=str(uuid.uuid4()), name=name, document_id=docs.pop(), fingerprint=fingerprint)
    sel.items = [SelectionItem(node_id=n.id, position=i, content_hash=n.content_hash) for i, n in enumerate(nodes)]
    db.add(sel)
    db.flush()
    return sel, True


def get_selection(db: Session, selection_id: str) -> Selection:
    sel = db.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(404, f"selection {selection_id!r} not found")
    return sel


def selection_detail(db: Session, sel: Selection) -> dict:
    items = []
    versions = set()
    for it in sel.items:
        n = it.node
        versions.add(n.version.version_number)
        items.append({
            "node_id": n.id, "version": n.version.version_number, "section_number": n.section_number,
            "heading": n.heading, "content_hash": it.content_hash, "pinned_text": n.body_text,
            "pin_intact": content_hash(n.body_text) == it.content_hash,
            "latest": latest_status(db, n),
        })
    warnings = []
    if len(versions) > 1:
        warnings.append(f"selection mixes document versions {sorted(versions)}")
    if any(not i["pinned_text"].strip() for i in items):
        warnings.append("some selected nodes have no body text of their own (use include_descendants)")
    doc = db.get(Document, sel.document_id)
    return {"id": sel.id, "name": sel.name, "document": doc.slug, "created_at": sel.created_at,
            "versions": sorted(versions), "warnings": warnings, "items": items}
