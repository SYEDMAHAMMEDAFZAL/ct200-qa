"""End-to-end API tests: ingest v1 -> select -> generate -> ingest v2 -> staleness on retrieval."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import MockLLM
from app.main import create_app


def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 't.db'}", generation_store_path=":memory:",
                    llm_provider="mock", llm_base_url="", llm_model="", llm_api_key="", llm_max_attempts=3,
                    ocr_crosscheck=False)


@pytest.fixture
def client(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as c:
        c.app = app
        yield c


def ingest(c, name):
    r = c.post("/documents/ct200/versions", params={"path": f"data/{name}"})
    assert r.status_code == 201, r.text
    return r.json()


def select(c, ids, name="sel"):
    r = c.post("/selections", json={"name": name, "node_ids": ids})
    assert r.status_code == 201, r.text
    return r.json()


def test_browse_endpoints(client):
    ingest(client, "ct200_manual.pdf")
    top = client.get("/documents/ct200/sections").json()
    assert [n["section_number"] for n in top] == [str(i) for i in range(1, 9)]
    node = client.get("/nodes/ct200:v1:2.1").json()
    assert [c["id"] for c in node["children"]] == ["ct200:v1:2.1.1.1"]
    assert node["blocks"][0]["kind"] == "table" and len(node["content_hash"]) == 64
    hits = client.get("/search", params={"q": "Error Codes", "field": "heading"}).json()
    assert [h["id"] for h in hits] == ["ct200:v1:4.2", "ct200:v1:7.1"]
    assert client.get("/nodes/nope").status_code == 404


def test_upload_and_idempotent_reingest(client):
    with open("data/ct200_manual.pdf", "rb") as fh:
        r = client.post("/documents/ct200/versions", files={"file": ("ct200_manual.pdf", fh, "application/pdf")})
    assert r.status_code == 201 and r.json()["created"] is True
    again = ingest(client, "ct200_manual.pdf")
    assert again["created"] is False and again["version"]["version_number"] == 1


def test_full_versioning_and_staleness_flow(client):
    ingest(client, "ct200_manual.pdf")
    sel = select(client, ["ct200:v1:3.2", "ct200:v1:4.2", "ct200:v1:6.1"], "inflation + errors")
    gen = client.post(f"/selections/{sel['id']}/generations").json()
    assert gen["status"] == "succeeded" and 3 <= len(gen["test_cases"]) <= 5
    assert gen["staleness"]["status"] == "current"

    # duplicate submit returns the same generation
    again = client.post(f"/selections/{sel['id']}/generations")
    assert again.status_code == 200 and again.json()["reused"] is True and again.json()["id"] == gen["id"]

    v2 = ingest(client, "ct200_manual_v2.pdf")["version"]
    assert v2["version_number"] == 2

    # the old selection still resolves to v1 text
    pinned = client.get(f"/selections/{sel['id']}").json()
    item = next(i for i in pinned["items"] if i["node_id"] == "ct200:v1:3.2")
    assert "40 mmHg increments" in item["pinned_text"] and item["pin_intact"]
    assert item["latest"]["status"] == "changed" and item["latest"]["severity"] == "substantive"

    # retrieval shows staleness per source and per test case
    got = client.get(f"/generations/{gen['id']}").json()
    st = got["staleness"]
    assert st["checked_against_version"] == 2
    by_src = {s["node_id"]: s for s in st["sources"]}
    assert by_src["ct200:v1:3.2"]["status"] == "changed"
    assert by_src["ct200:v1:6.1"]["status"] == "unchanged"
    assert st["status"] in ("review", "stale")
    assert "ct200:v2:5.3" in st["new_sections_since"]

    # lookup by node id works with the *new* version's id of the same logical section
    by_node = client.get("/generations", params={"node_id": "ct200:v2:3.2"}).json()
    assert [g["id"] for g in by_node] == [gen["id"]]

    changes = client.get("/nodes/ct200:v1:3.2/changes").json()
    assert changes["changed_across_versions"] and changes["steps"][0]["diff"]["severity"] == "substantive"


def test_unchanged_selection_stays_current_after_v2(client):
    ingest(client, "ct200_manual.pdf")
    sel = select(client, ["ct200:v1:1.1", "ct200:v1:6.1", "ct200:v1:7.2"])
    gen = client.post(f"/selections/{sel['id']}/generations").json()
    ingest(client, "ct200_manual_v2.pdf")
    assert client.get(f"/generations/{gen['id']}").json()["staleness"]["status"] == "current"


def test_test_case_on_untouched_quote_is_review_not_stale(client):
    ingest(client, "ct200_manual.pdf")
    sel = select(client, ["ct200:v1:4.2"])
    gen = client.post(f"/selections/{sel['id']}/generations").json()
    ingest(client, "ct200_manual_v2.pdf")
    cases = {c["test_case_id"]: c["status"]
             for c in client.get(f"/generations/{gen['id']}").json()["staleness"]["test_cases"]}
    quotes = {c["id"]: c["requirement_quote"] for c in gen["test_cases"]}
    for tc_id, quote in quotes.items():
        expected = "stale" if "2 seconds" in quote else "review"
        assert cases[tc_id] == expected, (quote, cases[tc_id])


def test_malformed_llm_output_is_retried_then_accepted(client):
    ingest(client, "ct200_manual.pdf")
    client.app.state.llm = MockLLM(["malformed", "bad_quote"])
    sel = select(client, ["ct200:v1:4.1", "ct200:v1:4.2"])
    r = client.post(f"/selections/{sel['id']}/generations")
    assert r.status_code == 201
    g = client.get(f"/generations/{r.json()['id']}", params={"attempts": True}).json()
    assert [a.get("accepted", False) for a in g["attempts"]] == [False, False, True]
    assert "could not parse JSON" in g["attempts"][0]["errors"][0]


def test_exhausted_retries_store_a_failed_generation(client):
    ingest(client, "ct200_manual.pdf")
    client.app.state.llm = MockLLM(["bad_ids", "timeout", "too_few"])
    sel = select(client, ["ct200:v1:4.1"])
    r = client.post(f"/selections/{sel['id']}/generations")
    assert r.status_code == 502
    body = r.json()
    assert body["status"] == "failed" and body["test_cases"] == [] and len(body["attempts"]) == 3
    # a failure is not cached: the next submit tries again
    client.app.state.llm = MockLLM()
    assert client.post(f"/selections/{sel['id']}/generations").status_code == 201


def test_force_creates_new_generation_that_supersedes(client):
    ingest(client, "ct200_manual.pdf")
    sel = select(client, ["ct200:v1:4.2"])
    first = client.post(f"/selections/{sel['id']}/generations").json()
    second = client.post(f"/selections/{sel['id']}/generations", json={"force": True}).json()
    assert second["id"] != first["id"] and second["supersedes"] == first["id"]
    assert len(client.get("/generations", params={"selection_id": sel["id"]}).json()) == 2
