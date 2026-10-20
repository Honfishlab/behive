"""Contracts for the provider-independent passage extractor."""
from behive.engine.claim_extractor import _deterministic_passages


TOPIC = (
    "What does the broad published and registered evidence show about IGFBP7 and HMB "
    "across human ALS genetics, biomarkers, mechanisms, and clinical trials?"
)


def test_deterministic_fallback_returns_exact_empirical_sentence():
    text = (
        "This review introduces the field. "
        "The IGFBP7 variant was associated with an ALS reversal phenotype in the discovery cohort. "
        "Additional work is needed."
    )
    result = _deterministic_passages(TOPIC, "IGFBP7 genetics in ALS", text)
    assert len(result) == 1
    assert result[0]["claim"] == result[0]["exact_quote"]
    assert result[0]["exact_quote"] in text
    assert result[0]["confidence"] < .5


def test_deterministic_fallback_rejects_unrelated_empirical_sentence():
    text = "The model showed improved prediction of heart failure in a large hospital cohort."
    assert _deterministic_passages(TOPIC, "Artificial intelligence in cardiology", text) == []


def test_deterministic_fallback_requires_empirical_cue():
    text = "IGFBP7 is a protein that may be relevant to multiple biological systems."
    assert _deterministic_passages(TOPIC, "IGFBP7 overview", text) == []
