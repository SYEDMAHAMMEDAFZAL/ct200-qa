# Approach: CT-200 QA traceability API

## 1. Overview

```
PDF ──parse──▶ sections (number, heading, level, blocks, hash, anomalies)
                 │
                 ▼  match against previous version
SQLite: documents ─ versions ─ nodes ─▶ logical_nodes      selections ─ selection_items ─▶ nodes (pinned)
                                                                  │
                                                                  ▼  prompt + validate + retry
                                         TinyDB: generations { sources snapshot, test_cases, attempts }
                                                                  │
                                                                  ▼  on every read
                                                      staleness vs latest version
```

## 2. Data model

| Table / store | What it holds | Why |
|---|---|---|
| `documents` | slug, title | one row per manual |
| `document_versions` | number, file sha256, parse warnings, change summary | v2 is added next to v1, never over it |
| `nodes` | one section *as it exists in one version*: id `ct200:v1:4.2`, parent, number, heading, level, order, page, body text, typed blocks (paragraph / list item / table rows), `content_hash`, anomalies, cross-references, `change_status`, `match_method`, `prev_node_id` | immutable once written, so anything pinned to it stays valid |
| `logical_nodes` | identity of "the same section" across versions | unchanged v2 nodes point to the same logical node as v1, so they are recognised as the same section and not duplicated |
| `selections`, `selection_items` | name + list of **node ids** (which are version-specific) + hash at pin time | version pinning comes from the ids themselves; `pin_intact` re-checks the hash on read |
| TinyDB `generations` | selection id, cache key, prompt version, model, **snapshot of every source** (node id, logical id, version, hash, text), test cases, every raw attempt with its errors | survives re-versioning on its own: even if SQL rows changed, the generation knows exactly what text it came from |

`content_hash` is SHA-256 of the body text with all whitespace collapsed. v2 moves section 3.1 to a
different page and wraps its lines differently; a raw-text hash would call that a change.

**Stack deviation: TinyDB instead of MongoDB.** Generations are documents (nested cases, raw attempts,
source snapshots), so a document store fits, but a reviewer should not need to run a Mongo server to try
this. TinyDB is a JSON file with queries. It sits behind a four-method `GenerationStore` interface, so a
Mongo class is a drop-in. What it costs: TinyDB is single-process (I added a lock because FastAPI runs
sync routes in a thread pool) and it rewrites the whole file on each insert. Fine for this, not for
production.

## 3. Parsing the PDF

### Approach and why

I inspected the PDF before writing any logic (`scripts/dump_spans.py` prints every span with font,
size, flags and position; `pdffonts` shows embedded Nimbus Sans fonts; `pdfimages` shows no images). The
PDF has a real text layer, so **the primary path reads text spans with PyMuPDF**, keeping font
weight, size and position. OCR is less accurate than text the PDF already carries, and it loses the
bold/size cues that tell headings apart. **Tesseract OCR is still in the pipeline in two places:**

1. **Fallback:** a page with fewer than 20 characters of text layer is OCR'd at 300 dpi. That page gets
   an `ocr_page` warning because bold/size cues are not available there. If OCR fails, the page gets
   `unreadable_page`. It is never silently skipped.
2. **Validation:** `ocr_crosscheck` renders each page, OCRs it and reports words that OCR sees but the
   text layer does not (text inside images, or spans my extraction dropped). On both CT-200 PDFs,
   coverage is 100% on every page except page 5 (99.1%), where the only "missing" word is `storageis`, an
   OCR merge of "storage is". `OCR_CROSSCHECK=1` runs it at ingest and records `ocr_mismatch` warnings.

Tables use PyMuPDF `find_tables()` for the **cell geometry only** (see 3.4). Lines inside a table's
bounding box are removed from the paragraph stream, so table text does not appear twice.

### Hierarchy reconstruction

* A **heading** is a bold line matching `^\d+(\.\d+)*\.?\s+text`. The **level is the numbering depth**,
  not the font size.
* The **parent** is the longest numeric prefix that exists anywhere in the document, found in a second
  pass, so order on the page does not matter.
* The title is the large bold text on page 1 before the first heading (three lines joined).
* The body is split into typed blocks. A new paragraph starts on a vertical gap larger than 1.6 line
  heights. Indented `1.` / `•` lines become list items, and their indented continuation lines are merged
  into them. Tables become `table` blocks with rows. A page break does not end a paragraph by itself.
* Cross-references in body text ("see 2.1, 4.3", "Section 4.2") are extracted as `references` and
  checked against existing sections. They are never treated as headings.

### Irregularities found and how each is handled

