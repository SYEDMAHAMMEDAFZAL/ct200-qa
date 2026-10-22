# Approach Document: CT-200 QA Traceability API

S. Md. Afzal · Tri9T AI Engineering Internship Assignment
Repo: https://github.com/SYEDMAHAMMEDAFZAL/ct200-qa

## 1. What I built

A FastAPI backend that:

1. reads the CT-200 manual PDF and turns it into a tree of sections,
2. stores each new version of the manual next to the old ones,
3. lets you browse, search and compare sections,
4. saves a "selection" of sections pinned to one version,
5. asks an LLM for 3 to 5 QA test cases for a selection and checks the answer,
6. tells you later whether those test cases are still valid after the manual changes.

Stack: FastAPI, Pydantic, SQLAlchemy with SQLite, TinyDB for the LLM output, PyMuPDF for the PDF,
Tesseract for OCR, and Groq (Llama 3.3) as the LLM. There is also a mock LLM so everything runs offline.

## 2. Data model

- **documents**: one row per manual.
- **document_versions**: v1, v2 and so on. A new upload adds a version and never overwrites the old one.
  Uploading the exact same file again does nothing.
- **nodes**: one section in one version. The id is readable, for example `ct200:v1:4.2`. Each node
  stores the heading, level, parent, body text, blocks (paragraphs, list items, table rows), a content
  hash and any problems the parser noticed. Nodes are never edited after they are saved.
- **logical_nodes**: links "the same section" across versions. If 3.1 did not change in v2, both
  versions point to the same logical node, so it is not counted as a new section.
- **selections**: a name and a list of node ids. A node id already includes the version, so a selection
  always points to the exact text it was made from, even after v2 is loaded.
- **generations (TinyDB)**: the test cases, plus a copy of the source text they were made from, plus
  every raw LLM answer and its errors.

The content hash is SHA-256 of the body text with all whitespace squeezed to single spaces. I did this
because in v2, section 3.1 moves to another page and its lines wrap differently even though the words
are the same. Hashing the raw text would have reported a false change.

**Why TinyDB and not MongoDB:** the LLM output is naturally a JSON document, but I did not want
reviewers to have to install and run a Mongo server just to try the project. TinyDB is a single JSON
file. All access goes through a small `GenerationStore` class, so swapping in Mongo later means
writing one new class. The downside is that TinyDB only works for one process and rewrites the whole
file on every insert.

## 3. Parsing the PDF

### How I approached it

Before writing any parser code I dumped every piece of text in the PDF with its font, size and position
(`scripts/dump_spans.py`). That showed me the PDF has real text inside it, not scanned images. So I read
the text directly with PyMuPDF. That is more accurate than OCR, and it keeps the bold and size
information I need to find headings.

I still use OCR (Tesseract) in two places:
- **fallback**: if a page has almost no text, it gets OCR'd and marked with a warning.
- **check**: `scripts/ocr_check.py` OCRs every page and lists words that OCR can see but my text
  extraction missed. On both PDFs it found nothing missing (one page shows 99%, and the only
  difference is OCR joining "storage is" into "storageis").

### How I build the tree

- A heading is a **bold line that starts with a section number** such as `3.2` or `2.1.1.1`.
- The level comes from the **number** (`2.1.1.1` is level 4), not the font size.
- The parent is the closest number above it that actually exists.
- Normal text is split into paragraphs, list items and tables.
- Tables are found with PyMuPDF, and their text is taken out of the normal paragraphs.

### Problems in this PDF and what I did with each

1. **Two sections called "Error Codes"** (4.2 and 7.1). Ids come from section numbers, not titles,
   so they are two separate nodes under 4 and 7.
2. **2.1.1.1 exists but 2.1.1 does not.** I attach it to 2.1, keep its level as 4, and add a
   `skipped_level` warning. I do not make up an empty 2.1.1.
3. **3.4 comes before 3.3.** I keep the order they appear in, parent both under 3, and flag both
   as `out_of_order`.
4. **3.2 and 2.1.1.1 use the same font size as body text** (11pt bold, while other headings are
   12.9pt). This is why I use the number for the level, not the font size.
