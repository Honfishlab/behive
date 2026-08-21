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
 pass_count INTEGER DEFAULT 0, pass_history JSONB DEFAULT '[]'::jsonb,
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
    db.execute("ALTER TABLE hive_boundary_programs ADD COLUMN IF NOT EXISTS pass_count INTEGER DEFAULT 0")
    db.execute("ALTER TABLE hive_boundary_programs ADD COLUMN IF NOT EXISTS pass_history JSONB DEFAULT '[]'::jsonb")
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


def _update(pid, status, stage, message, *, plan=None, metrics=None, error=None, complete=False,
            pass_count=None, pass_history=None):
    db=connect(); ensure_schema(db)
    db.execute("UPDATE hive_boundary_programs SET status=?,stage=?,message=?,plan=COALESCE(?::jsonb,plan),"
               "metrics=COALESCE(?::jsonb,metrics),error_message=?,pass_count=COALESCE(?,pass_count),"
               "pass_history=COALESCE(?::jsonb,pass_history),updated_at=NOW(),"
               "completed_at=CASE WHEN ? THEN NOW() ELSE completed_at END WHERE id=?",
               [status,stage,message,json.dumps(plan) if plan is not None else None,
                json.dumps(metrics) if metrics is not None else None,error,pass_count,
                json.dumps(pass_history) if pass_history is not None else None,complete,pid]); db.close()


