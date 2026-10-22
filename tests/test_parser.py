"""Parser tests aimed at the structural irregularities found in the CT-200 PDFs."""
from pathlib import Path

import pymupdf
import pytest

from app.parser import content_hash, parse_pdf

DATA = Path(__file__).resolve().parent.parent / "data"
V1 = DATA / "ct200_manual.pdf"
V2 = DATA / "ct200_manual_v2.pdf"


@pytest.fixture(scope="module")
def v1():
    return parse_pdf(V1)


@pytest.fixture(scope="module")
def v2():
    return parse_pdf(V2)


def anomaly_types(section):
    return {a["type"] for a in section.anomalies}


def test_title_is_reassembled_from_three_lines(v1):
    assert v1.title == "CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual"


def test_all_numbered_sections_found(v1, v2):
    expected = {"1", "1.1", "1.2", "2", "2.1", "2.1.1.1", "2.2", "3", "3.1", "3.2", "3.3", "3.4",
                "4", "4.1", "4.2", "4.3", "5", "5.1", "5.2", "6", "6.1", "6.2", "7", "7.1", "7.2",
                "8", "8.1"}
    assert {s.key for s in v1.sections} == expected
    assert {s.key for s in v2.sections} == expected | {"5.3"}


def test_duplicate_heading_gives_two_nodes_with_correct_parents(v1):
    error_codes = [s for s in v1.sections if s.heading == "Error Codes"]
    assert [s.key for s in error_codes] == ["4.2", "7.1"]
    assert [s.parent_key for s in error_codes] == ["4", "7"]
    assert all("duplicate_heading" in anomaly_types(s) for s in error_codes)
    # and their content is not merged
    assert "E1 |" in error_codes[0].body_text
    assert "persists after following" in error_codes[1].body_text


def test_skipped_level_attaches_to_nearest_ancestor_and_is_flagged(v1):
    s = v1.by_key()["2.1.1.1"]
    assert s.level == 4
    assert s.parent_key == "2.1"  # 2.1.1 does not exist; we do not invent it
    assert "skipped_level" in anomaly_types(s)
    assert "2.1.1" not in v1.by_key()


def test_body_size_bold_heading_is_still_a_heading(v1):
    # 3.2 is printed at 11pt bold (same size as body text), unlike its 12.9pt siblings
    s = v1.by_key()["3.2"]
    assert s.heading == "Cuff Inflation Sequence"
    assert s.parent_key == "3" and s.level == 2
    assert s.body_text.startswith("On starting a measurement")


def test_out_of_order_siblings_keep_document_order_and_are_flagged(v1):
    keys = [s.key for s in v1.children("3")]
    assert keys == ["3.1", "3.2", "3.4", "3.3"]
    assert "out_of_order" in anomaly_types(v1.by_key()["3.4"])
    assert "out_of_order" in anomaly_types(v1.by_key()["3.3"])
    assert "out_of_order" not in anomaly_types(v1.by_key()["3.2"])


def test_numbered_list_is_not_parsed_as_headings(v1):
    s = v1.by_key()["3.3"]
    items = [b for b in s.blocks if b.kind == "list_item"]
    assert [b.marker for b in items] == ["1", "2", "3", "4", "5"]
    # the wrapped second line of item 5 belongs to item 5
    assert items[4].text.endswith("device recommends seeking immediate medical attention")
    assert not any(s.heading.startswith(("Normal", "Elevated")) for s in v1.sections)
    assert all(c.parent_key != "3.3" for c in v1.sections)


def test_cross_references_are_not_headings(v1):
    s = v1.by_key()["3.3"]
    assert s.references == ["2.1", "4.3"]
    assert v1.by_key()["7.1"].references == ["4.2"]


def test_paragraph_split_across_page_break_stays_in_its_section(v1):
    s = v1.by_key()["3.1"]
    assert s.page == 2
    assert "Press and hold the power button" in s.body_text
    assert "at the time of measurement." in s.body_text  # this line is on page 3
    assert len(s.blocks) == 1


def test_tables_are_extracted_with_correct_cells(v1, v2):
    t1 = v1.by_key()["4.2"].blocks[0]
    assert t1.kind == "table"
    assert t1.rows[0] == ["Code", "Meaning", "Device Behavior"]
    assert t1.rows[1] == ["E1", "Cuff not connected or leak detected", "Aborts measurement, displays E1"]
    assert len(t1.rows) == 6
    t2 = v2.by_key()["4.2"].blocks[0]
    assert t2.rows[-1][0] == "E6"
    # table text must not leak into the section as loose paragraphs
    assert [b.kind for b in v1.by_key()["2.1"].blocks] == ["table"]


def test_ligatures_and_nonbreaking_hyphens_are_normalised(v1):
    text = v1.by_key()["2.2"].body_text
    assert "CT-200 fits" in text and "ﬁ" not in text and "‑" not in text


def test_reflowed_text_hashes_identically(v1, v2):
    # 3.1 wraps differently in v2 (it moves page) but its text is unchanged
    assert v1.by_key()["3.1"].content_hash == v2.by_key()["3.1"].content_hash
    assert content_hash("a  b\nc") == content_hash("a b c")


def test_changed_sections_have_different_hashes(v1, v2):
    a, b = v1.by_key(), v2.by_key()
    changed = {k for k in a if a[k].content_hash != b[k].content_hash}
    assert changed == {"2.1.1.1", "3.2", "4.2", "4.3"}


# ---------------------------------------------------------------- synthetic edge cases

def _make_pdf(path: Path, lines: list[tuple[str, bool]]) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60
    for text, bold in lines:
        page.insert_text((40, y), text, fontname="hebo" if bold else "helv", fontsize=11)
        y += 30
    doc.save(str(path))


def test_duplicate_section_number_gets_distinct_key(tmp_path):
    pdf = tmp_path / "dup.pdf"
    _make_pdf(pdf, [("1. Intro", True), ("Body one.", False), ("1.1 Part", True), ("Body two.", False),
                    ("1.1 Part again", True), ("Body three.", False)])
    doc = parse_pdf(pdf)
    keys = [s.key for s in doc.sections]
    assert keys == ["1", "1.1", "1.1~2"]
    dup = doc.by_key()["1.1~2"]
    assert dup.parent_key == "1" and "duplicate_number" in anomaly_types(dup)


def test_orphan_and_front_matter_are_exposed(tmp_path):
    pdf = tmp_path / "orphan.pdf"
    _make_pdf(pdf, [("Some preface text.", False), ("4.2 Lonely", True), ("Body.", False)])
    doc = parse_pdf(pdf)
    by = doc.by_key()
    assert "front_matter" in anomaly_types(by["front-matter"])
    assert by["4.2"].parent_key is None and "orphan" in anomaly_types(by["4.2"])
