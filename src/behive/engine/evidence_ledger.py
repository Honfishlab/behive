"""Durable, queryable audit trail for evidence-pipeline decisions."""
from __future__ import annotations

import json
from behive.engine.db import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_evidence_ledger (
  id BIGSERIAL PRIMARY KEY,
  mission_id VARCHAR NOT NULL,
  stage VARCHAR NOT NULL,
  item_type VARCHAR NOT NULL,
  item_id VARCHAR,
  status VARCHAR NOT NULL,
  reason VARCHAR,
  details JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_evidence_ledger_mission
ON hive_evidence_ledger(mission_id, created_at DESC);
CREATE TABLE IF NOT EXISTS hive_evidence_passages (
  id BIGSERIAL PRIMARY KEY,
  mission_id VARCHAR NOT NULL,
  source_url TEXT NOT NULL,
  claim_id BIGINT,
  passage TEXT NOT NULL,
  locator VARCHAR,
  evidence_type VARCHAR DEFAULT 'direct_quote',
  direct_support BOOLEAN DEFAULT FALSE,
  relevance_score DOUBLE PRECISION DEFAULT 0,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_evidence_passages_mission
ON hive_evidence_passages(mission_id, created_at DESC);
"""


def ensure_schema(db=None) -> None:
    owns = db is None
    db = db or connect()
    try:
        db.execute(SCHEMA)
        if owns:
            db.commit()
    finally:
        if owns:
            db.close()


def record(mission_id: str, stage: str, item_type: str, status: str, *,
           item_id: str = "", reason: str = "", details: dict | None = None,
           db=None) -> None:
    owns = db is None
    db = db or connect()
    try:
        ensure_schema(db)
        db.execute(
            "INSERT INTO hive_evidence_ledger "
            "(mission_id,stage,item_type,item_id,status,reason,details) "
            "VALUES (?,?,?,?,?,?,?::jsonb)",
            [mission_id, stage, item_type, str(item_id or ""), status,
             reason[:1000], json.dumps(details or {})],
        )
        if owns:
            db.commit()
    finally:
        if owns:
            db.close()