5. **The numbered list in 3.3** ("1. Normal ...") looks like headings. These lines are not bold and
   are indented, so they become list items. The wrapped second line of item 5 is joined back onto it.
6. **Tables** in 2.1 and 4.2 with cells that wrap over two lines. They are stored as table rows.
7. **Section 3.1 continues onto the next page.** A page break alone does not start a new paragraph,
   so the text stays together in 3.1.
8. **Special characters**: "fi" and "ff" ligatures, and a special non-breaking hyphen in a different
   font. I normalise these so "CT-200" is always spelled the same way.
9. **References like "see 2.1, 4.3"** inside text. These are saved as references, not treated as headings.

Other cases I guard against even though this PDF does not have them: a repeated section number (gets
id `1.1~2`), text before the first heading, a section with no parent at all, and bold lines without a
number. Each one is kept and flagged, never dropped.

My main rule: **if the parser has to guess, it says so.** Every warning is stored on the node and
returned by the API, so nobody gets a clean-looking tree that is actually wrong.

One more thing I noticed: section 6.2 says "*Anthropic* recommends professional recalibration", but the
manufacturer is CardioTrack. That looks like a mistake in the document itself. I kept the text as it is,
because fixing source content is not the parser's job.

### What went wrong at first and how I found it

1. **Table text came out broken.** PyMuPDF's `Table.extract()` turned "Aborts measurement, displays
   E1" into "Aborts measurement displays E1" followed by a lone ",". I saw this when I compared the
   output with `pdftotext -layout`. Fix: I only use PyMuPDF to find the cell boxes, and fill each cell
   from my own text lines that sit inside it.
2. **The diff counted digits inside error codes.** For 4.3 ("E1–E5" becoming "E1–E6") it reported
   "5 removed, 6 added" as if they were numbers. I saw this in the first v1 to v2 report. Fix: digits
   right after a letter are ignored, and error codes are read as codes.
3. **Search results showed `child_count: 0`** because I never calculated it there. A wrong 0 is
   worse than no value, so it now returns `null`.

I avoided two other problems because I inspected the PDF first: font-size-based levels (they would have
broken 3.2) and bold detection breaking on the one-character hyphen spans in a different font.

How I checked the results: I printed the tree (`scripts/show_tree.py`) and compared it with the PDF by
eye. I diffed the plain text of v1 and v2 with `pdftotext` to get an independent list of what really
changed. I ran the OCR check. And I wrote a unit test for every problem above (32 tests in total).

## 4. Matching sections between v1 and v2

I try three steps in order:

1. **Same section number and a similar heading** (similarity of at least 0.6). Section numbers are
   the manual's own ids, and they keep the two "Error Codes" sections apart.
2. **Same heading under a parent with the same heading.** This catches a section that was renumbered.
3. **Fuzzy match** on heading plus body text, only if one candidate is clearly better than the
   rest. If it is unclear, the section is treated as new. I would rather miss a match than make a
   wrong one.

Matched and same hash means "unchanged". Matched with a different hash means "modified". No match
means "added", and old sections that were not matched are listed as "removed".

On the real files: 23 sections unchanged, 4 modified (2.1.1.1, 3.2, 4.2, 4.3), 1 added (5.3), and none
removed. This matches the `pdftotext` diff exactly.

**Where it breaks:**
- A section that is renumbered **and** retitled at the same time shows up as removed plus added. Its
  old test cases then show as stale, which is the safe side to be wrong on.
- A section split into two is not tracked as a split. The first half matches, and the second is new.
- Very short headings make the 0.6 similarity check weak.
- If a number is reused for a different topic with a similar title, it is accepted as the same section.

## 5. LLM test case generation

### The prompt

- It tells the model it is a medical-device QA engineer and asks for 3 to 5 test cases in an exact
  JSON format.
- Each section is wrapped in a `<section node_id=...>` tag. The prompt says text inside the tags is
  only data, so instructions hidden in the manual are ignored.
- Every test case must list the `source_node_ids` it is based on and a **`requirement_quote` copied
  word for word** from the manual. This quote is the most important part of my design. It ties each
  test case to one exact sentence or table row, and that is what makes the staleness check possible.

### When the LLM gives a bad answer