def run_program(program_id: str) -> dict:
    db=connect(); ensure_schema(db)
    row=db.execute("SELECT mission_id,title,boundary,COALESCE(plan,'{}'::jsonb),COALESCE(metrics,'{}'::jsonb),"
                   "COALESCE(pass_count,0),COALESCE(pass_history,'[]'::jsonb) FROM hive_boundary_programs WHERE id=?",[program_id]).fetchone(); db.close()
    if not row: raise ValueError('Boundary program not found')
    mission_id,title,boundary,prior_plan,prior_metrics,prior_pass_count,pass_history=row
    pass_number=int(prior_pass_count or 0)+1; pass_history=list(pass_history or [])
    try:
        _update(program_id,'running','planning',f'Designing targeted evidence pass {pass_number}',pass_count=pass_number)
        previous_queries=[x.get('query') for x in (prior_plan.get('queries') or []) if isinstance(x,dict) and x.get('query')]
        prior_synthesis=(prior_metrics or {}).get('synthesis') or {}
        plan_prompt=f"""Turn this evidence boundary into a targeted evidence-acquisition plan. Do not answer it.
BOUNDARY TITLE: {title}\nBOUNDARY: {boundary}
PASS: {pass_number}
PREVIOUS QUERIES (do not repeat): {json.dumps(previous_queries)}
REMAINING ABSENCES FROM PRIOR PASS: {json.dumps(prior_synthesis.get('remaining_absences') or [])}
SUGGESTED NEXT QUERIES FROM PRIOR PASS: {json.dumps(prior_synthesis.get('next_queries') or [])}
Return JSON with observable_claims (max 8, each claim,measurement,distinguishing_value),
queries (max 6, each query,source_family from primary_study|dataset|thesis|registry|patent|regulatory|ethnography|multilingual),
comparison_fields, and stop_conditions. Every query must materially differ from previous queries. Route remaining
absences to the most appropriate source family and prefer documented observations, null results, and contradictions.
Prefer primary_study, dataset, and registry for biomedical mechanism questions. Use thesis, patent, regulatory,
multilingual, or ethnography only when that family can directly answer this boundary; never add a family merely for
coverage. Include important scientific synonyms, intervention names, model systems, and measurable outcomes in each
query. A query that only changes generic words such as study, research, or review is a repeat."""
        plan=_parse(complete(plan_prompt,stage='plan',system='Design rigorous, non-experimental evidence acquisition.',max_tokens=2500,temperature=.15,json_mode=True))
        plan['observable_claims']=(plan.get('observable_claims') or [])[:8]; plan['queries']=(plan.get('queries') or [])[:6]
        _update(program_id,'running','scouting',f'Searching authoritative source families · pass {pass_number}',plan=plan)
        db=connect()
        before_urls={r[0] for r in db.execute("SELECT url FROM hive_sources WHERE mission_id=?",[mission_id]).fetchall()}
        reviewed_urls={url for entry in pass_history for url in (entry.get('reviewed_urls') or [])}
        db.close()
        reports=[]; probes=[]
        for probe in plan['queries']:
            query=probe.get('query') if isinstance(probe,dict) else str(probe)
            if query: probes.append((query,probe.get('source_family') if isinstance(probe,dict) else None))
        with ThreadPoolExecutor(max_workers=min(3,len(probes) or 1)) as pool:
            futures={pool.submit(ingest_authority_sources,mission_id,query,8,family):(query,family) for query,family in probes}
            for future in as_completed(futures):
                query,family=futures[future]
                try: reports.append({'query':query,'source_family':family,'result':future.result()})
                except Exception as exc: reports.append({'query':query,'source_family':family,'result':{'errors':[{'error':str(exc)[:300]}]}})
        db=connect()
        after_urls={r[0] for r in db.execute("SELECT url FROM hive_sources WHERE mission_id=?",[mission_id]).fetchall()}
        new_urls=after_urls-before_urls
        _update(program_id,'running','extracting',f'Extracting only newly acquired evidence · pass {pass_number}',
                metrics={'pass':pass_number,'searches':len(reports),'new_sources':len(new_urls),'registry_reports':reports})
        docs=db.execute("SELECT c.title,c.url,c.raw_text,c.quality_score,COALESCE(s.source_type,''),"
                        "COALESCE(s.evidence_tier,9),COALESCE(s.provenance,'{}'::jsonb) FROM hive_content c "
                        "LEFT JOIN hive_sources s ON s.mission_id=c.mission_id AND s.url=c.url "
                        "WHERE c.mission_id=? AND c.url=ANY(?) ORDER BY c.harvested_at DESC LIMIT 250",
                        [mission_id,list(new_urls)]).fetchall() if new_urls else []
        db.close()
        target=_tokens(title+' '+boundary+' '+' '.join(str(x) for x in plan.get('observable_claims',[])))
        docs=[r for r in docs if r[1] not in reviewed_urls]
        docs=sorted(docs,key=lambda r:len(target&_tokens((r[0] or '')+' '+(r[2] or '')[:2500])),reverse=True)[:24]
        packet=[{'title':r[0],'url':r[1],'text':(r[2] or '')[:4500],'quality':r[3],'source_type':r[4],'tier':r[5],'provenance':r[6]} for r in docs]
        extract_prompt=f"""Extract a structured observation matrix for this evidence boundary using ONLY the NEW source packet from pass {pass_number}.
Boundary: {boundary}\nPlan: {json.dumps(plan,default=str)}\nSources: {json.dumps(packet,default=str)[:80000]}
Every row must point to an exact source_url present above. Do not infer an observation absent from source text.
Classify evidence_class as direct, indirect, contradiction, or documented_absence.
absence_class when relevant: not_searched, no_source_found, full_text_unavailable, measurement_not_reported,
design_cannot_answer, conflicting_results, indirect_only, wrong_context, heterogeneous.
Return JSON with observations (max 40): claim, organism, substrate, process_conditions, measurement, outcome,
population, evidence_class, absence_class, source_url, source_title, source_family, quality 0-1, limitations array;
and synthesis: patterns, contradictions, remaining_absences, next_queries."""
        if packet:
            extracted=_parse(complete(extract_prompt,stage='process',system='You extract traceable documented observations; never invent evidence.',max_tokens=6000,temperature=.1,json_mode=True))
        else:
            extracted={'observations':[],'synthesis':{
                'patterns':[], 'contradictions':[],
                'remaining_absences':prior_synthesis.get('remaining_absences') or [boundary],
                'next_queries':prior_synthesis.get('next_queries') or previous_queries,
                'stop_reason':'No new evidence-ready documents were retrieved in this pass.'}}
        valid_urls={r['url'] for r in packet}; packet_by_url={r['url']:r for r in packet}; observations=[]; promoted=0
        db=connect()
        for item in (extracted.get('observations') or [])[:40]:
            if not isinstance(item,dict) or item.get('source_url') not in valid_urls or not item.get('claim'): continue
            evidence_class=item.get('evidence_class') if item.get('evidence_class') in {'direct','indirect','contradiction','documented_absence'} else 'indirect'
            observation_existed=db.execute("SELECT 1 FROM hive_boundary_observations WHERE program_id=? AND claim=? AND source_url=?",
                                           [program_id,str(item['claim'])[:3000],item['source_url']]).fetchone()
            values=[program_id,str(item['claim'])[:3000],str(item.get('organism') or '')[:500],str(item.get('substrate') or '')[:500],str(item.get('process_conditions') or '')[:1500],str(item.get('measurement') or '')[:1000],str(item.get('outcome') or '')[:1500],str(item.get('population') or '')[:500],evidence_class,str(item.get('absence_class') or '')[:80],item['source_url'],str(item.get('source_title') or '')[:1000],str(item.get('source_family') or '')[:100],float(item.get('quality') or 0),json.dumps(item.get('limitations') or [])]
            db.execute("INSERT INTO hive_boundary_observations (program_id,claim,organism,substrate,process_conditions,measurement,outcome,population,evidence_class,absence_class,source_url,source_title,source_family,quality,limitations) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb) ON CONFLICT (program_id,claim,source_url) DO UPDATE SET outcome=EXCLUDED.outcome,quality=EXCLUDED.quality,limitations=EXCLUDED.limitations",values)
            if not observation_existed: observations.append(item)
            if evidence_class in {'direct','contradiction'} and float(item.get('quality') or 0) >= .55:
                source=packet_by_url[item['source_url']]; evidence='; '.join(str(x) for x in [item.get('measurement'),item.get('outcome'),item.get('process_conditions')] if x)
                existed=db.execute("SELECT 1 FROM hive_claims WHERE mission_id=? AND claim=? AND source_url=?",[mission_id,str(item['claim'])[:3000],item['source_url']]).fetchone()
                if not existed:
                    db.execute("INSERT INTO hive_claims (mission_id,claim,evidence,source_url,claim_type,confidence,quality_score,quality_details,source_operation,source_tier,verified,evidence_flags,is_garbage) VALUES (?,?,?,?,?,?,?,?::jsonb,'boundary_research',?,?,?::text,FALSE)",[mission_id,str(item['claim'])[:3000],evidence[:5000],item['source_url'],'contradiction' if evidence_class=='contradiction' else 'boundary_observation',float(item.get('quality') or 0),float(source.get('quality') or item.get('quality') or 0),json.dumps({'program_id':program_id,'boundary':boundary,'evidence_class':evidence_class,'limitations':item.get('limitations') or []}),int(source.get('tier') or 3),bool(float(item.get('quality') or 0)>=.7),'boundary_targeted;source_linked']); promoted+=1
                    from behive.engine.decision_ledger import record
                    record(mission_id,'observation_promoted',('Contradictory' if evidence_class=='contradiction' else 'Supporting')+' observation added: '+str(item['claim'])[:240],evidence,subject_key=program_id,impact='weakens' if evidence_class=='contradiction' else 'supports',before={'canonical_finding':False},after={'canonical_finding':True,'evidence_class':evidence_class},confidence_before=0,confidence_after=float(item.get('quality') or 0),source_url=item['source_url'],reversal_conditions='Reassess if the source is retracted, the measurement is shown to be incomparable, or stronger independent evidence contradicts this observation.')
        counts={}
        for r in db.execute("SELECT evidence_class,COUNT(*) FROM hive_boundary_observations WHERE program_id=? GROUP BY evidence_class",[program_id]).fetchall(): counts[r[0]]=r[1]
        synthesis=extracted.get('synthesis') or {}
        prior_query_keys={str(q).strip().casefold() for q in previous_queries}
        if isinstance(synthesis.get('next_queries'),list):
            synthesis['next_queries']=[q for q in synthesis['next_queries']
                                       if isinstance(q,str) and q.strip().casefold() not in prior_query_keys]
        metrics={'pass':pass_number,'searches':len(reports),'new_sources':len(new_urls),
                 'new_documents_reviewed':len(packet),'documents_reviewed':len(packet),
                 'new_observations':len(observations),'observations':sum(counts.values()),
                 'cumulative_observations':sum(counts.values()),
                 'findings_promoted':promoted,'classes':counts,'registry_reports':reports,'synthesis':synthesis,
                 'source_families':sorted({family for _,family in probes if family})}
        pass_entry={'pass':pass_number,'queries':[q for q,_ in probes],'source_families':metrics['source_families'],
                    'new_sources':len(new_urls),'reviewed_urls':[x['url'] for x in packet],
                    'metrics':{'new_documents':len(packet),'new_observations':len(observations),'findings_promoted':promoted},
                    'remaining_absences':synthesis.get('remaining_absences') or [],'next_queries':synthesis.get('next_queries') or []}
        pass_history.append(pass_entry)
        db.close(); _update(program_id,'complete','complete',
            f"Pass {pass_number}: {len(observations)} new observations from {len(packet)} new documents",
            plan=plan,metrics=metrics,complete=True,pass_count=pass_number,pass_history=pass_history[-20:])
        return {'id':program_id,'status':'complete','metrics':metrics}
    except Exception as exc:
        _update(program_id,'failed','failed','Evidence-boundary research stopped with a recoverable error',error=str(exc)[:1500])
        raise


