"""Claim-level citation integrity auditing with enforceable quarantine."""
from __future__ import annotations
import json,re
from behive.engine.db import connect
from behive.engine.llm import complete

SCHEMA="""
CREATE TABLE IF NOT EXISTS hive_citation_audits (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, claim_id INTEGER NOT NULL,
 status VARCHAR NOT NULL, score DOUBLE PRECISION DEFAULT 0, reason TEXT,
 supporting_quote TEXT, locator TEXT, source_url TEXT, checks JSONB DEFAULT '{}'::jsonb,
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,claim_id));
CREATE INDEX IF NOT EXISTS idx_citation_audit_mission ON hive_citation_audits(mission_id,status,updated_at DESC);
ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS integrity_status VARCHAR DEFAULT 'unaudited';
ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS integrity_score DOUBLE PRECISION DEFAULT 0;
ALTER TABLE hive_claims ADD COLUMN IF NOT EXISTS integrity_details JSONB DEFAULT '{}'::jsonb;
"""
STOP={'the','a','an','and','or','of','to','in','on','for','with','by','from','is','are','was','were','has','have','had','this','that','these','those','during','content','contents','increase','increases','increased','decrease','decreases','reduced','affects','effect'}
UNITS={'mg/kg','mg/g','g/kg','g/l','mg/l','µg','ug','mmol','percent','%','hours','days','°c'}

def ensure_schema(db=None):
    owned=db is None; db=db or connect()
    for s in (x.strip() for x in SCHEMA.split(';') if x.strip()): db.execute(s)
    if owned: db.close()

def _tokens(text): return [x for x in re.findall(r'[a-z0-9µ°%]+(?:/[a-z]+)?',str(text or '').lower()) if len(x)>1 and x not in STOP]
def _paragraphs(text): return [re.sub(r'\s+',' ',x).strip() for x in re.split(r'\n\s*\n|(?<=[.!?])\s+(?=[A-Z])',text or '') if len(x.strip())>40]
def _parse(raw):
    raw=(raw or '').strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:return json.loads(raw)
    except Exception:
        m=re.search(r'\{.*\}',raw,re.S); return json.loads(m.group(0)) if m else {}

