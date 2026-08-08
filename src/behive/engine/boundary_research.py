"""Targeted evidence-acquisition programs for living-summary boundaries."""
from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha1

from behive.engine.authority import ingest_authority_sources
from behive.engine.db import connect
from behive.engine.frontier import ensure_schema as ensure_frontier_schema
from behive.engine.llm import complete

SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_boundary_programs (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, section_key VARCHAR NOT NULL,
 title TEXT NOT NULL, boundary TEXT NOT NULL, status VARCHAR DEFAULT 'queued', stage VARCHAR DEFAULT 'queued',
 message TEXT, plan JSONB DEFAULT '{}'::jsonb, metrics JSONB DEFAULT '{}'::jsonb, error_message TEXT,
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(), completed_at TIMESTAMP,
 UNIQUE(mission_id,section_key));
CREATE TABLE IF NOT EXISTS hive_boundary_observations (
 id SERIAL PRIMARY KEY, program_id VARCHAR NOT NULL, claim TEXT NOT NULL,
 organism TEXT, substrate TEXT, process_conditions TEXT, measurement TEXT, outcome TEXT, population TEXT,
 evidence_class VARCHAR NOT NULL, absence_class VARCHAR, source_url TEXT, source_title TEXT,
 source_family VARCHAR, quality DOUBLE PRECISION DEFAULT 0, limitations JSONB DEFAULT '[]'::jsonb,
 created_at TIMESTAMP DEFAULT NOW(), UNIQUE(program_id,claim,source_url));
