"""Supported passage-backed claim extraction for the research pipeline."""
from __future__ import annotations

import json
import re

from behive.engine.db import connect
from behive.engine.evidence_ledger import ensure_schema, record
from behive.engine.llm import complete
from behive.engine.research_intent import assess_relevance


def _parse(value: str) -> list[dict]:
    try:
        data = json.loads(value)
    except Exception:
        match = re.search(r"\{.*\}|\[.*\]", value or "", re.S)
        data = json.loads(match.group(0)) if match else []
    if isinstance(data, dict):
        data = data.get("claims", [])
    return data if isinstance(data, list) else []


def extract_mission_claims(mission_id: str, limit: int = 30) -> int:
    """Extract claims only when an exact source passage supports them."""
    with connect() as db:
        ensure_schema(db)
        topic_row = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
        if not topic_row:
            return 0
        topic = topic_row[0]
        rows = db.execute(
            "SELECT c.url,c.title,c.raw_text,c.harvest_method,COALESCE(s.evidence_tier,9),"
            "COALESCE(s.verification_status,''),COALESCE(s.is_primary,FALSE) "
            "FROM hive_content c LEFT JOIN hive_sources s ON s.mission_id=c.mission_id AND s.url=c.url "
            "WHERE c.mission_id=? AND c.word_count>=40 "
            "AND COALESCE(c.harvest_method,'') NOT IN ('snippet_fallback','web_httpx_error') "
            "ORDER BY COALESCE(s.score_relevance,0) DESC,COALESCE(s.evidence_tier,9),"
            "c.quality_score DESC,c.word_count DESC LIMIT ?", [mission_id, limit]
        ).fetchall()
        docs = [row for row in rows if assess_relevance(topic, row[1] or "", row[2] or "")["accepted"]]
        if not docs:
            record(mission_id, "extract", "corpus", "failed", reason="no relevant documents after admission", db=db)
            db.commit()
            return 0

        saved = 0
        for offset in range(0, len(docs), 8):
            batch = docs[offset:offset + 8]
            source_text = "\n\n".join(
                f"SOURCE {i+1}\nURL: {doc[0]}\nTITLE: {doc[1] or ''}\nTEXT:\n{(doc[2] or '')[:6500]}"
                for i, doc in enumerate(batch)
            )
            prompt = f"""Research intent: {topic}

Extract only findings directly supported by the supplied source text. Return JSON with a claims array.
Each claim requires: claim, source_index, exact_quote, locator, limitation, confidence, direct_support.
The exact_quote must be copied verbatim from that source and must entail the complete claim. Do not infer
a disease, intervention, population, or outcome absent from the quote. Return an empty array when support
is only topical or indirect. Maximum 8 claims.

{source_text}"""
            try:
                items = _parse(complete(prompt, stage="process", max_tokens=2200,
                                        temperature=0, json_mode=True))
            except Exception as exc:
                record(mission_id, "extract", "batch", "failed", reason=str(exc),
                       details={"offset": offset}, db=db)
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("direct_support") is not True:
                    continue
                try:
                    index = int(item.get("source_index")) - 1
                except (TypeError, ValueError):
                    continue
                if index < 0 or index >= len(batch):
                    continue
                doc = batch[index]
                claim = str(item.get("claim") or "").strip()
                quote = str(item.get("exact_quote") or "").strip()
                # Exact substring verification makes citations auditable and
                # prevents plausible-looking paraphrases from becoming facts.
                if not claim or len(quote) < 20 or quote.lower() not in (doc[2] or "").lower():
                    continue
                relevance = assess_relevance(topic, claim, quote)
                if not relevance["accepted"]:
                    record(mission_id, "extract", "candidate_claim", "rejected",
                           reason="claim failed mission relevance", details=relevance, db=db)
                    continue
                duplicate = db.execute(
                    "SELECT 1 FROM hive_claims WHERE mission_id=? AND LOWER(claim)=LOWER(?) "
                    "AND COALESCE(is_garbage,FALSE)=FALSE LIMIT 1", [mission_id, claim[:2000]],
                ).fetchone()
                if duplicate:
                    record(mission_id, "extract", "candidate_claim", "duplicate",
                           reason="equivalent active claim already exists", details=relevance, db=db)
                    continue
                confidence = max(.35, min(.78, float(item.get("confidence") or .5)))
                details = {"exact_quote": quote, "locator": item.get("locator") or "source text",
                           "limitation": item.get("limitation") or "", "direct_support": True,
                           "relevance": relevance}
                inserted = db.execute(
                    "INSERT INTO hive_claims (mission_id,claim,evidence,source_url,claim_type,confidence,"
                    "quality_score,quality_details,source_operation,source_tier,verified,evidence_flags,"
                    "is_garbage,extracted_at) VALUES (?,?,?,?,?,?,?,?::jsonb,'passage_extractor',?,FALSE,"
                    "'direct_quote;source_linked',FALSE,NOW()) RETURNING id",
                    [mission_id, claim[:2000], quote[:5000], doc[0], "authority_finding", confidence,
                     confidence, json.dumps(details), max(1, min(4, int(doc[4] or 4)))],
                ).fetchone()
                claim_id = inserted[0]
                db.execute(
                    "INSERT INTO hive_evidence_passages (mission_id,source_url,claim_id,passage,locator,"
                    "direct_support,relevance_score,metadata) VALUES (?,?,?,?,?,TRUE,?,?::jsonb)",
                    [mission_id, doc[0], claim_id, quote[:10000], details["locator"],
                     relevance["score"], json.dumps({"limitation": details["limitation"]})],
                )
                record(mission_id, "extract", "claim", "admitted", item_id=str(claim_id),
                       reason="exact passage verified", details=relevance, db=db)
                saved += 1
        db.execute("UPDATE hive_missions SET sources_processed=?,total_facts=?,process_done_at=NOW() WHERE id=?",
                   [len(docs), saved, mission_id])
        db.commit()
        return saved
