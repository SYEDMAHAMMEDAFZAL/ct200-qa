"""End-to-end demo of the versioning + staleness flow.

    python -m scripts.demo_flow                       # against a running server on :8000
    python -m scripts.demo_flow --base http://host:port
    python -m scripts.demo_flow --inprocess           # no server needed, fresh temp database

Steps: ingest v1 -> browse -> select sections -> generate test cases ->
submit again (dedupe) -> ingest v2 -> old selection still pinned to v1 text ->
retrieve generation with staleness -> look up by the v2 node id -> node diff.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def step(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(obj, keys=None) -> None:
    if keys:
        obj = {k: obj.get(k) for k in keys}
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str)[:3000])


def check(r: httpx.Response, *ok: int) -> dict | list:
    if r.status_code not in ok:
        print(f"!! {r.request.method} {r.request.url} -> {r.status_code}\n{r.text[:1000]}")
        sys.exit(1)
    return r.json()


def run(c) -> None:
    step("1. Ingest v1 (data/ct200_manual.pdf)")
    v1 = check(c.post("/documents/ct200/versions", params={"path": "data/ct200_manual.pdf"}), 201)
    show(v1["version"], ["version_number", "source_filename", "page_count", "title"])
    if not v1["created"]:
        print("(already ingested - this database was used before; use --inprocess for a clean run)")

    step("2. Top-level sections of the latest version, and node 2.1 (skipped level 2.1.1)")
    for s in check(c.get("/documents/ct200/sections"), 200):
        print(f"  {s['id']:<18} {s['section_number']:>3} {s['heading']}  ({s['child_count']} children)")
    n = check(c.get("/nodes/ct200:v1:2.1.1.1"), 200)
    show(n, ["id", "heading", "level", "parent_id", "breadcrumb", "content_hash", "anomalies"])

    step("3. Search headings for 'Error Codes' (duplicate heading -> two distinct nodes)")
    for h in check(c.get("/search", params={"q": "Error Codes", "field": "heading"}), 200):
        print(f"  {h['id']}  parent section {h['id'].rsplit(':', 1)[1].split('.')[0]}")

    step("4. Create a selection pinned to v1: 3.2 inflation, 4.2 error codes, 6.1 cleaning")
    sel = check(c.post("/selections", json={"name": "Inflation, errors, cleaning",
                                            "node_ids": ["ct200:v1:3.2", "ct200:v1:4.2", "ct200:v1:6.1"]}), 201)
    print(f"  selection id: {sel['id']}  versions: {sel['versions']}")

    step("5. Generate test cases")
    gen = check(c.post(f"/selections/{sel['id']}/generations"), 201, 200)
    print(f"  generation {gen['id']} status={gen['status']} model={gen['provider']}/{gen['model']} "
          f"attempts={gen['attempt_count']} staleness={gen['staleness']['status']}")
    for tc in gen["test_cases"]:
        print(f"  {tc['id']} [{tc['priority']}] {tc['title']}\n      quote: {tc['requirement_quote']!r}"
              f"\n      from: {tc['source_node_ids']}")

    step("6. Submit the same selection again (policy: reuse, no second LLM call)")
    again = check(c.post(f"/selections/{sel['id']}/generations"), 200, 201)
    print(f"  reused={again['reused']} same id={again['id'] == gen['id']}")

    step("7. Ingest v2 (data/ct200_manual_v2.pdf) - v1 is kept")
    v2 = check(c.post("/documents/ct200/versions", params={"path": "data/ct200_manual_v2.pdf"}), 201)
    show(v2["version"]["change_summary"], ["added", "modified", "removed", "renumbered"])
    print(f"  unchanged: {len(v2['version']['change_summary'].get('unchanged', []))} nodes")

    step("8. The old selection still resolves to the exact v1 text")
    pinned = check(c.get(f"/selections/{sel['id']}"), 200)
    for it in pinned["items"]:
        print(f"  {it['node_id']:<16} pin_intact={it['pin_intact']}  latest: {it['latest']['status']}"
              f" / {it['latest']['severity']}")
    print("  3.2 pinned text:", next(i for i in pinned["items"] if i["node_id"] == "ct200:v1:3.2")["pinned_text"][:140])

    step("9. Retrieve the generation: staleness is computed against v2")
    got = check(c.get(f"/generations/{gen['id']}"), 200)
    st = got["staleness"]
    print(f"  overall: {st['status']} (generated from v{st['generated_from_versions']}, "
          f"checked against v{st['checked_against_version']})")
    for s in st["sources"]:
        extra = ""
        if s.get("diff"):
            nc = s["diff"]["numeric_changes"]
            extra = f" replaced={[(r['old'], r['new']) for r in nc['replaced']]} added={nc['added']}"
        print(f"  source {s['node_id']:<16} {s['status']:<9} {s['severity']}{extra}")
    for tc in st["test_cases"]:
        print(f"  {tc['test_case_id']}: {tc['status']:<12} {tc['reason']}")
    print(f"  new sections since generation (coverage gap, not staleness): {st['new_sections_since']}")

    step("10. Look up generations by the v2 id of section 3.2 (same logical node)")
    by_node = check(c.get("/generations", params={"node_id": "ct200:v2:3.2"}), 200)
    print(f"  found {[g['id'] for g in by_node]} -> staleness {[g['staleness']['status'] for g in by_node]}")

    step("11. Change report for node 3.2")
    ch = check(c.get("/nodes/ct200:v1:3.2/changes"), 200)
    d = ch["steps"][0]["diff"]
    show({"changed_across_versions": ch["changed_across_versions"], "severity": d["severity"],
          "numeric_changes": d["numeric_changes"], "changes": d["changes"][:3]})
    print("\nDemo finished.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--inprocess", action="store_true")
    args = ap.parse_args()
    if args.inprocess:
        from fastapi.testclient import TestClient

        from app.config import get_settings
        from app.main import create_app
        tmp = tempfile.mkdtemp()
        s = get_settings()
        s = s.__class__(**{**s.__dict__, "database_url": f"sqlite:///{tmp}/demo.db",
                           "generation_store_path": f"{tmp}/generations.json"})
        with TestClient(create_app(s)) as c:
            run(c)
    else:
        with httpx.Client(base_url=args.base, timeout=120) as c:
            run(c)


if __name__ == "__main__":
    main()
