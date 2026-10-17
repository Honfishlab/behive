"""Gold-topic contracts for deterministic evidence admission."""
from behive.engine.research_intent import (
    assess_corpus_adequacy,
    assess_relevance,
    build_intent,
    build_query_families,
)


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


def test_compound_gold_nad_queries_preserve_both_entities():
    topic = "Can 5nm gold particles catalyze NADH to NAD+ in the brain and CNS?"
    intent = build_intent(topic)
    assert {item["canonical"] for item in intent.primary_entities} == {"gold nanoparticle", "nad"}
    queries = build_query_families(topic)
    assert {item["purpose"] for item in queries} >= {"direct catalysis", "size constraint", "CNS delivery"}
    assert all("gold" in item["query"].lower() and "nad" in item["query"].lower() for item in queries)


def test_corpus_about_only_nad_is_retrieval_inadequate_for_gold_nad_topic():
    topic = "Can 5nm gold particles catalyze NADH to NAD+ in the brain and CNS?"
    records = [
        {"title": "NAD metabolism in aging", "abstract": "NAD controls redox in the brain."},
        {"title": "NADH oxidation", "abstract": "NAD+ supports CNS metabolism."},
        {"title": "NAD and neurological disease", "abstract": "Brain NAD declines with age."},
    ]
    result = assess_corpus_adequacy(topic, records)
    assert not result["adequate"]
    assert result["joint_entity_records"] == 0
    assert "gold nanoparticle" in result["missing_entities"]


def test_joint_gold_nad_corpus_is_adequate():
    topic = "Can 5nm gold particles catalyze NADH to NAD+ in the brain and CNS?"
    records = [
        {"title": "Gold nanoparticle catalysis of NADH oxidation", "abstract": "AuNP converts NADH to NAD+."},
        {"title": "Colloidal gold nanozyme redox activity", "abstract": "NADH oxidation by gold nanoparticles."},
        {"title": "Brain delivery of AuNP", "abstract": "Gold nanoparticles cross the BBB; NAD redox was measured."},
    ]
    result = assess_corpus_adequacy(topic, records)
    assert result["adequate"]
    assert result["joint_entity_records"] == 3