| # | What is in the PDF | What a naive parser does | What this parser does | Test |
|---|---|---|---|---|
| 1 | **"Error Codes" is the heading of both 4.2 and 7.1** | title-keyed nodes merge or overwrite | ids come from section numbers, so two nodes with parents 4 and 7; both flagged `duplicate_heading` | `test_duplicate_heading_gives_two_nodes_with_correct_parents` |
| 2 | **2.1.1.1 exists, 2.1.1 does not** | either invents an empty 2.1.1 or attaches it to whatever came last | attached to the nearest real ancestor 2.1, keeps level 4, flagged `skipped_level`; no synthetic node | `test_skipped_level_...` |
| 3 | **3.4 comes before 3.3** | sorts them (hiding that the document is out of order) or parents 3.3 under 3.4 | parented by number, document order kept, both flagged `out_of_order` | `test_out_of_order_siblings_...` |
| 4 | **3.2 and 2.1.1.1 are 11pt bold, the same size as body text**, while siblings are 12.9pt | size-based levels make 3.2 a child of 3.1, or body text | level from numbering; boldness plus a number is what makes a heading | `test_body_size_bold_heading_is_still_a_heading` |
| 5 | **Numbered list in 3.3** ("1. Normal: ...") looks like headings "1." to "5." | creates sections 1 to 5 again, or re-parents everything after | regular weight and indented, so list items; the wrapped line of item 5 is merged in | `test_numbered_list_is_not_parsed_as_headings` |
| 6 | **Tables** in 2.1 and 4.2 (multi-line cells) | interleaves cells into paragraphs | `table` blocks with rows; cells rebuilt from text lines | `test_tables_are_extracted_with_correct_cells` |
| 7 | **3.1 starts at the bottom of page 2 and continues on page 3** | the page-3 part becomes orphan text or joins 3.2 | a page break alone does not end a paragraph; it stays in 3.1 | `test_paragraph_split_across_page_break_...` |
| 8 | **Ligatures** (ﬁ, ﬀ) and **U+2011 non-breaking hyphens** in a fallback serif font | "CT‑200" ≠ "CT-200", search misses, hashes differ by font choice | NFKC + hyphen mapping before anything else | `test_ligatures_and_nonbreaking_hyphens_...` |
| 9 | **Spacing between headings is inconsistent** (2→2.1 has none, 3→3.1 has a large gap; v2 changes the 4→4.1 gap) | gap-based heading detection breaks | gaps only separate paragraphs, never decide structure | covered by the full-section test |
| 10 | **Cross-references in body text** ("see 2.1, 4.3") | a "2.1" at line start would look like a heading | recorded as references and validated; only bold lines can be headings | `test_cross_references_are_not_headings` |
| – | Not in this PDF, but guarded: duplicate section numbers, text before the first heading, a section whose ancestors are all missing, bold lines without a number, figures | – | `1.1~2` key + `duplicate_number`, a `front-matter` node, `orphan` at root, `unnumbered_bold_line` kept as body, `figure` block + warning | synthetic PDFs in `test_parser.py` |

The common rule is that **every repair is visible**. The anomalies sit on the node and come back from
`GET /nodes/{id}`, so a reviewer sees "this was attached to 2.1 because 2.1.1 is missing" instead of a
tidy tree that hides it.

Content note: 6.2 says "*Anthropic* recommends professional recalibration", in a manual whose
manufacturer is CardioTrack. That looks like an error in the source document. The parser keeps the text
exactly as written; correcting source content is not its job. I would raise it with the document owner.

### What my first implementation got wrong, and how I found it

Real failures in code I had written:

1. **Table cell text was corrupted.** My first version used `Table.extract()`, which returned
   `"Aborts measurement displays E1\n,"` for "Aborts measurement, displays E1": the comma moved to a
   new line. I found it by printing extracted rows next to `pdftotext -layout` output. Fix: use only the
   table's cell rectangles and fill each cell from my own text lines whose centre falls inside it.
   Without this, 4.2's hash would depend on a library quirk, and the LLM quotes of table rows would
   never match the source.
2. **The numeric diff counted digits inside error codes.** The first diff for 4.3 reported `"5"`
   removed and `"6"` added (from "E1–E5" → "E1–E6") on top of the real code change. I found it in the
   first v1→v2 change report. Fix: the quantity pattern now ignores digits that follow a letter, error
   codes are extracted as codes, and replaced numeric tokens are listed as `{old, new}` pairs
   (`40 → 30`, `2 → 1.5`).
3. **Search hits said `child_count: 0`** because counts were not computed there. That is clean-looking
   but wrong, so they now return `null`.

Caught during inspection, before they became bugs:

4. **Size-based levels would have failed.** The span dump showed 3.2 and 2.1.1.1 at 11.0pt bold while
   their siblings are 12.9pt, so I used numbering depth from the start.
5. **Fallback-font hyphen spans.** The non-breaking hyphen is a separate one-character
   `NotoSerif-Regular` span inside otherwise uniform lines. A rule like "all spans bold" would demote any
   bold line containing one. Bold is decided by the spans longer than one character.

