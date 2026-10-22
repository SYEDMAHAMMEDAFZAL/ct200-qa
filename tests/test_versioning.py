from app.diffing import diff_summary
from app.ingest import change_report, ingest_pdf
from app.models import Node
from tests.conftest import V1, V2


def test_v2_is_a_new_version_and_v1_survives(db):
    v1 = ingest_pdf(db, V1).version
    v2 = ingest_pdf(db, V2).version
    assert (v1.version_number, v2.version_number) == (1, 2)
    old = db.get(Node, "ct200:v1:3.2")
    assert "40 mmHg increments" in old.body_text  # v1 text is untouched
    assert "30 mmHg increments" in db.get(Node, "ct200:v2:3.2").body_text


def test_reingesting_identical_file_is_idempotent(db):
    ingest_pdf(db, V1)
    again = ingest_pdf(db, V1)
    assert again.created is False and again.version.version_number == 1


def test_unchanged_nodes_share_logical_identity_and_changes_are_flagged(db):
    ingest_pdf(db, V1)
    v2 = ingest_pdf(db, V2).version
    s = v2.change_summary
    assert set(s["modified"]) == {"ct200:v2:2.1.1.1", "ct200:v2:3.2", "ct200:v2:4.2", "ct200:v2:4.3"}
    assert s["added"] == ["ct200:v2:5.3"]
    assert s["removed"] == [] and len(s["unchanged"]) == 23
    a, b = db.get(Node, "ct200:v1:3.1"), db.get(Node, "ct200:v2:3.1")
    assert a.logical_id == b.logical_id and b.change_status == "unchanged"
    # duplicate headings stay matched to their own section, not to each other
    assert db.get(Node, "ct200:v2:7.1").logical_id == db.get(Node, "ct200:v1:7.1").logical_id
    assert db.get(Node, "ct200:v2:7.1").logical_id != db.get(Node, "ct200:v2:4.2").logical_id


def test_change_report_flags_threshold_change_as_substantive(db):
    ingest_pdf(db, V1)
    ingest_pdf(db, V2)
    rep = change_report(db, db.get(Node, "ct200:v1:4.2"))
    step = rep["steps"][0]
    assert rep["changed_across_versions"] and step["diff"]["severity"] == "substantive"
    assert "2 seconds" in step["diff"]["numeric_changes"]["removed"]
    assert "1.5 seconds" in step["diff"]["numeric_changes"]["added"]
    assert change_report(db, db.get(Node, "ct200:v1:3.1"))["changed_across_versions"] is False


def test_renumbered_section_is_matched_by_heading_path(db, tmp_path):
    import pymupdf

    def make(path, sections):
        doc = pymupdf.open()
        page = doc.new_page()
        y = 60
        for head, body in sections:
            page.insert_text((40, y), head, fontname="hebo", fontsize=11)
            page.insert_text((40, y + 20), body, fontname="helv", fontsize=11)
            y += 60
        doc.save(str(path))

    make(tmp_path / "a.pdf", [("1. Ops", ""), ("1.1 Start", "Press start."), ("1.2 Stop", "Press stop.")])
    make(tmp_path / "b.pdf", [("1. Ops", ""), ("1.1 Stop", "Press stop."), ("1.2 Start", "Press start now.")])
    ingest_pdf(db, tmp_path / "a.pdf", slug="t")
    v2 = ingest_pdf(db, tmp_path / "b.pdf", slug="t").version
    new_stop = db.get(Node, "t:v2:1.1")
    assert new_stop.logical_id == db.get(Node, "t:v1:1.2").logical_id
    assert new_stop.match_method == "heading_path" and new_stop.change_status == "unchanged"
    assert db.get(Node, "t:v2:1.2").change_status == "modified"
    assert {"from": "t:v1:1.2", "to": "t:v2:1.1"} in v2.change_summary["renumbered"]


def test_wording_change_is_not_substantive():
    d = diff_summary("Wipe the device with a soft cloth.", "Wipe the unit with a soft cloth.")
    assert d["severity"] == "wording"
    assert diff_summary("Do not submerge.", "Do submerge.")["severity"] == "substantive"