CREATE INDEX IF NOT EXISTS idx_boundary_program_mission ON hive_boundary_programs(mission_id,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_boundary_observation_program ON hive_boundary_observations(program_id,evidence_class);
"""


def ensure_schema(db=None):
    owned = db is None; db = db or connect()
    for statement in (x.strip() for x in SCHEMA.split(';') if x.strip()): db.execute(statement)
    if owned: db.close()


def _parse(raw):
    raw = (raw or '').strip()
    if raw.startswith('```'): raw = raw.split('\n',1)[1].rsplit('```',1)[0]
    try: return json.loads(raw)
    except Exception:
        match = re.search(r'\{.*\}|\[.*\]', raw, re.S)
        return json.loads(match.group(0)) if match else {}


def _tokens(value):
    return set(re.findall(r'[a-z0-9]{3,}', (value or '').lower()))


def create_program(mission_id: str, section_key: str, title: str, boundary: str) -> dict:
    ensure_frontier_schema(); db = connect(); ensure_schema(db)
    mission = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    if not mission: db.close(); raise ValueError('Mission not found')
    existing = db.execute("SELECT id,status FROM hive_boundary_programs WHERE mission_id=? AND section_key=?", [mission_id,section_key]).fetchone()
    if existing:
        db.execute("UPDATE hive_boundary_programs SET title=?,boundary=?,status='queued',stage='queued',message='Queued for targeted evidence acquisition',error_message=NULL,updated_at=NOW(),completed_at=NULL WHERE id=?", [title,boundary,existing[0]])
        pid = existing[0]
    else:
        pid = 'boundary_'+uuid.uuid4().hex[:18]
        db.execute("INSERT INTO hive_boundary_programs (id,mission_id,section_key,title,boundary,message) VALUES (?,?,?,?,?,'Queued for targeted evidence acquisition')", [pid,mission_id,section_key,title,boundary])
    # Make the boundary visible in the question tree and continuous frontier controller.
    project = db.execute("SELECT id FROM hive_projects WHERE LOWER(TRIM(root_question))=LOWER(TRIM(?)) LIMIT 1", [mission[0]]).fetchone()
    question_id = 'question_'+sha1(f'{mission_id}|boundary|{section_key}'.encode()).hexdigest()[:16]
    if project:
        root = db.execute("SELECT id FROM hive_questions WHERE project_id=? AND parent_id IS NULL LIMIT 1", [project[0]]).fetchone()
        db.execute("INSERT INTO hive_questions (id,project_id,parent_id,question,depth,kind,priority,status) VALUES (?,?,?,?,1,'evidence_boundary',.9,'researching') ON CONFLICT (id) DO UPDATE SET question=EXCLUDED.question,priority=.9,status='researching'", [question_id,project[0],root[0] if root else None,f'Deepen evidence boundary: {title}'])
    frontier_id = 'frontier_'+sha1(f'{mission_id}|boundary|{section_key}'.encode()).hexdigest()[:18]
    db.execute("INSERT INTO hive_frontiers (id,mission_id,title,frontier_type,rationale,absence_type,evidence_search,scores,priority,status) VALUES (?,?,?,'evidence_boundary',?,'unresolved_boundary',?::jsonb,?::jsonb,.95,'open') ON CONFLICT (id) DO UPDATE SET rationale=EXCLUDED.rationale,evidence_search=EXCLUDED.evidence_search,priority=.95,status='open',updated_at=NOW()", [frontier_id,mission_id,title,boundary,json.dumps({'program_id':pid,'boundary':boundary}),json.dumps({'novelty':.7,'testability':.9,'impact':.85,'evidence_strength':.05})])
    db.commit(); db.close()
    return {'id':pid,'mission_id':mission_id,'section_key':section_key,'status':'queued','question_id':question_id,'frontier_id':frontier_id}


def _update(pid, status, stage, message, *, plan=None, metrics=None, error=None, complete=False):
    db=connect(); ensure_schema(db)
    db.execute("UPDATE hive_boundary_programs SET status=?,stage=?,message=?,plan=COALESCE(?::jsonb,plan),metrics=COALESCE(?::jsonb,metrics),error_message=?,updated_at=NOW(),completed_at=CASE WHEN ? THEN NOW() ELSE completed_at END WHERE id=?", [status,stage,message,json.dumps(plan) if plan is not None else None,json.dumps(metrics) if metrics is not None else None,error,complete,pid]); db.close()


def run_program(program_id: str) -> dict:
    db=connect(); ensure_schema(db)
    row=db.execute("SELECT mission_id,title,boundary FROM hive_boundary_programs WHERE id=?",[program_id]).fetchone(); db.close()
    if not row: raise ValueError('Boundary program not found')
    mission_id,title,boundary=row
    try:
        _update(program_id,'running','planning','Converting the boundary into observable claims and source-specific searches')
        plan_prompt=f"""Turn this evidence boundary into a targeted evidence-acquisition plan. Do not answer it.
BOUNDARY TITLE: {title}\nBOUNDARY: {boundary}
Return JSON with observable_claims (max 8, each claim,measurement,distinguishing_value),
queries (max 6, each query,source_family from primary_study|dataset|thesis|registry|patent|regulatory|ethnography|multilingual),
comparison_fields, and stop_conditions. Prefer queries likely to retrieve documented observations and null/contradictory results."""
        plan=_parse(complete(plan_prompt,stage='plan',system='Design rigorous, non-experimental evidence acquisition.',max_tokens=2500,temperature=.15,json_mode=True))
        plan['observable_claims']=(plan.get('observable_claims') or [])[:8]; plan['queries']=(plan.get('queries') or [])[:6]
        _update(program_id,'running','scouting','Searching authority registries with targeted probes',plan=plan)
        reports=[]; probes=[]
        for probe in plan['queries']:
            query=probe.get('query') if isinstance(probe,dict) else str(probe)
            if query: probes.append((query,probe.get('source_family') if isinstance(probe,dict) else None))
        with ThreadPoolExecutor(max_workers=min(3,len(probes) or 1)) as pool:
            futures={pool.submit(ingest_authority_sources,mission_id,query,8):(query,family) for query,family in probes}
            for future in as_completed(futures):
                query,family=futures[future]
                try: reports.append({'query':query,'source_family':family,'result':future.result()})
                except Exception as exc: reports.append({'query':query,'source_family':family,'result':{'errors':[{'error':str(exc)[:300]}]}})
        _update(program_id,'running','extracting','Normalizing documented observations into a comparison matrix',metrics={'searches':len(reports),'registry_reports':reports})
        db=connect()
        docs=db.execute("SELECT c.title,c.url,c.raw_text,c.quality_score,COALESCE(s.source_type,''),COALESCE(s.evidence_tier,9),COALESCE(s.provenance,'{}'::jsonb) FROM hive_content c LEFT JOIN hive_sources s ON s.mission_id=c.mission_id AND s.url=c.url WHERE c.mission_id=? ORDER BY c.harvested_at DESC LIMIT 250",[mission_id]).fetchall(); db.close()
        target=_tokens(title+' '+boundary+' '+' '.join(str(x) for x in plan.get('observable_claims',[])))
        docs=sorted(docs,key=lambda r:len(target&_tokens((r[0] or '')+' '+(r[2] or '')[:2500])),reverse=True)[:24]
        packet=[{'title':r[0],'url':r[1],'text':(r[2] or '')[:4500],'quality':r[3],'source_type':r[4],'tier':r[5],'provenance':r[6]} for r in docs]
        extract_prompt=f"""Extract a structured observation matrix for this evidence boundary using ONLY the supplied source packet.
Boundary: {boundary}\nPlan: {json.dumps(plan,default=str)}\nSources: {json.dumps(packet,default=str)[:80000]}
Every row must point to an exact source_url present above. Do not infer an observation absent from source text.
Classify evidence_class as direct, indirect, contradiction, or documented_absence.
absence_class when relevant: not_searched, no_source_found, full_text_unavailable, measurement_not_reported,
design_cannot_answer, conflicting_results, indirect_only, wrong_context, heterogeneous.
Return JSON with observations (max 40): claim, organism, substrate, process_conditions, measurement, outcome,
population, evidence_class, absence_class, source_url, source_title, source_family, quality 0-1, limitations array;
and synthesis: patterns, contradictions, remaining_absences, next_queries."""
        extracted=_parse(complete(extract_prompt,stage='process',system='You extract traceable documented observations; never invent evidence.',max_tokens=6000,temperature=.1,json_mode=True))
        valid_urls={r['url'] for r in packet}; packet_by_url={r['url']:r for r in packet}; observations=[]; promoted=0
        db=connect()
        for item in (extracted.get('observations') or [])[:40]:
            if not isinstance(item,dict) or item.get('source_url') not in valid_urls or not item.get('claim'): continue
            evidence_class=item.get('evidence_class') if item.get('evidence_class') in {'direct','indirect','contradiction','documented_absence'} else 'indirect'
            values=[program_id,str(item['claim'])[:3000],str(item.get('organism') or '')[:500],str(item.get('substrate') or '')[:500],str(item.get('process_conditions') or '')[:1500],str(item.get('measurement') or '')[:1000],str(item.get('outcome') or '')[:1500],str(item.get('population') or '')[:500],evidence_class,str(item.get('absence_class') or '')[:80],item['source_url'],str(item.get('source_title') or '')[:1000],str(item.get('source_family') or '')[:100],float(item.get('quality') or 0),json.dumps(item.get('limitations') or [])]
            db.execute("INSERT INTO hive_boundary_observations (program_id,claim,organism,substrate,process_conditions,measurement,outcome,population,evidence_class,absence_class,source_url,source_title,source_family,quality,limitations) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb) ON CONFLICT (program_id,claim,source_url) DO UPDATE SET outcome=EXCLUDED.outcome,quality=EXCLUDED.quality,limitations=EXCLUDED.limitations",values); observations.append(item)
            if evidence_class in {'direct','contradiction'} and float(item.get('quality') or 0) >= .55:
                source=packet_by_url[item['source_url']]; evidence='; '.join(str(x) for x in [item.get('measurement'),item.get('outcome'),item.get('process_conditions')] if x)
                existed=db.execute("SELECT 1 FROM hive_claims WHERE mission_id=? AND claim=? AND source_url=?",[mission_id,str(item['claim'])[:3000],item['source_url']]).fetchone()
                if not existed:
                    db.execute("INSERT INTO hive_claims (mission_id,claim,evidence,source_url,claim_type,confidence,quality_score,quality_details,source_operation,source_tier,verified,evidence_flags,is_garbage) VALUES (?,?,?,?,?,?,?,?::jsonb,'boundary_research',?,?,?::text,FALSE)",[mission_id,str(item['claim'])[:3000],evidence[:5000],item['source_url'],'contradiction' if evidence_class=='contradiction' else 'boundary_observation',float(item.get('quality') or 0),float(source.get('quality') or item.get('quality') or 0),json.dumps({'program_id':program_id,'boundary':boundary,'evidence_class':evidence_class,'limitations':item.get('limitations') or []}),int(source.get('tier') or 3),bool(float(item.get('quality') or 0)>=.7),'boundary_targeted;source_linked']); promoted+=1
        counts={}
        for r in db.execute("SELECT evidence_class,COUNT(*) FROM hive_boundary_observations WHERE program_id=? GROUP BY evidence_class",[program_id]).fetchall(): counts[r[0]]=r[1]
        metrics={'searches':len(reports),'documents_reviewed':len(packet),'observations':sum(counts.values()),'findings_promoted':promoted,'classes':counts,'registry_reports':reports,'synthesis':extracted.get('synthesis') or {}}
        db.close(); _update(program_id,'complete','complete',f"Mapped {metrics['observations']} documented observations from {len(packet)} relevant sources",plan=plan,metrics=metrics,complete=True)
        return {'id':program_id,'status':'complete','metrics':metrics}
    except Exception as exc:
        _update(program_id,'failed','failed','Evidence-boundary research stopped with a recoverable error',error=str(exc)[:1500])
        raise


def get_programs(mission_id: str) -> list[dict]:
    db=connect(); ensure_schema(db)
    programs=db.execute("SELECT id,section_key,title,boundary,status,stage,message,plan,metrics,error_message,created_at,updated_at,completed_at FROM hive_boundary_programs WHERE mission_id=? ORDER BY updated_at DESC",[mission_id]).fetchall()
    result=[]
    for r in programs:
        obs=db.execute("SELECT id,claim,organism,substrate,process_conditions,measurement,outcome,population,evidence_class,absence_class,source_url,source_title,source_family,quality,limitations FROM hive_boundary_observations WHERE program_id=? ORDER BY CASE evidence_class WHEN 'direct' THEN 0 WHEN 'contradiction' THEN 1 WHEN 'indirect' THEN 2 ELSE 3 END,quality DESC",[r[0]]).fetchall()
        result.append({'id':r[0],'section_key':r[1],'title':r[2],'boundary':r[3],'status':r[4],'stage':r[5],'message':r[6],'plan':r[7] or {},'metrics':r[8] or {},'error':r[9],'created_at':r[10],'updated_at':r[11],'completed_at':r[12],'observations':[{'id':x[0],'claim':x[1],'organism':x[2],'substrate':x[3],'conditions':x[4],'measurement':x[5],'outcome':x[6],'population':x[7],'evidence_class':x[8],'absence_class':x[9],'url':x[10],'source_title':x[11],'source_family':x[12],'quality':x[13],'limitations':x[14]} for x in obs]})
    db.close(); return result
