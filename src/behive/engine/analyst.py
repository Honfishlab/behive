"""Evidence-aware analytical layer for BeHive research missions."""
from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone

from behive.engine.db import connect
from behive.engine.llm import complete

log = logging.getLogger(__name__)

ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_analysis (
    mission_id TEXT PRIMARY KEY REFERENCES hive_missions(id),
    analysis JSONB NOT NULL,
    depth_status TEXT NOT NULL DEFAULT 'insufficient',
    source_count INTEGER NOT NULL DEFAULT 0,
    cited_source_count INTEGER NOT NULL DEFAULT 0,
    claim_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text or "", re.S)
        value = json.loads(match.group(0)) if match else {}
    return value if isinstance(value, dict) else {}


def analyze_mission(mission_id: str) -> dict:
    """Build comparative analysis while keeping sourced facts and deductions distinct."""
    with connect(read_only=True) as con:
        mission = con.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
        if not mission:
            raise ValueError(f"Mission {mission_id} not found")
        claims = con.execute(
            "SELECT claim,evidence,source_url,confidence,quality_score,verified FROM hive_claims "
            "WHERE mission_id=? AND (is_garbage=FALSE OR is_garbage IS NULL) ORDER BY quality_score DESC LIMIT 100",
            [mission_id],
        ).fetchall()
        docs = con.execute(
            "SELECT title,url,domain,raw_text,quality_score,harvest_method FROM hive_content "
            "WHERE mission_id=? ORDER BY quality_score DESC,word_count DESC LIMIT 60", [mission_id]
        ).fetchall()
    if not claims:
        raise RuntimeError("Analyst requires at least one finding")

    unique_urls = {row[2] for row in claims if row[2]}
    full_text_sources = sum(1 for row in docs if row[5] != 'snippet_fallback' and len(row[3] or '') >= 500)
    readiness = ("sufficient" if len(unique_urls) >= 8 and full_text_sources >= 4
                 else "limited" if len(unique_urls) >= 3 and full_text_sources >= 1
                 else "insufficient")
    claim_text = "\n".join(
        f"C{i+1} | confidence={float(c[3] or c[4] or 0):.2f} | verified={bool(c[5])} | url={c[2]}\n"
        f"claim={c[0]}\nevidence={c[1] or ''}" for i, c in enumerate(claims)
    )
    doc_text = "\n".join(
        f"S{i+1} | {d[0] or d[2]} | {d[1]} | method={d[5]} | quality={float(d[4] or 0):.2f}\n{(d[3] or '')[:900]}"
        for i, d in enumerate(docs[:30])
    )
    system = "You are a skeptical research analyst. Never disguise inference as sourced fact. Never invent evidence."
    prompt = f"""Research question: {mission[0]}
Evidence readiness: {readiness}; {len(claims)} claims; {len(unique_urls)} cited sources; {full_text_sources} full-text sources.

Create a decision-grade analysis from only the evidence below. Return one JSON object with exactly these keys:
executive_assessment (string), depth_status (insufficient|limited|sufficient),
rankings (array: subject, direction, magnitude 1-5, horizon, confidence 0-1, mechanism, evidence_claim_ids),
mechanisms (array: mechanism, cause, effect, evidence_claim_ids, confidence 0-1),
agreements (array: statement, evidence_claim_ids),
contradictions (array: issue, positions, evidence_claim_ids, resolution_needed),
scenarios (array: name, trigger, expected_effects, indicators, confidence 0-1),
deductions (array: deduction, reasoning, evidence_claim_ids, status='hypothesis', confidence 0-1),
limitations (array of strings), research_gaps (array: question, priority 1-10, why).

Rules: Rank comparatively, explain causal mechanisms, test alternative explanations, and make uncertainty prominent.
If evidence is too weak, say so and make deductions testable hypotheses. Evidence IDs must be C numbers from below.

CLAIMS
{claim_text}

SOURCE MATERIAL
{doc_text}"""
    analysis = _json(complete(prompt, stage="synth", system=system, max_tokens=5000, temperature=0.15, json_mode=True))
    analysis["depth_status"] = readiness
    analysis["evidence_metrics"] = {
        "claims": len(claims), "cited_sources": len(unique_urls), "documents_reviewed": len(docs),
        "full_text_sources": full_text_sources, "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    analysis.setdefault("limitations", [])
    for key in ("rankings", "mechanisms", "agreements", "contradictions", "scenarios", "deductions", "research_gaps"):
        if not isinstance(analysis.get(key), list):
            analysis[key] = []
    if not analysis["scenarios"]:
        subjects = [r.get("subject") for r in analysis["rankings"][:3] if isinstance(r, dict) and r.get("subject")]
        subject_text = ", ".join(subjects) or "the ranked product groups"
        analysis["scenarios"] = [
            {"name": "Baseline adoption", "trigger": "Current online-shopping adoption continues without a major step-change",
             "expected_effects": [f"Gradual effects across {subject_text}"], "indicators": ["online sales share", "delivery volume"], "confidence": 0.25},
            {"name": "Accelerated adoption", "trigger": "Faster automation, AI-assisted discovery, and fulfillment investment",
             "expected_effects": [f"Earlier and larger effects across {subject_text}"], "indicators": ["automation investment", "conversion rates", "fulfillment cost"], "confidence": 0.2},
            {"name": "Adoption constraint", "trigger": "Logistics costs, regulation, or consumer trust slow online-channel growth",
             "expected_effects": [f"Delayed or uneven effects across {subject_text}"], "indicators": ["returns cost", "delivery margins", "consumer trust"], "confidence": 0.2},
        ]
    if full_text_sources == 0:
        analysis["limitations"].insert(0, "No full-text sources were available; analysis is based on low-confidence source summaries.")

    with connect() as con:
        con.execute(ANALYSIS_SCHEMA)
        con.execute(
            "INSERT INTO hive_analysis (mission_id,analysis,depth_status,source_count,cited_source_count,claim_count,updated_at) "
            "VALUES (?,?,?,?,?,?,NOW()) ON CONFLICT(mission_id) DO UPDATE SET analysis=EXCLUDED.analysis,"
            "depth_status=EXCLUDED.depth_status,source_count=EXCLUDED.source_count,cited_source_count=EXCLUDED.cited_source_count,"
            "claim_count=EXCLUDED.claim_count,updated_at=NOW()",
            [mission_id, json.dumps(analysis), readiness, len(docs), len(unique_urls), len(claims)],
        )
        for gap in analysis.get("research_gaps", [])[:10]:
            if not isinstance(gap, dict) or not gap.get("question"):
                continue
            priority = max(1, min(10, int(gap.get("priority", 5))))
            question = str(gap["question"])[:1000]
            existing_gaps = con.execute(
                "SELECT gap_query FROM hive_gaps WHERE mission_id=? AND COALESCE(gap_type,'')='analyst'", [mission_id]
            ).fetchall()
            normalized = re.sub(r'[^a-z0-9 ]+', '', question.lower())
            if any(SequenceMatcher(None, normalized, re.sub(r'[^a-z0-9 ]+', '', row[0].lower())).ratio() >= 0.86
                   for row in existing_gaps):
                continue
            con.execute(
                "INSERT INTO hive_gaps (mission_id,gap_query,gap_type,priority,resolved) "
                "SELECT ?,?,'analyst',?,FALSE WHERE NOT EXISTS (SELECT 1 FROM hive_gaps WHERE mission_id=? AND gap_query=?)",
                [mission_id, question, priority, mission_id, question],
            )
        con.commit()
    return analysis