def audit_claim(mission_id:str,claim_id:int)->dict:
    db=connect(); ensure_schema(db)
    row=db.execute("SELECT claim,evidence,source_url FROM hive_claims WHERE id=? AND mission_id=?",[claim_id,mission_id]).fetchone()
    if not row: db.close(); raise ValueError('Finding not found')
    claim,evidence,url=row; content=db.execute("SELECT raw_text,title FROM hive_content WHERE mission_id=? AND url=? AND COALESCE(word_count,0)>0 ORDER BY word_count DESC LIMIT 1",[mission_id,url]).fetchone()
    status='unverifiable'; score=0.; reason='Full source text is unavailable; the claim cannot pass citation integrity.'; quote=''; locator=''; checks={'source_text_available':bool(content)}
    if content:
        text,title=content; lower=text.lower(); claim_terms=list(dict.fromkeys(_tokens(claim))); measurement=(evidence or '').split(';')[0]; measurement_terms=list(dict.fromkeys(_tokens(measurement))); required_units=[u for u in UNITS if u in str(evidence or '').lower()]; numbers=re.findall(r'(?<![a-z])\d+(?:\.\d+)?',measurement)
        claim_coverage=sum(t in lower for t in claim_terms)/max(1,len(claim_terms)); measurement_coverage=sum(t in lower for t in measurement_terms)/max(1,len(measurement_terms)); units_ok=all(u in lower for u in required_units); numbers_ok=all(n in text for n in numbers)
        ranked=sorted(_paragraphs(text),key=lambda p:sum(t in p.lower() for t in set(claim_terms+measurement_terms)),reverse=True)[:8]
        checks.update({'claim_term_coverage':round(claim_coverage,3),'measurement_term_coverage':round(measurement_coverage,3),'required_units':required_units,'units_found':units_ok,'measurement_numbers_found':numbers_ok,'candidate_passages':len(ranked)})
        if measurement_terms and measurement_coverage<.65:
            status='unsupported'; reason='The claimed measurement is not present in the cited source.'; score=min(.35,measurement_coverage*.4)
        elif not units_ok or not numbers_ok:
            status='unsupported'; reason='The claim introduces units or measured values that cannot be located in the cited source.'; score=.25
        elif claim_coverage<.45 or not ranked:
            status='unsupported'; reason='No source passage contains enough of the claim-specific concepts to support the assertion.'; score=claim_coverage*.5
        else:
            prompt=f"""Decide whether the SOURCE PASSAGES directly entail the CLAIM and MEASUREMENT. Do not use outside knowledge.
CLAIM: {claim}\nMEASUREMENT/OUTCOME: {evidence}\nPASSAGES: {json.dumps(ranked,ensure_ascii=False)[:30000]}
Return JSON: verdict supported|unsupported|ambiguous, score 0-1, exact_quote copied verbatim from one passage, reason. A topic match is not support. The measured variable and asserted direction/outcome must both be explicit."""
            result=_parse(complete(prompt,stage='verify',system='You are a strict citation entailment auditor. Prefer unsupported over invented support.',max_tokens=900,temperature=0,json_mode=True)); verdict=result.get('verdict','ambiguous'); quote=str(result.get('exact_quote') or '').strip(); quote_valid=bool(quote and quote.lower() in lower)
            status='supported' if verdict=='supported' and quote_valid else 'unsupported' if verdict=='unsupported' or (verdict=='supported' and not quote_valid) else 'ambiguous'; score=float(result.get('score') or 0); reason=str(result.get('reason') or ('The model did not provide a source-verbatim supporting passage.' if not quote_valid else ''))[:2000]
            if not quote_valid: quote=''
            if quote: locator=f"Source text near character {lower.find(quote.lower())+1}"
            checks['entailment_verdict']=verdict; checks['quote_verified_verbatim']=quote_valid
    direct_text=f"{quote} {evidence or ''}".lower()
    direct_signals=bool(re.search(r'\b(measur(?:e|ed|ement)|observ(?:e|ed|ation)|assay|cohort|participants?|patients?|mice|mouse|rats?|cells?|tissue|western blot|immuno(?:histochemistry|fluorescence)|microscopy|expression|concentration|dose|significant|fold change)\b|\d+(?:\.\d+)?\s*%',direct_text))
    evidence_class='direct' if status=='supported' and direct_signals else 'indirect'
    checks['direct_evidence_signals']=direct_signals
    quarantined=status!='supported'; details={'reason':reason,'supporting_quote':quote,'locator':locator,'checks':checks,'source_url':url,'evidence_class':evidence_class}
    db.execute("INSERT INTO hive_citation_audits (mission_id,claim_id,status,score,reason,supporting_quote,locator,source_url,checks) VALUES (?,?,?,?,?,?,?,?,?::jsonb) ON CONFLICT (mission_id,claim_id) DO UPDATE SET status=EXCLUDED.status,score=EXCLUDED.score,reason=EXCLUDED.reason,supporting_quote=EXCLUDED.supporting_quote,locator=EXCLUDED.locator,source_url=EXCLUDED.source_url,checks=EXCLUDED.checks,updated_at=NOW()",[mission_id,claim_id,status,score,reason,quote,locator,url,json.dumps(checks)]); db.execute("UPDATE hive_claims SET integrity_status=?,integrity_score=?,integrity_details=?::jsonb,quality_details=COALESCE(quality_details,'{}'::jsonb)||?::jsonb,is_garbage=? WHERE id=? AND mission_id=?",[status,score,json.dumps(details),json.dumps({'evidence_class':evidence_class,'citation_audited':True}),quarantined,claim_id,mission_id]); db.close()
    from behive.engine.decision_ledger import record
    record(mission_id,'citation_integrity',f'{"Supported" if status=="supported" else "Quarantined"}: {claim[:240]}',reason,impact='supports' if status=='supported' else 'weakens',before={'integrity':'unaudited'},after={'integrity':status,'quarantined':quarantined,'score':score},confidence_after=score,source_url=url,reversal_conditions='Re-audit when corrected full text, a valid passage locator, or materially different evidence is supplied.')
    return {'claim_id':claim_id,'status':status,'score':score,'quarantined':quarantined,**details}

def audit_mission(mission_id:str,limit:int=100,only_unaudited:bool=False)->dict:
    db=connect(); ensure_schema(db); where="AND COALESCE(integrity_status,'unaudited')='unaudited'" if only_unaudited else ""; ids=[r[0] for r in db.execute(f"SELECT id FROM hive_claims WHERE mission_id=? {where} ORDER BY CASE integrity_status WHEN 'unaudited' THEN 0 ELSE 1 END,quality_score DESC LIMIT ?",[mission_id,limit]).fetchall()]; db.close(); results=[]
    for cid in ids:
        try: results.append(audit_claim(mission_id,cid))
        except Exception as exc: results.append({'claim_id':cid,'status':'error','reason':str(exc)[:500]})
    return {'mission_id':mission_id,'audited':len(results),'supported':sum(x.get('status')=='supported' for x in results),'quarantined':sum(x.get('quarantined',False) for x in results),'errors':sum(x.get('status')=='error' for x in results),'results':results}
