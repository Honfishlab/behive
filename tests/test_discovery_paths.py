from behive.engine import discovery_paths


def test_each_finding_generates_bounded_typed_paths():
    specs = discovery_paths._path_specs(
        "Autophagy changes TDP-43 clearance under cellular stress", 1,
        "TDP-43 biology in neurodegeneration",
    )
    assert len(specs) == 2
    assert specs[0][0] == "cross_domain"
    assert specs[1][0] in discovery_paths.PATH_TYPES
    assert all(question.endswith("?") for _, question, _ in specs)


def test_fanout_portfolio_is_diverse_and_not_review_dominated():
    specs = [item for index in range(5) for item in discovery_paths._path_specs(
        "NAD supports cellular energy", index, "NAD in ALS and neurological disease"
    )]
    kinds = [item[0] for item in specs]
    assert len(set(kinds)) == 10
    assert not set(kinds) & set(discovery_paths.NARROW_REVIEW_TYPES)
    assert any("adjacent" in question.lower() or "neighboring" in question.lower()
               for _, question, _ in specs)
    assert any("intervention" in kind or "practical" in kind for kind in kinds)


def test_research_plan_has_evidence_and_stopping_rules():
    plan = discovery_paths._plan(
        "adjacent_system", "What connects to this?", "TDP-43 clearance changes under stress",
        "TDP-43 in neurodegeneration",
    )
    assert len(plan["queries"]) == 3
    assert plan["minimum_independent_sources"] == 2
    assert plan["completion_criterion"]
    assert plan["abandonment_criterion"]
    assert plan["revisit_when"]
    assert plan["strategy"] == "frontier_fanout"
    assert plan["strategy_version"] == 2


def test_competing_explanations_preserve_alternatives():
    explanations = discovery_paths._explanations("A changes B")
    assert {item["type"] for item in explanations} == {
        "network", "downstream", "context", "opportunity"
    }
    assert round(sum(item["weight"] for item in explanations), 4) == 1
