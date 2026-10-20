"""Canonical research intent and relevance scoring.

This module is deliberately deterministic.  LLMs may help phrase questions,
but evidence admission must remain explainable and testable.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


_ALIASES = {
    "igfbp7": ["igfbp7", "igfbp-7", "insulin-like growth factor binding protein 7", "angiomodulin"],
    "hmb": ["hmb", "beta-hydroxy-beta-methylbutyrate", "beta hydroxy beta methylbutyrate"],
    "igf-1": ["igf-1", "igf1", "insulin-like growth factor 1", "insulin like growth factor i"],
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
    "reversal": ["reversal", "recovery", "reverser"],
    "genetics": ["genetic", "genomic", "eqtl", "variant", "rs4242007", "igfbp7-as1"],
    "biomarker": ["biomarker", "serum", "plasma", "blood", "csf", "expression", "transcript"],
    "trial": ["clinical trial", "registered trial", "clinicaltrials.gov", "nct07589764"],
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
    mode: str = "intersection"
    primary_entities: list[dict] = field(default_factory=list)
    contexts: list[dict] = field(default_factory=list)
    excluded_senses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_intent(topic: str) -> ResearchIntent:
    normalized = _norm(topic)
    portfolio_markers = ("broad evidence", "evidence map", "across human", "published and registered",
                         "separate direct", "research questions")
    mode = "portfolio" if any(marker in normalized for marker in portfolio_markers) else "intersection"
    entities = []
    for canonical, aliases in _ALIASES.items():
        if any(_contains(normalized, alias) for alias in aliases):
            entities.append({"canonical": canonical, "aliases": aliases})
    if not entities:
        # Acronyms are often the most information-bearing biomedical subjects.
        acronyms = re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9-]{1,8}\+?)(?![A-Za-z0-9])", topic or "")
        seen_acronyms = set()
        for value in acronyms:
            canonical = value.lower()
            if canonical in seen_acronyms:
                continue
            seen_acronyms.add(canonical)
            entities.append({"canonical": canonical, "aliases": [canonical]})
            if len(entities) >= 3:
                break
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
    return ResearchIntent(topic=topic, mode=mode, primary_entities=entities, contexts=contexts, excluded_senses=excluded)


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
    scoring_entity_ratio = 1.0 if intent.mode == "portfolio" and entity_hits else entity_ratio
    context_ratio = len(context_hits) / max(1, len(intent.contexts)) if intent.contexts else 1.0
    score = max(0.0, min(1.0, .7 * scoring_entity_ratio + .3 * context_ratio - (.8 if excluded else 0)))
    # When the mission names contexts (disease, mechanism, outcome), entity-only
    # mentions are discovery noise, not admissible evidence.
    specific_context = bool({"als", "neurodegeneration"}.intersection(context_hits))
    contextual_support = (not intent.contexts or specific_context or len(context_hits) >= 2
                          or (bool(title_entity_hits) and bool(context_hits)))
    # Compound questions need intersection evidence.  A paper about only NAD,
    # or only gold nanoparticles, may inspire a child path but cannot support
    # the parent mechanism and must not enter its evidence corpus.
    complete_entity_match = bool(entity_hits) and (
        intent.mode == "portfolio" or len(intent.primary_entities) == 1
        or len(entity_hits) == len(intent.primary_entities)
    )
    if intent.mode == "portfolio" and title_entity_hits:
        contextual_support = True
    accepted = complete_entity_match and not excluded and contextual_support
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
    enough_joint = intent.mode == "portfolio" or len(required) < 2 or len(joint) >= 1
    enough_coverage = not required or all(value in covered for value in required)
    adequate = bool(records) and enough_relevant and enough_joint and enough_coverage
    return {
        "adequate": adequate,
        "records": len(records),
        "relevant_records": len(relevant),
        "joint_entity_records": len(joint),
        "required_entities": required,
        "intent_mode": intent.mode,
        "covered_entities": covered,
        "missing_entities": [value for value in required if value not in covered],
        "reason": ("adequate" if adequate else
                   "no records retrieved" if not records else
                   "portfolio branches are missing from retrieval" if intent.mode == "portfolio" and not enough_coverage else
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
    if intent.mode == "portfolio":
        canonicals = {entity["canonical"] for entity in intent.primary_entities}
        if {"igfbp7", "hmb"}.issubset(canonicals):
            return [
                {"purpose": "direct ALS", "query": 'IGFBP7 amyotrophic lateral sclerosis ALS'},
                {"purpose": "reversal genetics", "query": 'IGFBP7 rs4242007 ALS reversal genetics eQTL IGFBP7-AS1'},
                {"purpose": "tissue expression", "query": 'IGFBP7 ALS spinal cord motor neuron iPSC serum CSF expression'},
                {"purpose": "mechanism", "query": 'IGFBP7 IGF-1 signaling motor neuron mechanism'},
                {"purpose": "HMB intervention", "query": 'HMB beta-hydroxy-beta-methylbutyrate IGFBP7'},
                {"purpose": "HMB adjacent evidence", "query": 'HMB IGF-1 muscle neurological ALS'},
                {"purpose": "trial verification", "query": 'NCT07589764 IGFBP7 HMB ALS clinical trial'},
                {"purpose": "datasets", "query": 'IGFBP7 IGFBP7-AS1 ALS transcriptomics single cell GEO'},
            ]
        families = [{"purpose": entity["canonical"], "query": f"{subject_for(entity)} review evidence"}
                    for entity in intent.primary_entities]
        return families[:8]
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


def build_competing_hypotheses(topic: str) -> list[dict]:
    """Return clearly labeled test targets, never asserted conclusions."""
    entities = {item["canonical"] for item in build_intent(topic).primary_entities}
    if not {"igfbp7", "hmb"}.issubset(entities):
        return []
    return [
        {
            "hypothesis": "IGFBP7 is a causal modifier of an ALS-reversal phenotype.",
            "competes_with": "The association reflects linkage, selection, diagnostic heterogeneity, or chance.",
            "falsified_by": "Well-powered replication and functional perturbation show no genotype-expression-phenotype chain.",
        },
        {
            "hypothesis": "ALS-associated IGFBP7 measurements reflect motor-neuron or CNS disease biology.",
            "competes_with": "Circulating IGFBP7 reflects peripheral tissue injury, vascular biology, age, or systemic inflammation.",
            "falsified_by": "Paired blood/CSF/tissue studies show no compartment concordance or ALS-specific signal.",
        },
        {
            "hypothesis": "HMB changes an IGFBP7-linked pathway relevant to ALS.",
            "competes_with": "Any HMB-associated IGFBP7 change is exercise/muscle-specific and does not translate to CNS biology.",
            "falsified_by": "Controlled studies show no target change, no dose-response, or no CNS/motor-neuron pathway engagement.",
        },
    ]
