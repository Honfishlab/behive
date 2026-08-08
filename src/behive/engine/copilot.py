"""Mission-aware, evidence-grounded Research Copilot."""
from __future__ import annotations

import json, re, uuid
from behive.engine.db import connect
from behive.engine.llm import complete

SCHEMA="""
CREATE TABLE IF NOT EXISTS hive_copilot_threads (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, title TEXT,
 scope VARCHAR DEFAULT 'mission', created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW());
CREATE TABLE IF NOT EXISTS hive_copilot_messages (
 id SERIAL PRIMARY KEY, thread_id VARCHAR NOT NULL, role VARCHAR NOT NULL, content TEXT NOT NULL,
 evidence JSONB DEFAULT '[]'::jsonb, suggestions JSONB DEFAULT '[]'::jsonb,
 proposed_actions JSONB DEFAULT '[]'::jsonb, created_at TIMESTAMP DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_copilot_messages_thread ON hive_copilot_messages(thread_id,id);
"""

def ensure_schema(db=None):
    owned=db is None; db=db or connect()
    for s in (x.strip() for x in SCHEMA.split(';') if x.strip()): db.execute(s)
    if owned: db.close()

def _tokens(text): return {x for x in re.findall(r"[a-z0-9]{3,}",(text or '').lower()) if x not in {'what','when','where','which','with','from','that','this','about'}}
def _rank(rows, query, text_index=0, limit=12):
    q=_tokens(query)
    return sorted(rows,key=lambda r:len(q&_tokens(str(r[text_index]))),reverse=True)[:limit]

def evidence_packet(mission_id:str, query:str)->dict:
    # These tables are optional for older missions; initialize them before retrieval.
    from behive.engine.frontier import ensure_schema as ensure_frontier_schema
    from behive.engine.controller import ensure_schema as ensure_controller_schema
    ensure_frontier_schema(); ensure_controller_schema()
    db=connect(); ensure_schema(db)
    mission=db.execute("SELECT topic,status FROM hive_missions WHERE id=?",[mission_id]).fetchone()
    if not mission: db.close(); raise ValueError('Mission not found')
    project=db.execute("SELECT id FROM hive_projects WHERE LOWER(TRIM(root_question))=LOWER(TRIM(?)) LIMIT 1",[mission[0]]).fetchone()
    questions=[]
    if project:
        questions=db.execute("SELECT id,parent_id,question,depth,status,priority FROM hive_questions WHERE project_id=? ORDER BY depth,priority DESC",[project[0]]).fetchall()
    claims=_rank(db.execute("SELECT claim,evidence,source_url,confidence,quality_score,claim_type FROM hive_claims WHERE mission_id=? AND (is_garbage=FALSE OR is_garbage IS NULL) ORDER BY quality_score DESC LIMIT 100",[mission_id]).fetchall(),query)
    nodes=_rank(db.execute("SELECT label,description,epistemic_state,node_type,id FROM hive_discovery_nodes WHERE mission_id=? ORDER BY novelty DESC LIMIT 150",[mission_id]).fetchall(),query)
    frontiers=_rank(db.execute("SELECT title,rationale,absence_type,priority,id FROM hive_frontiers WHERE mission_id=? AND status='open' ORDER BY priority DESC LIMIT 80",[mission_id]).fetchall(),query)
    hypotheses=_rank(db.execute("SELECT name,statement,reasoning,scores,id FROM hive_hypothesis_paths WHERE mission_id=? ORDER BY updated_at DESC LIMIT 60",[mission_id]).fetchall(),query)
    changes=db.execute("SELECT headline,detail,change_type,created_at FROM hive_research_changes WHERE mission_id=? ORDER BY created_at DESC LIMIT 12",[mission_id]).fetchall()
    db.close()
    return {'mission':{'id':mission_id,'topic':mission[0],'status':mission[1],'project_id':project[0] if project else None},
      'questions':[{'id':r[0],'parent_id':r[1],'question':r[2],'depth':r[3],'status':r[4],'priority':r[5]} for r in questions],
      'findings':[{'claim':r[0],'evidence':r[1],'url':r[2],'confidence':r[3],'quality':r[4],'type':r[5]} for r in claims],
      'concepts':[{'label':r[0],'description':r[1],'status':r[2],'type':r[3],'id':r[4]} for r in nodes],
      'frontiers':[{'title':r[0],'rationale':r[1],'absence_type':r[2],'priority':r[3],'id':r[4]} for r in frontiers],
      'hypotheses':[{'name':r[0],'statement':r[1],'reasoning':r[2],'scores':r[3],'id':r[4]} for r in hypotheses],
      'changes':[{'headline':r[0],'detail':r[1],'type':r[2],'created_at':str(r[3])} for r in changes]}

