"""Living research projects: question trees, evidence links, and discovery planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


STOPWORDS = {
    "about", "after", "before", "could", "from", "have", "into", "should",
    "their", "there", "these", "those", "what", "when", "where", "which",
    "while", "with", "would", "this", "that", "than", "were", "will",
}


def keywords(text: str) -> set[str]:
    """Return stable terms used to connect otherwise separate findings."""
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text)
        if token.lower() not in STOPWORDS
    }


def connection_strength(left: str, right: str) -> float:
    """Jaccard similarity, suitable for explainable cross-thread suggestions."""
    a, b = keywords(left), keywords(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


@dataclass(frozen=True)
class QuestionSignal:
    id: str
    question: str
    depth: int
    priority: float = 0.5
    confidence: float = 0.0
    finding_count: int = 0
    contradiction_count: int = 0
    stale_days: int = 0

    @property
    def discovery_score(self) -> float:
        """Favor high-value gaps, contradictions, and stale under-researched nodes."""
        uncertainty = 1.0 - min(max(self.confidence, 0.0), 1.0)
        evidence_gap = 1.0 / (1.0 + max(self.finding_count, 0))
        contradiction = min(self.contradiction_count / 3.0, 1.0)
        staleness = min(self.stale_days / 30.0, 1.0)
        depth_cost = 1.0 / (1.0 + max(self.depth, 0) * 0.15)
        score = (
            0.30 * self.priority
            + 0.25 * uncertainty
            + 0.20 * evidence_gap
            + 0.15 * contradiction
            + 0.10 * staleness
        ) * depth_cost
        return round(score, 4)


def rank_frontier(signals: Iterable[QuestionSignal], limit: int = 5) -> list[QuestionSignal]:
    """Choose the next research frontier deterministically."""
    return sorted(signals, key=lambda item: (-item.discovery_score, item.depth, item.id))[:limit]


def suggest_followups(question: str, entities: Iterable[str] = ()) -> list[dict]:
    """Create a tiered follow-up set without pretending an LLM response is evidence."""
    topic = question.strip().rstrip("?")
    entity_list = [e.strip() for e in entities if e and e.strip()][:3]
    suggestions = [
        ("mechanism", f"What mechanisms best explain {topic}?"),
        ("evidence", f"What primary evidence would falsify the leading account of {topic}?"),
        ("change", f"What has changed recently that could alter the answer to {topic}?"),
        ("countercase", f"Which credible sources disagree about {topic}, and why?"),
    ]
    suggestions.extend(
        ("connection", f"How does {entity} change or constrain {topic}?")
        for entity in entity_list
    )
    return [
        {"kind": kind, "question": text, "rationale": "Generated discovery lead; requires sourced research."}
        for kind, text in suggestions
    ]
