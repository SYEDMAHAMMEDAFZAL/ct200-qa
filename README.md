# CT-200 QA traceability API

Backend that turns the CardioTrack CT-200 manual (PDF) into a versioned section tree, generates QA
test-case ideas from selected sections with an LLM, and tells you at retrieval time whether those test
cases still match the current version of the manual.

Design decisions, the parser's edge cases, the matching strategy and the decision log are in
[docs/APPROACH.md](docs/APPROACH.md).

## Stack

FastAPI · Pydantic v2 · SQLAlchemy 2 + SQLite (documents, versions, nodes, selections) ·
TinyDB JSON store (LLM generations; see the approach doc for why not MongoDB) · PyMuPDF for the PDF text
layer with Tesseract OCR for fallback/cross-check · any OpenAI-compatible LLM (Groq, OpenRouter, Gemini),
or an offline mock.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # defaults work offline with the mock LLM
```

Tesseract is optional. It is only needed for pages without a text layer and for `OCR_CROSSCHECK=1`
(`apt install tesseract-ocr` / `brew install tesseract`). The CT-200 PDFs have a text layer, so
everything else works without it.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///var/ct200.db` | SQLAlchemy URL for the tree, versions and selections |
| `GENERATION_STORE_PATH` | `var/generations.json` | TinyDB file for generations (`:memory:` for tests) |
| `LLM_PROVIDER` | `mock` | `mock` (offline, deterministic) or `openai_compat` |
| `LLM_BASE_URL` | Groq endpoint | e.g. `https://api.groq.com/openai/v1`, `https://openrouter.ai/api/v1`, `https://generativelanguage.googleapis.com/v1beta/openai` |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | model name at that provider |
| `LLM_API_KEY` | – | required for `openai_compat` |
| `LLM_MAX_ATTEMPTS` | `3` | attempts per generation (validation failures and transport errors both count) |
| `MOCK_LLM_FAILURES` | – | comma list to make the mock misbehave, e.g. `malformed,bad_quote` |
| `OCR_CROSSCHECK` | `0` | `1` = OCR every page at ingest and warn where the text layer misses words |

## Run

```bash
uvicorn app.main:create_app --factory --reload
# Swagger UI: http://127.0.0.1:8000/docs
```

## Test

```bash
pytest -q
```

32 tests. `tests/test_parser.py` covers the PDF irregularities (duplicate heading → two nodes with the
right parents, skipped level 2.1.1, 3.4 before 3.3, body-size bold heading 3.2, numbered list that is not
headings, tables, page-break paragraph, reflow-insensitive hashes, duplicate section numbers, orphans).
`tests/test_versioning.py` covers matching and diffs, `tests/test_generation.py` covers output
validation, and `tests/test_api.py` runs the whole flow through the API, including LLM failures.

## The v1 → v2 re-ingestion flow

The quickest way to see all of it is the demo script (sample output in
[docs/demo_output.txt](docs/demo_output.txt)):

```bash
python -m scripts.demo_flow --inprocess    # fresh temp database, no server needed
python -m scripts.demo_flow                # against a server running on :8000 (use a fresh var/ folder)
```

The same flow with curl:

```bash
B=http://127.0.0.1:8000
# 1. ingest v1
curl -X POST "$B/documents/ct200/versions?path=data/ct200_manual.pdf"
#    (or upload: curl -X POST -F file=@data/ct200_manual.pdf $B/documents/ct200/versions)
# 2. browse
curl "$B/documents/ct200/sections"                 # latest version by default; ?version=1
curl "$B/nodes/ct200:v1:4.2"                       # children, full text, blocks, content hash, anomalies
curl "$B/search?q=mmHg&field=text&version=all"
# 3. pin a selection to v1 and generate test cases
SEL=$(curl -s -X POST $B/selections -H 'content-type: application/json' \
  -d '{"name":"inflation + errors","node_ids":["ct200:v1:3.2","ct200:v1:4.2"]}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
GEN=$(curl -s -X POST $B/selections/$SEL/generations | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -X POST $B/selections/$SEL/generations        # same selection again -> 200, "reused": true
# 4. re-ingest the modified manual as version 2 (v1 is kept)
curl -X POST "$B/documents/ct200/versions?path=data/ct200_manual_v2.pdf"   # change_summary lists modified/added/removed
# 5. see the impact
curl "$B/selections/$SEL"                          # still the exact v1 text, plus where each section stands in v2
curl "$B/generations/$GEN"                         # "staleness": per source section and per test case
curl "$B/generations?node_id=ct200:v2:3.2"         # v2 id finds generations made from v1 (same logical node)
curl "$B/nodes/ct200:v1:3.2/changes"               # diff summary: 40 -> 30 mmHg, severity substantive
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/documents/{slug}/versions` | ingest a PDF (`file` upload or `?path=`) as the next version; identical bytes → `created: false` |
| GET | `/documents`, `/documents/{slug}/versions` | documents and their versions with parse warnings and change summary |
| GET | `/documents/{slug}/sections?version=` | top-level sections (default latest) |
| GET | `/documents/{slug}/tree?version=` | full tree |
| GET | `/nodes/{node_id}` | node with children, full text, blocks, content hash, anomalies, breadcrumb |
| GET | `/nodes/{node_id}/changes` | has the section changed across versions + diff summary and severity |
| GET | `/search?q=&field=heading\|text\|both&version=N\|latest\|all` | search headings/text |
| POST | `/selections` | named, version-pinned selection (`include_descendants` expands subtrees) |
| GET | `/selections/{id}` | pinned text + current status of each section |
| POST | `/selections/{id}/generations` | generate 3–5 test cases (`{"force": true}` to regenerate) |
| GET | `/selections/{id}/generations`, `/generations?selection_id=\|node_id=`, `/generations/{id}` | retrieval, always with `staleness` |

Node ids are readable: `ct200:v1:4.2` is section 4.2 of version 1. Duplicate section numbers get a
suffix (`1.1~2`).

## Layout

```
app/parser.py         PDF -> sections (text layer + OCR fallback), anomaly flags
app/ingest.py         versions, cross-version matching, change reports
app/diffing.py        diff summary + severity (numbers/codes/negations)
app/browse.py         read queries, selections
app/generation.py     prompt, output validation, retry, duplicate policy
app/staleness.py      retrieval-time staleness
app/llm.py            mock + OpenAI-compatible clients
app/store.py          TinyDB generation store
scripts/              demo_flow, show_tree, dump_spans, ocr_check
```