def _parse(raw):
    raw=(raw or '').strip()
    if raw.startswith('```'): raw=raw.split('\n',1)[1].rsplit('```',1)[0]
    try:return json.loads(raw)
    except Exception:
        m=re.search(r'\{.*\}',raw,re.S); return json.loads(m.group(0)) if m else {'answer':raw}

def chat(mission_id:str,message:str,thread_id:str|None=None,scope:str='mission',external:bool=False)->dict:
    packet=evidence_packet(mission_id,message)
    external_result=None
    if external:
        from behive.engine.authority import ingest_authority_sources
        external_result=ingest_authority_sources(mission_id,message,limit=8)
        packet=evidence_packet(mission_id,message)
    db=connect(); ensure_schema(db)
    if not thread_id:
        thread_id='chat_'+uuid.uuid4().hex[:16]
        db.execute("INSERT INTO hive_copilot_threads (id,mission_id,title,scope) VALUES (?,?,?,?)",[thread_id,mission_id,message[:120],scope])
    else:
        owner=db.execute("SELECT mission_id FROM hive_copilot_threads WHERE id=?",[thread_id]).fetchone()
        if not owner or owner[0] != mission_id:
            db.close(); raise ValueError('Copilot thread does not belong to this mission')
    history=db.execute("SELECT role,content FROM hive_copilot_messages WHERE thread_id=? ORDER BY id DESC LIMIT 10",[thread_id]).fetchall()[::-1]
    db.execute("INSERT INTO hive_copilot_messages (thread_id,role,content) VALUES (?,'user',?)",[thread_id,message]); db.commit(); db.close()
    prompt=f"""You are BeHive Research Copilot. Converse intelligently about an exploratory investigation, not as a generic answer bot.
Use only the supplied mission packet for claims about what BeHive found. Clearly label inference, hypothesis, unknown, and outside information.
Never treat model prose as evidence. Cite findings with their exact source URL. Suggest high-value subjects from graph gaps and contradictions.
You may PROPOSE actions but never claim they ran.

CONVERSATION: {json.dumps(history,default=str)[:8000]}
USER: {message}
MISSION PACKET: {json.dumps(packet,default=str)[:28000]}
EXTERNAL INGESTION RESULT: {json.dumps(external_result,default=str)}

Return JSON with: answer (clear markdown), evidence_used (array of label,url,epistemic_status), unknowns (array),
suggestions (array of title,rationale,question,expected_value 0-1), proposed_actions (array of type add_question|start_controller|external_search, label, question, parent_id optional)."""
    result=_parse(complete(prompt,stage='synth',system='You are an evidence-grounded research copilot with explicit epistemic boundaries.',max_tokens=3500,temperature=.25,json_mode=True))
    result.setdefault('answer','I could not form a grounded response.'); result.setdefault('evidence_used',[]); result.setdefault('unknowns',[]); result.setdefault('suggestions',[]); result.setdefault('proposed_actions',[])
    db=connect(); db.execute("INSERT INTO hive_copilot_messages (thread_id,role,content,evidence,suggestions,proposed_actions) VALUES (?,'assistant',?,?::jsonb,?::jsonb,?::jsonb)",[thread_id,result['answer'],json.dumps(result['evidence_used']),json.dumps(result['suggestions']),json.dumps(result['proposed_actions'])]); db.execute("UPDATE hive_copilot_threads SET updated_at=NOW() WHERE id=?",[thread_id]); db.commit(); db.close()
    return {'thread_id':thread_id,'mission_id':mission_id,**result,'external':external_result}

def history(mission_id):
    db=connect(); ensure_schema(db); rows=db.execute("SELECT id,title,scope,created_at,updated_at FROM hive_copilot_threads WHERE mission_id=? ORDER BY updated_at DESC LIMIT 30",[mission_id]).fetchall(); db.close()
    return [{'id':r[0],'title':r[1],'scope':r[2],'created_at':r[3],'updated_at':r[4]} for r in rows]

def messages(mission_id:str, thread_id:str)->list[dict]:
    db=connect(); ensure_schema(db)
    owner=db.execute("SELECT 1 FROM hive_copilot_threads WHERE id=? AND mission_id=?",[thread_id,mission_id]).fetchone()
    if not owner: db.close(); raise ValueError('Copilot thread not found')
    rows=db.execute("SELECT role,content,evidence,suggestions,proposed_actions,created_at FROM hive_copilot_messages WHERE thread_id=? ORDER BY id",[thread_id]).fetchall(); db.close()
    return [{'role':r[0],'content':r[1],'evidence':r[2] or [],'suggestions':r[3] or [],'proposed_actions':r[4] or [],'created_at':r[5]} for r in rows]
