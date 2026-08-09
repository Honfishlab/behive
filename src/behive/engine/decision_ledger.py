"""Durable, source-linked audit trail for autonomous research decisions."""
from __future__ import annotations
import json
from behive.engine.db import connect

SCHEMA="""
CREATE TABLE IF NOT EXISTS hive_decision_ledger (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, subject_key VARCHAR,
 event_type VARCHAR NOT NULL, impact VARCHAR DEFAULT 'contextual', headline TEXT NOT NULL,
 explanation TEXT, before_state JSONB DEFAULT '{}'::jsonb, after_state JSONB DEFAULT '{}'::jsonb,
 confidence_before DOUBLE PRECISION, confidence_after DOUBLE PRECISION,
 source_url TEXT, observation_id INTEGER, affected_questions JSONB DEFAULT '[]'::jsonb,
 affected_hypotheses JSONB DEFAULT '[]'::jsonb, reversal_conditions TEXT,
 created_at TIMESTAMP DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_decision_mission_time ON hive_decision_ledger(mission_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_subject ON hive_decision_ledger(mission_id,subject_key,created_at DESC);
"""

def ensure_schema(db=None):
    owned=db is None; db=db or connect()
    for statement in (x.strip() for x in SCHEMA.split(';') if x.strip()): db.execute(statement)
    if owned: db.close()

def record(mission_id,event_type,headline,explanation='',*,subject_key=None,impact='contextual',before=None,after=None,
           confidence_before=None,confidence_after=None,source_url=None,observation_id=None,questions=None,
           hypotheses=None,reversal_conditions=''):
    db=connect(); ensure_schema(db)
    db.execute("INSERT INTO hive_decision_ledger (mission_id,subject_key,event_type,impact,headline,explanation,before_state,after_state,confidence_before,confidence_after,source_url,observation_id,affected_questions,affected_hypotheses,reversal_conditions) VALUES (?,?,?,?,?,?,?::jsonb,?::jsonb,?,?,?,?,?::jsonb,?::jsonb,?)",
      [mission_id,subject_key,event_type,impact,headline,explanation,json.dumps(before or {}),json.dumps(after or {}),confidence_before,confidence_after,source_url,observation_id,json.dumps(questions or []),json.dumps(hypotheses or []),reversal_conditions]); db.close()

def list_entries(mission_id,subject_key=None,limit=300):
    db=connect(); ensure_schema(db)
    sql="SELECT id,subject_key,event_type,impact,headline,explanation,before_state,after_state,confidence_before,confidence_after,source_url,observation_id,affected_questions,affected_hypotheses,reversal_conditions,created_at FROM hive_decision_ledger WHERE mission_id=?"
    params=[mission_id]
    if subject_key: sql+=" AND subject_key=?"; params.append(subject_key)
    sql+=" ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    rows=db.execute(sql,params).fetchall(); db.close()
    return [{'id':r[0],'subject_key':r[1],'event_type':r[2],'impact':r[3],'headline':r[4],'explanation':r[5],
      'before':r[6] or {},'after':r[7] or {},'confidence_before':r[8],'confidence_after':r[9],
      'source_url':r[10],'observation_id':r[11],'affected_questions':r[12] or [],'affected_hypotheses':r[13] or [],
      'reversal_conditions':r[14],'created_at':r[15]} for r in rows]
