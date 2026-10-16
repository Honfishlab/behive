"""Mission-level relevance checks for extracted claims."""
from __future__ import annotations

import json

from behive.engine.research_intent import assess_relevance
from behive.engine.evidence_ledger import ensure_schema, record
from behive.engine.db import connect


def claim_is_relevant(topic: str, claim: str, evidence: str = "") -> bool:
    """Return whether a claim/evidence pair addresses the mission topic."""
    return bool(assess_relevance(topic or "", claim or "", evidence or "")["accepted"])


def quarantine_irrelevant_claims(mission_id: str) -> dict:
    """Quarantine off-topic claims without deleting the audit records."""
    with connect() as db:
        row = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
        if not row:
            raise ValueError(f"Mission {mission_id} not found")
        topic = row[0] or ""
        ensure_schema(db)
        claims = db.execute(
            "SELECT id,claim,evidence,COALESCE(is_garbage,FALSE),COALESCE(integrity_status,'') "
            "FROM hive_claims WHERE mission_id=? AND "
            "(COALESCE(is_garbage,FALSE)=FALSE OR integrity_status='off_topic')", [mission_id]
        ).fetchall()
        rejected, restored = [], []
        for claim_id, claim, evidence, is_garbage, integrity_status in claims:
            assessment = assess_relevance(topic, claim, evidence)
            if assessment["accepted"]:
                if is_garbage and integrity_status == "off_topic":
                    db.execute(
                        "UPDATE hive_claims SET is_garbage=FALSE, integrity_status='unaudited', "
                        "integrity_score=NULL, integrity_details=COALESCE(integrity_details,'{}'::jsonb)||?::jsonb, "
                        "quality_details=COALESCE(quality_details,'{}'::jsonb)||?::jsonb WHERE id=? AND mission_id=?",
                        [json.dumps({"topic_relevance_restored": True, "assessment": assessment}),
                         json.dumps({"topic_relevance_restored": True}), claim_id, mission_id],
                    )
                    restored.append(claim_id)
                    record(mission_id, "integrity", "claim", "restored", item_id=str(claim_id),
                           reason="canonical intent re-evaluation accepted claim", details=assessment, db=db)
                continue
            rejected.append(claim_id)
            details = json.dumps({
                "topic_relevance_quarantined": True,
                "reason": "Claim does not contain the mission subject and independent context",
            })
            db.execute(
                "UPDATE hive_claims SET is_garbage=TRUE, integrity_status='off_topic', "
                "integrity_score=0, integrity_details=COALESCE(integrity_details,'{}'::jsonb)||?::jsonb, "
                "quality_details=COALESCE(quality_details,'{}'::jsonb)||?::jsonb "
                "WHERE id=? AND mission_id=?",
                [details, details, claim_id, mission_id],
            )
            record(mission_id, "integrity", "claim", "quarantined", item_id=str(claim_id),
                   reason="claim failed canonical entity/context relevance", details=assessment, db=db)
        db.commit()
    return {"examined": len(claims), "quarantined": len(rejected), "restored": len(restored),
            "claim_ids": rejected, "restored_ids": restored}
