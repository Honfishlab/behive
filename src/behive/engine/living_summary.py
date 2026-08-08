"""Cached, automatically invalidated plain-language mission summaries."""
from __future__ import annotations

import hashlib
import json
import re

from behive.engine.db import connect
from behive.engine.frontier import ensure_schema as ensure_frontier_schema
from behive.engine.llm import complete

SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_living_summaries (
 mission_id VARCHAR PRIMARY KEY, fingerprint VARCHAR NOT NULL, summary JSONB NOT NULL,
 question_count INTEGER DEFAULT 0, finding_count INTEGER DEFAULT 0,
 generated_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW());
"""


def ensure_schema(db=None):
    owned = db is None
    db = db or connect()
    db.execute(SCHEMA)
    if owned: db.close()


def _parse(raw: str) -> dict:
    raw = (raw or '').strip()
    if raw.startswith('```'): raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
    try: return json.loads(raw)
    except Exception:
        match = re.search(r'\{.*\}', raw, re.S)
        if match: return json.loads(match.group(0))
        raise ValueError('Summary model did not return valid JSON')


def get_living_summary(mission_id: str, force: bool = False) -> dict:
    ensure_frontier_schema()
    db = connect(); ensure_schema(db)
    mission = db.execute("SELECT topic,status,phase FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    if not mission: db.close(); raise ValueError('Mission not found')
    project = db.execute("SELECT id FROM hive_projects WHERE LOWER(TRIM(root_question))=LOWER(TRIM(?)) LIMIT 1", [mission[0]]).fetchone()
    questions = db.execute("SELECT id,parent_id,question,depth,status,priority FROM hive_questions WHERE project_id=? AND status!='archived' ORDER BY depth,priority DESC", [project[0]]).fetchall() if project else []
    findings = db.execute("SELECT claim,evidence,source_url,confidence,quality_score,claim_type FROM hive_claims WHERE mission_id=? AND (is_garbage=FALSE OR is_garbage IS NULL) ORDER BY quality_score DESC LIMIT 500", [mission_id]).fetchall()
    frontiers = db.execute("SELECT title,rationale,absence_type,priority FROM hive_frontiers WHERE mission_id=? AND status='open' ORDER BY priority DESC LIMIT 40", [mission_id]).fetchall()
    hypotheses = db.execute("SELECT name,statement,reasoning,status FROM hive_hypothesis_paths WHERE mission_id=? ORDER BY updated_at DESC LIMIT 30", [mission_id]).fetchall()
    source = {
      'topic': mission[0], 'status': mission[1], 'phase': mission[2],
      'questions': [{'id':r[0],'parent_id':r[1],'text':r[2],'depth':r[3],'status':r[4],'priority':r[5]} for r in questions],
      'findings': [{'text':r[0],'evidence':r[1],'url':r[2],'confidence':r[3],'quality':r[4],'type':r[5]} for r in findings],
      'frontiers': [{'title':r[0],'rationale':r[1],'absence_type':r[2],'priority':r[3]} for r in frontiers],
      'hypotheses': [{'name':r[0],'statement':r[1],'reasoning':r[2],'status':r[3]} for r in hypotheses],
    }
    fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()
    cached = db.execute("SELECT summary,generated_at FROM hive_living_summaries WHERE mission_id=? AND fingerprint=?", [mission_id,fingerprint]).fetchone()
    if cached and not force:
        db.close(); return {'mission_id':mission_id,'fingerprint':fingerprint,'generated_at':cached[1],'cached':True,**cached[0]}
    prompt = f"""Create the comprehensive living summary for this exploratory research mission.
The reader should understand the entire investigation easily without reading a database or article list.
Use at most FIVE sections. Account for EVERY supplied question and finding, merging repetition naturally.
Do not merely restate articles: explain relationships, patterns, tensions, limits, and why they matter.
Clearly distinguish observed findings, inference, hypothesis, and unknown. Never promote a hypothesis to a finding.
Write concise natural-language paragraphs. Each section should have a useful descriptive title, not generic labels.
For each section include related_question_ids and evidence_urls so the interface can trace it to underlying material.

MISSION DATA: {json.dumps(source, default=str)[:60000]}

Return JSON: title, orientation (2-3 sentences), sections (1-5 items of title, narrative, epistemic_note,
related_question_ids array, evidence_urls array), next_read (one sentence), coverage_note (one sentence)."""
    result = _parse(complete(prompt, stage='synth', system='You are a rigorous research editor producing readable, traceable living synthesis.', max_tokens=5000, temperature=.2, json_mode=True))
    result['sections'] = (result.get('sections') or [])[:5]
    result.setdefault('title', mission[0]); result.setdefault('orientation', 'Research is still developing.')
    result.setdefault('next_read', 'Review the open questions and evidence links below.')
    result['metrics'] = {'questions':len(questions),'findings':len(findings),'open_frontiers':len(frontiers),'hypotheses':len(hypotheses)}
    db.execute("INSERT INTO hive_living_summaries (mission_id,fingerprint,summary,question_count,finding_count) VALUES (?,?,?::jsonb,?,?) ON CONFLICT (mission_id) DO UPDATE SET fingerprint=EXCLUDED.fingerprint,summary=EXCLUDED.summary,question_count=EXCLUDED.question_count,finding_count=EXCLUDED.finding_count,generated_at=NOW(),updated_at=NOW()", [mission_id,fingerprint,json.dumps(result),len(questions),len(findings)])
    saved = db.execute("SELECT generated_at FROM hive_living_summaries WHERE mission_id=?", [mission_id]).fetchone(); db.close()
    return {'mission_id':mission_id,'fingerprint':fingerprint,'generated_at':saved[0],'cached':False,**result}
