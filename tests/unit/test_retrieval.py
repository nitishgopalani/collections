from app.engine.retrieval import parse_flow_tag, resolve_flows, tagged_flow_text


def test_parse_flow_tag():
    assert parse_flow_tag("[[flow:promise_to_pay]] Borrower agrees to pay") == "promise_to_pay"
    assert parse_flow_tag("no tag here") is None


def test_tagged_flow_text():
    text = tagged_flow_text("dispute", "Borrower disputes the loan.")
    assert text.startswith("[[flow:dispute]]")


def test_resolve_flows_from_tag():
    results = [
        {
            "doc_id": "doc-1",
            "score": 0.91,
            "text": "[[flow:promise_to_pay]] Borrower agrees to pay on a future date.",
        }
    ]
    candidates = resolve_flows(results)
    assert len(candidates) == 1
    assert candidates[0].name == "promise_to_pay"
    assert candidates[0].score == 0.91


def test_resolve_flows_from_doc_map_fallback():
    results = [{"doc_id": "doc-99", "score": 0.5, "text": "plain chunk without tag"}]
    candidates = resolve_flows(results, doc_map={"doc-99": "dispute"})
    assert len(candidates) == 1
    assert candidates[0].name == "dispute"


def test_resolve_flows_drops_unresolvable():
    results = [{"doc_id": "unknown", "score": 0.2, "text": "random text"}]
    assert resolve_flows(results) == []


def test_resolve_flows_keeps_highest_score_per_flow():
    results = [
        {"doc_id": "a", "score": 0.4, "text": "[[flow:pay_now]] low"},
        {"doc_id": "b", "score": 0.9, "text": "[[flow:pay_now]] high"},
    ]
    candidates = resolve_flows(results)
    assert len(candidates) == 1
    assert candidates[0].score == 0.9
