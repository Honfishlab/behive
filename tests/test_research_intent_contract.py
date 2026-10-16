"""Gold-topic contracts for deterministic evidence admission."""
from behive.engine.research_intent import assess_relevance, build_intent, build_query_families


def test_nad_is_primary_entity_not_generic_long_word():
    intent = build_intent("How does NAD function in the body and what are its implications for energy, repair, and ALS treatment?")
    assert intent.primary_entities[0]["canonical"] == "nad"
    assert {item["canonical"] for item in intent.contexts} >= {"als", "energy", "repair"}


def test_nad_energy_and_repair_source_is_admitted():
    result = assess_relevance(
        "How does NAD function in the body and what are its implications for energy, repair, and ALS treatment?",
        "NAD+ metabolism in health and disease",
        "Nicotinamide adenine dinucleotide controls redox metabolism, ATP production and PARP DNA repair.",
    )
    assert result["accepted"]
    assert result["score"] >= .7


def test_nad_als_source_is_admitted():
    assert assess_relevance(
        "NAD energy repair and ALS treatment",
        "NAD homeostasis in amyotrophic lateral sclerosis",
        "NAD metabolism is disrupted in motor neurons in ALS models.",
    )["accepted"]


def test_unrelated_ai_and_astronomical_nad_are_rejected():
    topic = "NAD energy repair and ALS treatment"
    assert not assess_relevance(topic, "AI in healthcare", "Machine learning predicts heart failure.")["accepted"]
    assert not assess_relevance(topic, "NaD stellar absorption", "Spectroscopy of galaxies and stellar mass.")["accepted"]
    assert not assess_relevance(topic, "AI for embryo assessment", "NAD autofluorescence enables metabolic imaging.")["accepted"]
    assert "als" not in assess_relevance(topic, "Embryo quality", "Infertility affects people globally; NAD supports metabolism.")["context_hits"]


def test_query_plan_is_faceted_and_entity_anchored():
    queries = build_query_families("NAD energy repair and ALS treatment")
    assert len(queries) >= 4
    assert all("NAD" in item["query"] or "nad" in item["query"] for item in queries)
    assert {item["purpose"] for item in queries} >= {"overview", "als", "human evidence"}
