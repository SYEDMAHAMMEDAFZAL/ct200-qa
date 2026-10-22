from app.generation import Source, extract_json, quote_in, validate_response

SRC = [Source(node_id="d:v1:4.1", logical_id=1, document="d", version=1, section_number="4.1",
              heading="Overpressure Protection", content_hash="x",
              text="If cuff pressure exceeds 299 mmHg at any point, the device vents the cuff within 2 seconds.")]


def case(**kw):
    base = {"title": "Overpressure vent", "objective": "Check venting", "steps": ["Pressurise to 300 mmHg"],
            "expected_result": "Cuff vents within 2 s", "priority": "high", "source_node_ids": ["d:v1:4.1"],
            "requirement_quote": "the device vents the cuff within 2 seconds"}
    base.update(kw)
    return base


def test_envelope_is_lenient():
    assert extract_json('Here you go:\n```json\n{"test_cases": []}\n```')["test_cases"] == []
    assert extract_json('[{"a": 1}]') == {"test_cases": [{"a": 1}]}


def test_content_is_strict():
    import json
    raw = json.dumps({"test_cases": [case(), case(title="Second case", priority="urgent"),
                                     case(title="Third case", source_node_ids=["x:v1:1"]),
                                     case(title="Fourth case", requirement_quote="vents in 5 seconds")]})
    valid, errors, _ = validate_response(raw, SRC)
    assert len(valid) == 1 and len(errors) == 3
    assert any("priority" in e for e in errors)
    assert any("not in selection" in e for e in errors)
    assert any("verbatim" in e for e in errors)


def test_quote_matching_ignores_case_dashes_and_table_pipes():
    assert quote_in("E3 Overpressure condition", "E3 | Overpressure condition | Auto-deflates")
    assert quote_in("arm circumference of 22-42 cm", "an arm circumference of 22–42 cm.")
    assert not quote_in("", "anything")