def get_programs(mission_id: str) -> list[dict]:
    db=connect(); ensure_schema(db)
    programs=db.execute("SELECT id,section_key,title,boundary,status,stage,message,plan,metrics,error_message,"
                        "created_at,updated_at,completed_at,COALESCE(pass_count,0),COALESCE(pass_history,'[]'::jsonb) "
                        "FROM hive_boundary_programs WHERE mission_id=? ORDER BY updated_at DESC",[mission_id]).fetchall()
    result=[]
    for r in programs:
        obs=db.execute("SELECT id,claim,organism,substrate,process_conditions,measurement,outcome,population,evidence_class,absence_class,source_url,source_title,source_family,quality,limitations FROM hive_boundary_observations WHERE program_id=? ORDER BY CASE evidence_class WHEN 'direct' THEN 0 WHEN 'contradiction' THEN 1 WHEN 'indirect' THEN 2 ELSE 3 END,quality DESC",[r[0]]).fetchall()
        question_id='question_'+sha1(f'{mission_id}|boundary|{r[1]}'.encode()).hexdigest()[:16]
        operational_status={'queued':'queued','running':'researching','failed':'blocked'}.get(r[4])
        if r[4]=='complete': operational_status='evidence_found' if obs else 'evidence_boundary'
        if operational_status:
            db.execute("UPDATE hive_questions SET status=? WHERE id=? AND status IS DISTINCT FROM ?",[operational_status,question_id,operational_status])
        result.append({'id':r[0],'section_key':r[1],'title':r[2],'boundary':r[3],'status':r[4],'stage':r[5],'message':r[6],'plan':r[7] or {},'metrics':r[8] or {},'error':r[9],'created_at':r[10],'updated_at':r[11],'completed_at':r[12],
                       'pass_count':r[13],'pass_history':r[14] or [],'observations':[{'id':x[0],'claim':x[1],'organism':x[2],'substrate':x[3],'conditions':x[4],'measurement':x[5],'outcome':x[6],'population':x[7],'evidence_class':x[8],'absence_class':x[9],'url':x[10],'source_title':x[11],'source_family':x[12],'quality':x[13],'limitations':x[14]} for x in obs]})
    db.close(); return result
