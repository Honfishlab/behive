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
SUMMARY_SCHEMA_VERSION = "subjects-v2"


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
    fingerprint = hashlib.sha256((SUMMARY_SCHEMA_VERSION+json.dumps(source, sort_keys=True, default=str)).encode()).hexdigest()
    cached = db.execute("SELECT summary,generated_at FROM hive_living_summaries WHERE mission_id=? AND fingerprint=?", [mission_id,fingerprint]).fetchone()
    if cached and not force:
        db.close(); return {'mission_id':mission_id,'fingerprint':fingerprint,'generated_at':cached[1],'cached':True,**cached[0]}
    prompt = f"""Create the comprehensive living summary for this exploratory research mission.
The reader should understand the entire investigation easily without reading a database or article list.
Use at most FIVE top-level reading sections, but DO NOT limit the number of distinct subjects.
Within each top-level section create as many coherent subject clusters as required to account for EVERY supplied
question and finding. Do not merge materially different subjects merely to reduce their number.
Do not merely restate articles: explain relationships, patterns, tensions, limits, and why they matter.
Clearly distinguish observed findings, inference, hypothesis, and unknown. Never promote a hypothesis to a finding.
Write concise natural-language paragraphs. Each top-level section and subject needs a descriptive title.
For each SUBJECT include a stable id, related_question_ids, related_finding_indexes (zero-based indexes into the
supplied findings array), and evidence_urls so it remains traceable and can own
its evidence-boundary research program. A subject belongs in exactly one top-level section.

MISSION DATA: {json.dumps(source, default=str)[:60000]}

Return JSON: title, orientation (2-3 sentences), sections (1-5 items of title, narrative, subjects array).
Each subject: id, title, narrative, epistemic_note, related_question_ids array, related_finding_indexes array,
evidence_urls array.
Also return next_read (one sentence), coverage_note (one sentence)."""
    result = _parse(complete(prompt, stage='synth', system='You are a rigorous research editor producing readable, traceable living synthesis.', max_tokens=5000, temperature=.2, json_mode=True))
    result['sections'] = (result.get('sections') or [])[:5]
    for index, section in enumerate(result['sections']):
        subjects = section.get('subjects') or []
        if not subjects:  # Normalize an older/flat model response without losing content.
            subjects = [{'id':f'subject-{index+1}','title':section.get('title') or f'Subject {index+1}',
                         'narrative':section.get('narrative') or '',
                         'epistemic_note':section.get('epistemic_note') or '',
                         'related_question_ids':section.get('related_question_ids') or [],
                         'evidence_urls':section.get('evidence_urls') or []}]
        for subject_index, subject in enumerate(subjects):
            subject.setdefault('id', f'subject-{index+1}-{subject_index+1}')
            subject.setdefault('related_question_ids', []); subject.setdefault('related_finding_indexes', [])
            subject.setdefault('evidence_urls', [])
        section['subjects'] = subjects
    if not result['sections']:
        result['sections']=[{'title':'Research record','narrative':'Active questions and findings collected so far.','subjects':[]}]
    all_subjects=[subject for section in result['sections'] for subject in section.get('subjects',[])]
    used_questions={str(qid) for subject in all_subjects for qid in subject.get('related_question_ids',[])}
    missing_questions=[q for q in source['questions'] if str(q['id']) not in used_questions]
    if missing_questions:
        result['sections'][-1]['subjects'].append({'id':'coverage-active-questions','title':'Additional active research questions',
          'narrative':'These active probes remain part of the investigation: '+'; '.join(q['text'] for q in missing_questions),
          'epistemic_note':'These questions are retained explicitly because current evidence does not yet support merging them into another subject.',
          'related_question_ids':[q['id'] for q in missing_questions],'related_finding_indexes':[],'evidence_urls':[]})
    used_findings={int(fid) for subject in all_subjects for fid in subject.get('related_finding_indexes',[]) if str(fid).isdigit()}
    missing_findings=[(i,f) for i,f in enumerate(source['findings']) if i not in used_findings]
    for chunk_index in range(0,len(missing_findings),8):
        chunk=missing_findings[chunk_index:chunk_index+8]
        result['sections'][-1]['subjects'].append({'id':f'coverage-findings-{chunk_index//8+1}','title':'Additional documented findings',
          'narrative':'Documented observations retained for completeness: '+'; '.join(f['text'] for _,f in chunk),
          'epistemic_note':'These findings remain separately visible until stronger synthesis or connecting evidence is available.',
          'related_question_ids':[],'related_finding_indexes':[i for i,_ in chunk],
          'evidence_urls':list(dict.fromkeys(f['url'] for _,f in chunk if f.get('url')))})
    result.setdefault('title', mission[0]); result.setdefault('orientation', 'Research is still developing.')
    result.setdefault('next_read', 'Review the open questions and evidence links below.')
    result['metrics'] = {'questions':len(questions),'findings':len(findings),'open_frontiers':len(frontiers),'hypotheses':len(hypotheses)}
    db.execute("INSERT INTO hive_living_summaries (mission_id,fingerprint,summary,question_count,finding_count) VALUES (?,?,?::jsonb,?,?) ON CONFLICT (mission_id) DO UPDATE SET fingerprint=EXCLUDED.fingerprint,summary=EXCLUDED.summary,question_count=EXCLUDED.question_count,finding_count=EXCLUDED.finding_count,generated_at=NOW(),updated_at=NOW()", [mission_id,fingerprint,json.dumps(result),len(questions),len(findings)])
    saved = db.execute("SELECT generated_at FROM hive_living_summaries WHERE mission_id=?", [mission_id]).fetchone(); db.close()
    return {'mission_id':mission_id,'fingerprint':fingerprint,'generated_at':saved[0],'cached':False,**result}
