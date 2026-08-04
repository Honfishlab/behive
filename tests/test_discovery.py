from behive.discovery import QuestionSignal, connection_strength, rank_frontier, suggest_followups


def test_frontier_prefers_unanswered_high_priority_question():
    answered = QuestionSignal("a", "What is established?", 1, priority=0.8, confidence=0.9, finding_count=8)
    gap = QuestionSignal("b", "What remains unknown?", 1, priority=0.8, confidence=0.1, finding_count=0)
    assert rank_frontier([answered, gap])[0].id == "b"


def test_contradictions_raise_discovery_score():
    calm = QuestionSignal("a", "A question", 2, contradiction_count=0)
    disputed = QuestionSignal("b", "A disputed question", 2, contradiction_count=3)
    assert disputed.discovery_score > calm.discovery_score


def test_followups_are_explicit_leads_not_findings():
    items = suggest_followups("Why did adoption slow?", ["Regulation"])
    assert {item["kind"] for item in items} >= {"mechanism", "evidence", "countercase", "connection"}
    assert all("requires sourced research" in item["rationale"] for item in items)


def test_connection_strength_is_explainable():
    assert connection_strength("AI regulation in Europe", "Europe changes AI regulation") > 0.4
    assert connection_strength("marine biology", "quantum compiler") == 0.0
