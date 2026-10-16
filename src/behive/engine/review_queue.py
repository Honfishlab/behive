"""Durable human review decisions for evidence-backed findings."""
from __future__ import annotations
import json
from behive.engine.db import connect

SCHEMA="""
CREATE TABLE IF NOT EXISTS hive_claim_reviews (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, claim_id INTEGER NOT NULL,
 decision VARCHAR NOT NULL, note TEXT, reviewer VARCHAR DEFAULT 'user',
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,claim_id));
CREATE INDEX IF NOT EXISTS idx_claim_reviews_mission ON hive_claim_reviews(mission_id,decision,updated_at DESC);
ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS integrity_status VARCHAR DEFAULT 'unaudited';
ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS integrity_score DOUBLE PRECISION DEFAULT 0;
ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS integrity_details JSONB DEFAULT '{}'::jsonb;
"""
def ensure_schema(db=None):
    owned=db is None; db=db or connect()
    for s in (x.strip() for x in SCHEMA.split(';') if x.strip()): db.execute(s)
    if owned: db.close()

def list_reviews(mission_id: str) -> dict:
    db=connect(); ensure_schema(db)
    mission=db.execute("SELECT project_id FROM hive_missions WHERE id=?",[mission_id]).fetchone()
    if not mission:
        db.close(); raise ValueError('Mission not found')
    rows=db.execute("SELECT c.id,c.claim,c.evidence,c.source_url,c.claim_type,c.confidence,c.quality_score,c.verified,c.source_tier,c.quality_details,r.decision,r.note,r.updated_at,COALESCE(c.integrity_status,'unaudited'),COALESCE(c.integrity_score,0),COALESCE(c.integrity_details,'{}'::jsonb),COALESCE(c.is_garbage,FALSE) FROM hive_claims c LEFT JOIN hive_claim_reviews r ON r.mission_id=c.mission_id AND r.claim_id=c.id WHERE c.mission_id=? ORDER BY CASE WHEN r.decision IS NULL THEN 0 WHEN r.decision='needs_more' THEN 1 ELSE 2 END,c.quality_score DESC,c.id DESC LIMIT 500",[mission_id]).fetchall(); db.close()
    items=[{'id':r[0],'claim':r[1],'evidence':r[2] or '','source_url':r[3] or '','claim_type':r[4] or 'finding','confidence':r[5] or 0,'quality':r[6] or 0,'verified':bool(r[7]),'source_tier':r[8],'quality_details':r[9] or {},'decision':r[10] or 'pending','note':r[11] or '','reviewed_at':r[12],'integrity_status':r[13],'integrity_score':r[14],'integrity_details':r[15] or {},'quarantined':bool(r[16])} for r in rows]
    active=[x for x in items if not x['quarantined']]
    return {'mission_id':mission_id,'project_id':mission[0],'items':items,'metrics':{
        'total':len(active),'quarantined':len(items)-len(active),
        'pending':sum(x['decision']=='pending' for x in active),
        'approved':sum(x['decision']=='approved' for x in active),
        'rejected':sum(x['decision']=='rejected' for x in active),
        'needs_more':sum(x['decision']=='needs_more' for x in active)}}

def decide(mission_id: str, claim_id: int, decision: str, note: str='') -> dict:
    if decision not in {'approved','rejected','needs_more','pending'}: raise ValueError('Unsupported review decision')
    db=connect(); ensure_schema(db); claim=db.execute("SELECT claim FROM hive_claims WHERE id=? AND mission_id=?",[claim_id,mission_id]).fetchone()
    if not claim: db.close(); raise ValueError('Finding not found')
    db.execute("INSERT INTO hive_claim_reviews (mission_id,claim_id,decision,note) VALUES (?,?,?,?) ON CONFLICT (mission_id,claim_id) DO UPDATE SET decision=EXCLUDED.decision,note=EXCLUDED.note,updated_at=NOW()",[mission_id,claim_id,decision,note[:2000]]); db.close()
    from behive.engine.decision_ledger import record
    record(mission_id,'human_review',f'{decision.replace("_"," ").title()}: {claim[0][:240]}',note or 'Human evidence review decision',impact='supports' if decision=='approved' else 'weakens' if decision=='rejected' else 'changes',before={'review':'pending'},after={'review':decision},reversal_conditions='Revisit if the source, extraction, or contradictory evidence materially changes.')
    return {'claim_id':claim_id,'decision':decision}
