from behive.engine import discovery_paths


def test_each_finding_generates_bounded_typed_paths():
    specs = discovery_paths._path_specs(
        "Autophagy changes TDP-43 clearance under cellular stress", 1
    )
    assert len(specs) == 2
    assert specs[0][0] == "mechanism"
    assert specs[1][0] in discovery_paths.PATH_TYPES
    assert all(question.endswith("?") for _, question, _ in specs)


def test_research_plan_has_evidence_and_stopping_rules():
    plan = discovery_paths._plan(
        "boundary", "When does this fail?", "TDP-43 clearance changes under stress"
    )
    assert len(plan["queries"]) == 3
    assert plan["minimum_independent_sources"] == 3
    assert plan["completion_criterion"]
    assert plan["abandonment_criterion"]
    assert plan["revisit_when"]


def test_competing_explanations_preserve_alternatives():
    explanations = discovery_paths._explanations("A changes B")
    assert {item["type"] for item in explanations} == {
        "primary", "alternative", "context", "artifact"
    }
    assert round(sum(item["weight"] for item in explanations), 4) == 1