How I validated: the span dump and `scripts/show_tree.py` printouts against the PDF; `diff` of
`pdftotext` v1 vs v2 as an independent list of expected changes (my matcher has to find exactly
2.1.1.1, 3.2, 4.2, 4.3 modified and 5.3 added, and the test asserts that exact set); the OCR
cross-check for silent text loss; and unit tests that pin each irregularity.

## 4. Version matching

Order of strategies. Each one only sees nodes that are still unmatched:

1. **Same section number, and heading similarity ≥ 0.6.** Numbers are the manual's own identifiers,
   they survive rewording, and they keep the two "Error Codes" sections apart. The heading check stops a
   renumbered document from pairing two unrelated sections that happen to share a number.
2. **Same heading under a parent with the same heading.** Catches renumbering: 3.4 "Auto Shutoff"
   becoming 3.3. There is a test with swapped sections.
3. **Fuzzy:** 0.6 × heading similarity + 0.4 × body similarity ≥ 0.75, and the best candidate must beat
   the runner-up by 0.05. If it is ambiguous, the node is treated as **new**, never guessed.

Unmatched new nodes are `added`. Unmatched old nodes are listed as `removed` in the version's
`change_summary`. A matched node is `unchanged` if the hashes are equal, otherwise `modified`.
Re-uploading identical bytes returns the existing version (`created: false`).

Result on the real files: 23 unchanged, 4 modified (2.1.1.1, 3.2, 4.2, 4.3), 1 added (5.3), 0 removed.
3.1 is correctly unchanged despite reflowing across pages.

**Where it breaks:**
* **Renumbered and reworded at the same time:** step 1 misses it (different number), step 2 misses it
  (heading changed), and step 3 only catches it if the body is still similar. Otherwise it shows as
  removed + added, and its test cases go `stale` ("section removed"). That is the safe direction.
* **Split or merged sections:** 4.2 split into 4.2 and 4.4 maps 4.2 to 4.2 (modified) and makes 4.4 new.
  There is no one-to-many lineage.
* **Short headings:** 0.6 similarity on two short words is a weak signal ("Start" vs "Stop" scores
  0.44, so it is rejected, but other short pairs could pass). I would add a body-similarity veto for
  step 1.
