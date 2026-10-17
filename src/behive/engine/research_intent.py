"""Canonical research intent and relevance scoring.

This module is deliberately deterministic.  LLMs may help phrase questions,
but evidence admission must remain explainable and testable.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


_ALIASES = {
    "nad": ["nad", "nad+", "nadh", "nad(h)", "nicotinamide adenine dinucleotide"],
    "gold nanoparticle": ["gold nanoparticle", "gold nanoparticles", "gold particle", "gold particles", "gold nanomaterial",
                          "gold nanomaterials", "aunp", "au nps", "colloidal gold", "nanogold"],
    "oxaloacetate": ["oxaloacetate", "oxaloacetic acid", "oaa"],
    "tdp-43": ["tdp-43", "tdp43", "tardbp", "tar dna-binding protein 43"],
    "mycelium": ["mycelium", "mycelial", "fungal mycelium"],
}
_CONTEXT_ALIASES = {
    "als": ["als", "amyotrophic lateral sclerosis", "motor neuron disease"],
    "energy": ["energy", "bioenergetic", "mitochondri", "atp", "metabolism", "redox"],
    "repair": ["repair", "parp", "dna damage", "genome maintenance"],
    "neurodegeneration": ["neurodegener", "neurolog", "motor neuron"],
    "glutamate": ["glutamate", "excitotoxic"],
    "autophagy": ["autophag", "lysosom"],
    "aggregation": ["aggregation", "aggregate", "misfold"],
    "fermentation": ["ferment"],
    "nutrition": ["nutrient", "nutrition", "digestib", "antinutr"],
    "cns": ["brain", "central nervous system", "cns", "blood brain barrier", "bbb"],
    "catalysis": ["cataly", "nanozyme", "oxidation", "redox", "electron transfer"],
    "particle_size": ["5 nm", "5nm", "nanometer", "particle size"],
}
_GENERIC = {
    "implication", "implications", "function", "functions", "body", "substance",
    "treatment", "treatments", "benefit", "benefits", "mechanism", "mechanisms",
    "effect", "effects", "research", "question", "energy", "repair", "levels",
    "clinical", "study", "studies", "human", "health", "disease", "diseases",
    "other", "what", "where", "when", "which", "does", "with", "from", "into",
    "about", "their", "most", "more", "current", "evidence", "therapy", "therapies",
}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", (value or "").lower()).strip()


def _contains(text: str, alias: str) -> bool:
    text, alias = _norm(text), _norm(alias)
    return bool(alias and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text))


def _context_contains(text: str, alias: str) -> bool:
    """Match short context acronyms as tokens, longer values as stems."""
    normalized, alias = _norm(text), _norm(alias)
    if len(alias) <= 4:
        return _contains(normalized, alias)
    return alias in normalized


@dataclass
class ResearchIntent:
    topic: str
    primary_entities: list[dict] = field(default_factory=list)
    contexts: list[dict] = field(default_factory=list)
    excluded_senses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_intent(topic: str) -> ResearchIntent:
    normalized = _norm(topic)
    entities = []
    for canonical, aliases in _ALIASES.items():
        if any(_contains(normalized, alias) for alias in aliases):
            entities.append({"canonical": canonical, "aliases": aliases})
    if not entities:
        # Acronyms are often the most information-bearing biomedical subjects.
        acronyms = re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9-]{1,8}\+?)(?![A-Za-z0-9])", topic or "")
        for value in acronyms[:2]:
            entities.append({"canonical": value.lower(), "aliases": [value.lower()]})
    if not entities:
        tokens = [t for t in re.findall(r"[a-z0-9-]{4,}", normalized) if t not in _GENERIC]
        if tokens:
            entities.append({"canonical": tokens[0], "aliases": [tokens[0]]})
    contexts = [
        {"canonical": canonical, "aliases": aliases}
        for canonical, aliases in _CONTEXT_ALIASES.items()
        if any(_context_contains(normalized, fragment) for fragment in aliases)
    ]
    excluded = []
    if any(e["canonical"] == "nad" for e in entities):
        excluded += ["sodium D spectral line", "NaD stellar absorption", "astronomical spectroscopy"]
    return ResearchIntent(topic=topic, primary_entities=entities, contexts=contexts, excluded_senses=excluded)


def assess_relevance(topic: str, title: str, abstract: str = "") -> dict:
    intent = build_intent(topic)
    text = f"{title or ''} {abstract or ''}"
    entity_hits = [e["canonical"] for e in intent.primary_entities
                   if any(_contains(text, alias) for alias in e["aliases"])]
    title_entity_hits = [e["canonical"] for e in intent.primary_entities
                         if any(_contains(title or "", alias) for alias in e["aliases"])]
    context_hits = [c["canonical"] for c in intent.contexts
                    if any(_context_contains(text, fragment) for fragment in c["aliases"])]
    excluded = []
    low = _norm(text)
    if any(e["canonical"] == "nad" for e in intent.primary_entities):
        if re.search(r"\b(stellar|galaxy|galaxies|spectral|spectroscopy|mass function)\b", low):
            excluded.append("astronomical NaD")
    entity_ratio = len(entity_hits) / max(1, len(intent.primary_entities))
    context_ratio = len(context_hits) / max(1, len(intent.contexts)) if intent.contexts else 1.0
    score = max(0.0, min(1.0, .7 * entity_ratio + .3 * context_ratio - (.8 if excluded else 0)))
    # When the mission names contexts (disease, mechanism, outcome), entity-only
    # mentions are discovery noise, not admissible evidence.
    specific_context = bool({"als", "neurodegeneration"}.intersection(context_hits))
    contextual_support = (not intent.contexts or specific_context or len(context_hits) >= 2
                          or (bool(title_entity_hits) and bool(context_hits)))
    accepted = bool(entity_hits) and not excluded and contextual_support
    return {"accepted": accepted, "score": round(score, 4), "entity_hits": entity_hits,
            "title_entity_hits": title_entity_hits, "context_hits": context_hits,
            "entity_ratio": round(entity_ratio, 4),
            "all_entities_present": bool(intent.primary_entities) and len(entity_hits) == len(intent.primary_entities),
            "excluded_senses": excluded, "intent": intent.to_dict()}


def assess_corpus_adequacy(topic: str, records: list[dict]) -> dict:
    """Decide whether retrieval represents the actual compound research intent.

    A large pile of papers about only one half of a multi-entity question is not
    evidence of absence.  It is a retrieval failure and must be re-scouted.
    """
    intent = build_intent(topic)
    checks = [assess_relevance(topic, row.get("title", ""), row.get("abstract", ""))
              for row in records]
    relevant = [item for item in checks if item["accepted"] and item["score"] >= .55]
    joint = [item for item in relevant if item["all_entities_present"]]
    covered = sorted({hit for item in relevant for hit in item["entity_hits"]})
    required = [item["canonical"] for item in intent.primary_entities]
    enough_relevant = len(relevant) >= min(3, max(1, len(records)))
    enough_joint = len(required) < 2 or len(joint) >= 1
    adequate = bool(records) and enough_relevant and enough_joint
    return {
        "adequate": adequate,
        "records": len(records),
        "relevant_records": len(relevant),
        "joint_entity_records": len(joint),
        "required_entities": required,
        "covered_entities": covered,
        "missing_entities": [value for value in required if value not in covered],
        "reason": ("adequate" if adequate else
                   "no records retrieved" if not records else
                   "compound topic was split during retrieval" if len(required) >= 2 and not joint else
                   "too few topically relevant records"),
    }


def build_query_families(topic: str) -> list[dict]:
    intent = build_intent(topic)
    if not intent.primary_entities:
        return [{"purpose": "original", "query": topic}]
    def subject_for(entity: dict) -> str:
        if entity["canonical"] == "nad":
            return '("NAD+" OR "nicotinamide adenine dinucleotide" OR NADH)'
        if entity["canonical"] == "gold nanoparticle":
            return '("gold nanoparticle" OR AuNP OR "colloidal gold")'
        return f'"{entity["aliases"][0]}"'

    subjects = [subject_for(entity) for entity in intent.primary_entities]
    subject = " ".join(subjects)
    contexts = [c["canonical"] for c in intent.contexts]
    families = [{"purpose": "overview", "query": f"{subject} review mechanism"}]
    families += [{"purpose": value, "query": f"{subject} {value}"} for value in contexts[:5]]
    canonicals = {entity["canonical"] for entity in intent.primary_entities}
    if {"nad", "gold nanoparticle"}.issubset(canonicals):
        families += [
            {"purpose": "direct catalysis", "query": f'{subject} NADH oxidation catalysis nanozyme'},
            {"purpose": "size constraint", "query": f'{subject} "5 nm" particle size'},
            {"purpose": "CNS delivery", "query": f'{subject} brain CNS blood-brain barrier biodistribution'},
            {"purpose": "measured outcome", "query": f'{subject} intracellular NAD+/NADH ratio measurement'},
            {"purpose": "safety", "query": f'{subject} neurotoxicity aggregation dose'},
        ]
    if "als" in contexts:
        families.append({"purpose": "human evidence", "query": f"{subject} ALS clinical trial"})
        families.append({"purpose": "intervention", "query": f"{subject} precursor ALS NR NMN"})
    seen, result = set(), []
    for family in families:
        key = family["query"].lower()
        if key not in seen:
            seen.add(key); result.append(family)
    return result[:8]
