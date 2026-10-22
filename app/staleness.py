"""Is a stored generation still true to the current document?

Computed at read time, never stored, so it is always relative to whatever
the latest ingested version is.

Per source section (by logical identity):
  unchanged  - same body hash in the latest version
  changed    - hash differs; diff severity is ``wording`` or ``substantive``
  removed    - the logical section no longer exists

Per test case, using the sections it cites and its verbatim requirement quote:
  current        - every cited section is unchanged
  review_minor   - a cited section changed only in wording and the quote is still there
  review         - a cited section changed substantively but the quoted requirement
                   is still present verbatim (e.g. E1 row intact while E3 changed)
  stale          - the quoted requirement is no longer in any cited section, or a
                   cited section was removed

Limits (also in docs/APPROACH.md): the quote check is literal, so a reworded
but equivalent requirement shows as ``stale``; and a requirement added
*elsewhere* (new section 5.3) cannot make an old test case stale - that is
a coverage gap, reported separately as ``new_sections_since``.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.diffing import diff_summary
from app.generation import quote_in
from app.ingest import latest_version
from app.models import Document, Node

RANK = {"current": 0, "review_minor": 1, "review": 2, "stale": 3}


def assess(db: Session, generation: dict) -> dict:
    sources = generation.get("sources", [])
    if not sources:
        return {"status": "unknown", "reason": "generation has no source snapshot"}
    doc = db.scalar(select(Document).where(Document.slug == sources[0]["document"]))
    latest = latest_version(db, doc.id) if doc else None
    if latest is None:
        return {"status": "unknown", "reason": "document no longer exists"}

    src_status: dict[str, dict] = {}
    for s in sources:
        cur = db.scalar(select(Node).where(Node.version_id == latest.id, Node.logical_id == s["logical_id"]))
        if cur is None:
            src_status[s["node_id"]] = {"status": "removed", "severity": "removed", "latest_node_id": None,
                                        "latest_text": None}
        elif cur.content_hash == s["content_hash"]:
            src_status[s["node_id"]] = {"status": "unchanged", "severity": "none", "latest_node_id": cur.id,
                                        "latest_text": cur.body_text}
        else:
            d = diff_summary(s["text"], cur.body_text)
            src_status[s["node_id"]] = {"status": "changed", "severity": d["severity"], "latest_node_id": cur.id,
                                        "latest_text": cur.body_text, "diff": d}

    case_reports = []
    for tc in generation.get("test_cases", []):
        cited = [src_status[n] for n in tc["source_node_ids"] if n in src_status]
        if all(c["status"] == "unchanged" for c in cited):
            verdict, reason = "current", "all cited sections unchanged"
        elif any(c["status"] == "removed" for c in cited):
            verdict, reason = "stale", "a cited section was removed from the document"
        else:
            still_there = any(c["latest_text"] and quote_in(tc["requirement_quote"], c["latest_text"]) for c in cited)
            worst = "substantive" if any(c["severity"] == "substantive" for c in cited) else "wording"
            if not still_there:
                verdict, reason = "stale", "the quoted requirement no longer appears in the current text"
            elif worst == "substantive":
                verdict, reason = "review", "cited section changed substantively, quoted requirement still present"
            else:
                verdict, reason = "review_minor", "cited section reworded, quoted requirement still present"
        case_reports.append({"test_case_id": tc["id"], "status": verdict, "reason": reason})

    overall = max((c["status"] for c in case_reports), key=RANK.get, default="current")
    source_nodes = {s["logical_id"] for s in sources}
    gen_versions = {s["version"] for s in sources}
    new_since = [n.id for n in db.scalars(select(Node).where(Node.version_id == latest.id,
                                                             Node.change_status == "new"))
                 if latest.version_number > max(gen_versions) and n.logical_id not in source_nodes]
    return {
        "status": overall,
        "checked_against_version": latest.version_number,
        "generated_from_versions": sorted(gen_versions),
        "sources": [{"node_id": nid, **{k: v for k, v in st.items() if k != "latest_text"}}
                    for nid, st in src_status.items()],
        "test_cases": case_reports,
        "new_sections_since": new_since,
    }