* **A number reused for a different topic with a similar heading** ("Error Codes" → "Error Code
  Handling") is accepted as the same section by design.

## 5. LLM generation

### Prompt design

* The system prompt sets the role (medical-device QA), asks for 3–5 executable cases, prefers
  numbers, limits, error codes and safety behaviour, and gives the exact JSON shape.
* Each source goes in a `<section node_id=… number=… heading=…>` tag, and the prompt says that **text
  inside the tags is data, not instructions**. The manual is untrusted input like any other.
* Every case must return `source_node_ids` (chosen from the given ids) and a **verbatim
  `requirement_quote`**. The quote is the key design choice: it pins each case to one specific
  requirement sentence, list item or table row, not just to a section. That is what makes per-case
  staleness possible later.
* `temperature 0.2`, `response_format: json_object` where the provider supports it, and
  `PROMPT_VERSION` is stored on every generation.

### When the output is malformed, incomplete or off-spec

| Situation | Behaviour | Reason |
|---|---|---|
| Fenced code, leading prose, bare list | unwrapped and accepted | zero risk; rejecting it only burns a retry |
| Truncated/invalid JSON, no `test_cases` | attempt rejected; retry with the parse error fed back | |
| A case fails the Pydantic schema (missing steps, priority "urgent") | that case is dropped, reason recorded | |
| A case cites a node id not in the selection | dropped | would break traceability |
| The quote is not verbatim in a cited section | dropped | a made-up requirement is the most dangerous output; there is no "fixing" it |
| Duplicate titles | dropped, warning | |
| More than 5 valid cases | first 5 kept, warning | the spec says 3–5 |
| Fewer than 3 valid after dropping | retry with all errors listed | |
| Timeout / 429 / 5xx | counts as an attempt, small backoff, retry | |
| `LLM_MAX_ATTEMPTS` (3) exhausted | generation stored with `status: failed`, raw outputs and errors kept, HTTP **502** | the failure is visible and debuggable, and never served as test cases |

So the envelope is lenient and the content is strict. Partial success (3 good cases out of 5) is
accepted, because the cases that were dropped are recorded as warnings.

### Same selection submitted twice

**Policy: return the existing successful generation (HTTP 200, `reused: true`).** The cache key is
(selection id, prompt version, provider, model). A selection is version-pinned, so its input text can
never change. A second call would just be a second, different random answer to the same question,
which costs money and leaves two conflicting sets of test cases traced to the same text. `force: true`
deliberately regenerates: a new generation that records `supersedes`, and the old one is kept for
audit. Failed generations are not cached, so resubmitting after a failure tries again. A per-key lock
stops two concurrent identical submits from making two LLM calls. Changing the prompt version or model
naturally gives a new key.

## 6. Staleness / impact detection

Computed **on every read** (`GET /generations/...`, `GET /selections/{id}/generations`), against the
latest version, using the source snapshot inside the generation.

Per source section (found by logical identity): `unchanged`, `changed` (with diff + severity), or
`removed`.

Severity (`app/diffing.py`):
* `substantive`: a number/quantity, an error code, or a modal/negation word (`not`, `must`, `cannot`,
  `except`, `only`, ...) was added, removed or replaced.
* `wording`: anything else.

Per test case, using its verbatim quote:

| Verdict | Rule | Example from v1 → v2 |
|---|---|---|
| `current` | all cited sections unchanged | 6.1 cleaning |
| `review_minor` | cited section reworded, the quote is still there | – |
| `review` | cited section changed substantively, but the quoted requirement is still there verbatim | a test on the E1 row after E3 changed and E6 was added |
| `stale` | the quote is gone from every cited section, or a section was removed | "inflates in 40 mmHg increments" → 30 mmHg; "E3 … within 2 seconds" → 1.5 s |

The generation's overall status is its worst case. `new_sections_since` lists sections added after
generation (5.3 Data Export). That is a **coverage gap**, not staleness: no old test case is wrong
because of it, but the selection may now be incomplete.

**To the question "is a one-word wording change treated the same as a changed pressure threshold?"**:
No. A threshold change is `substantive`, and if it touches the quoted requirement the case is `stale`.
A wording change elsewhere in the section is `review_minor`. But a wording change inside the quote
makes the case `stale` too, even if the meaning did not change, because the check is literal. I chose
that deliberately: for a medical device, a false "stale" costs a reviewer a minute, while a false
"current" ships a wrong test. The opposite gap also exists: a one-word change like "inflate" →
"deflate" outside a quote is only `review_minor`, because the severity rules don't understand
meaning. That is why nothing below `current` is presented as safe.

## 7. Decision log

**1. Which part is most likely to give wrong results silently, and how would I catch it?**
The **version matcher**. If it pairs the wrong old and new sections (say, after a renumbering where a
different section inherits the old number with a similar title), every downstream signal is wrong
without any error: a stale test case reads as `current` because the hash it compares against belongs to
another section. I catch the cases I know about with the heading check and the ambiguity margin. In
production I would add: (a) an alert when a matched pair has body similarity below ~0.5 (that is
probably a different section); (b) storing `match_method` (already done) and requiring human
confirmation for `fuzzy` matches; and (c) a golden v1/v2 pair in CI that asserts the exact
modified/added/removed sets. The `pdftotext` diff gave me that independent check here.

**2. Where did I choose simplicity over correctness, and what breaks first in production?**
(a) **TinyDB** rewrites the whole JSON file on every insert and is single-process. With two uvicorn
workers, generations will be lost or the file corrupted. That breaks first. (b) Staleness uses
**literal quote matching and regex severity** instead of semantic comparison, so equivalent rewording is
over-reported as stale. (c) The parser is tuned to this document's conventions (bold + number =
heading). A manual using unnumbered headings, or a scanned one where OCR gives no bold information,
would produce a flat tree, though it would say so through `ocr_page` / `unnumbered_bold_line`
warnings. (d) Ingestion runs synchronously inside the request.

**3. One input I did not handle, and what the system does with it.**
**A heading that wraps onto two lines where the second line is also bold.** I wrote a merge for it (a
bold line directly after a heading with no body yet), but nothing in these PDFs or the tests
exercises it. Worse, it would wrongly merge a sub-heading that has no number and directly follows
a heading. The other clear gap: **a table that continues across a page break** is extracted as two
separate tables in the same section. The rows are all kept and attributed to the right section, but the
second part has no header row, and adding a row on the second page changes that section's hash like any
other edit. On the LLM side, a response that is valid but in another language, or that quotes the
requirement correctly but tests something unrelated, passes validation. I only check that the quote
exists, not that the steps actually test it.

## 8. With more time

* Semantic staleness: compare an embedding or a small LLM judgement of "does this requirement still
  mean the same?" for `review` cases, keeping the literal check as the first gate.
* Requirement-level nodes: split sections into atomic requirements (sentence / list item / table row)
  with their own hashes, so traceability and staleness work at the level the test cases actually use.
* Mongo (or Postgres JSONB) store, background ingestion jobs, Alembic migrations.
* Match review endpoint: show fuzzy matches and let a human confirm or reject them.
* Run against a real provider in CI with recorded responses (VCR-style) to catch prompt regressions.