- **Fenced code or extra text around the JSON**: I strip it and continue.
- **Broken JSON**: I reject the attempt and retry, sending the error back to the model.
- **One test case fails the schema** (missing steps, wrong priority value): I drop that case and
  record why.
- **A case cites a section that was not in the selection**: I drop it.
- **A case's quote is not really in the manual**: I drop it. A made-up requirement is the most
  dangerous mistake an LLM can make here.
- **More than 5 valid cases**: I keep the first 5.
- **Fewer than 3 valid cases**: I retry.
- **Timeout or server error**: I wait a little and retry.
- **3 failed attempts**: I save the generation as `failed` with all the raw answers and return HTTP 502.

In short: I am relaxed about the wrapping and strict about the content. Nothing that failed the checks
is ever saved as a good result.

### Same selection submitted twice

I return the **existing** result instead of calling the LLM again. A selection is pinned to fixed text,
so asking again would only produce a second, different answer to the same question. That costs money,
and it leaves two conflicting sets of test cases for the same requirement. If someone really wants a new
one, `force: true` makes a new generation, marks which one it replaces, and keeps the old one. Failed
generations are not reused, so submitting again after a failure tries again.

## 6. Detecting stale test cases

Every time you fetch a generation, it is compared with the latest version of the manual.

For each source section: is it unchanged, changed, or removed? If it changed, is the change
**substantive** (a number, unit, error code, or a word like "not", "must" or "except" changed) or
only **wording**?

For each test case, using its quote:

- **current**: none of its sections changed.
- **review_minor**: a section was only reworded, and the quote is still there.
- **review**: a section changed in a way that matters, but the exact quoted requirement is still
  there. Example: a test on error E1 after E3 changed and E6 was added.
- **stale**: the quote is no longer in the text, or the section was removed. Example: "inflates in
  40 mmHg increments" became 30 mmHg.

It also lists sections that were added after the test cases were made (5.3 Data Export). That is
not staleness, it is a gap in coverage.

**Is a one-word wording change treated like a changed pressure threshold?** No. A threshold change
is "substantive" and makes the case stale. A wording change elsewhere in the section is only
"review_minor". There are limits, though. If the rewording is *inside* the quote, the case shows as
stale even when the meaning is the same, because I compare the text literally. I accept that, because
for a medical device a false alarm is cheap and a missed change is not. And a change like
"inflate" to "deflate" outside the quote only counts as wording, because my rules do not understand
meaning.

## 7. Decision log

**1. What part could silently give wrong results without any error? How would I catch it?**
The version matcher. If it pairs the wrong old and new sections, everything after it is wrong: a stale
test case can look "current" because it is being compared with the wrong section, and nothing crashes.
To catch it, I would raise an alert when a matched pair has very different body text, ask a person to
confirm every fuzzy match, and keep the real v1/v2 files as a fixed test that checks the exact list of
changed sections. That last check already exists in my tests.

**2. Where did I choose simplicity over correctness, and what breaks first in production?**
TinyDB breaks first. With more than one server process, generations would get lost or the file would
get corrupted. I also check staleness with exact quote matching instead of real meaning, so reworded
but equivalent text gets flagged. The parser depends on this manual's style (bold plus a number means
a heading), so a manual without numbered headings would come out flat, though with warnings. Ingestion
also runs inside the request instead of in a background job.

**3. One input I did not handle, and what happens.**
A table that continues onto the next page. It is read as two separate tables in the same section. No
rows are lost, but the second part has no header row. Another one: a heading that wraps onto two bold
lines. I wrote code to join them, but no test covers it, and it could wrongly join an unnumbered bold
line that comes right after a heading. On the LLM side, a test case can quote the right sentence but
test something unrelated. My checks only confirm that the quote exists, not that the steps really test it.

## 8. What I would do with more time

- Use embeddings or a small LLM check to decide whether a changed requirement still means the same thing.
- Split sections into single requirements (one per sentence, list item or table row), each with its own hash.
- Move to MongoDB or Postgres, run ingestion as a background job, and add database migrations.
- Add an endpoint where a person can approve or reject fuzzy matches.
- Record real LLM responses and replay them in tests, to catch it when a prompt change makes the output worse.
