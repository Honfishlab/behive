"""Operational lifecycle, history, and controls for discovery frontiers."""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from hashlib import sha1
from behive.engine.db import connect
from behive.engine.controller import ensure_schema as ensure_controller_schema, _branch_id, _choose_action, _record_change
from behive.engine.frontier import ensure_schema as ensure_frontier_schema, _normalize_search_strategy

def get_lifecycle(mission_id: str, frontier_id: str) -> dict:
    ensure_frontier_schema(); ensure_controller_schema(); db=connect()
    f=db.execute("SELECT title,frontier_type,rationale,source_node_id,target_node_id,absence_type,status,evidence_search,scores,priority,created_at,updated_at FROM hive_frontiers WHERE id=? AND mission_id=?",[frontier_id,mission_id]).fetchone()
    if not f: db.close(); raise ValueError('Frontier not found')
    branch=db.execute("SELECT id,status,reason,uncertainty,evidence_coverage,priority,retry_count,duplicate_streak,no_progress_cycles,last_action,last_checked_at,next_review_at,queries_attempted,sources_seen,created_at,updated_at FROM hive_research_branches WHERE mission_id=? AND subject_type='frontier' AND subject_id=?",[mission_id,frontier_id]).fetchone()
    actions=[]; queries=[]; changes=[]
    if branch:
        actions=db.execute("SELECT id,action_type,rationale,status,attempt,max_attempts,error_message,queued_at,started_at,completed_at,result FROM hive_research_actions WHERE mission_id=? AND branch_id=? ORDER BY queued_at DESC LIMIT 50",[mission_id,branch[0]]).fetchall()
        queries=db.execute("SELECT normalized_query,result_count,novel_source_count,status,attempted_at FROM hive_research_queries WHERE mission_id=? AND branch_id=? ORDER BY attempted_at DESC LIMIT 50",[mission_id,branch[0]]).fetchall()
        changes=db.execute("SELECT change_type,headline,detail,before_state,after_state,created_at FROM hive_research_changes WHERE mission_id=? AND branch_id=? ORDER BY created_at DESC LIMIT 50",[mission_id,branch[0]]).fetchall()
    hypotheses=db.execute("SELECT id,name,status,path_nodes,missing_links FROM hive_hypothesis_paths WHERE mission_id=?",[mission_id]).fetchall()
    linked=[{'id':h[0],'name':h[1],'status':h[2]} for h in hypotheses if frontier_id in (h[3] or []) or f[0].lower() in json.dumps(h[4] or []).lower()]
    children=db.execute("SELECT id,title,status,priority FROM hive_frontiers WHERE mission_id=? AND id<>? AND created_at>=? AND (source_node_id=? OR target_node_id=?) ORDER BY created_at DESC LIMIT 20",[mission_id,frontier_id,f[10],f[3],f[4]]).fetchall()
    db.close()
    return {'id':frontier_id,'mission_id':mission_id,'title':f[0],'type':f[1],'rationale':f[2],'source':f[3],'target':f[4],'absence_type':f[5],'status':f[6],'search_strategy':_normalize_search_strategy(f[7]),'scores':f[8] or {},'priority':f[9],'created_at':f[10],'updated_at':f[11],
      'branch':None if not branch else {'id':branch[0],'status':branch[1],'reason':branch[2],'uncertainty':branch[3],'evidence_coverage':branch[4],'priority':branch[5],'retry_count':branch[6],'duplicate_streak':branch[7],'no_progress_cycles':branch[8],'last_action':branch[9],'last_checked_at':branch[10],'next_review_at':branch[11],'queries_attempted':branch[12] or [],'sources_seen':branch[13] or [],'created_at':branch[14],'updated_at':branch[15]},
      'actions':[{'id':a[0],'type':a[1],'rationale':a[2],'status':a[3],'attempt':a[4],'max_attempts':a[5],'error':a[6],'queued_at':a[7],'started_at':a[8],'completed_at':a[9],'result':a[10] or {}} for a in actions],
      'queries':[{'query':q[0],'result_count':q[1],'novel_sources':q[2],'status':q[3],'attempted_at':q[4]} for q in queries],
      'changes':[{'type':c[0],'headline':c[1],'detail':c[2],'before':c[3] or {},'after':c[4] or {},'created_at':c[5]} for c in changes],
      'hypotheses':linked,'children':[{'id':c[0],'title':c[1],'status':c[2],'priority':c[3]} for c in children]}

def apply_action(mission_id: str, frontier_id: str, action: str, guidance: str='') -> dict:
    ensure_frontier_schema(); ensure_controller_schema(); db=connect()
    f=db.execute("SELECT title,rationale,scores,priority,status FROM hive_frontiers WHERE id=? AND mission_id=?",[frontier_id,mission_id]).fetchone()
    if not f: db.close(); raise ValueError('Frontier not found')
    bid=_branch_id(mission_id,'frontier',frontier_id)
    db.execute("INSERT INTO hive_research_branches (id,mission_id,subject_type,subject_id,title,status,reason,uncertainty,evidence_coverage,priority) VALUES (?,?,?,?,?,'watching',?,?,?,?) ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title,updated_at=NOW()",[bid,mission_id,'frontier',frontier_id,f[0],f[1] or '',1-float((f[2] or {}).get('evidence_strength',0) or 0),float((f[2] or {}).get('evidence_strength',0) or 0),float(f[3] or .5)])
    before={'frontier_status':f[4]}
    if action=='research_now':
        branch=db.execute("SELECT id,subject_id,title,status,reason,uncertainty,evidence_coverage,priority,retry_count,duplicate_streak,no_progress_cycles FROM hive_research_branches WHERE id=?",[bid]).fetchone(); kind,rationale,metrics=_choose_action(branch); fingerprint=sha1(f'{bid}|manual|{datetime.now().isoformat()}'.encode()).hexdigest()[:24]
        db.execute("INSERT INTO hive_research_actions (mission_id,branch_id,action_type,rationale,status,score,information_gain,source_quality_gain,novelty,feasibility,cost,fingerprint,payload) VALUES (?,?,?,?, 'queued',1,?,?,?,?,?,?,?::jsonb)",[mission_id,bid,kind,rationale,metrics['information_gain'],metrics['source_quality_gain'],metrics['novelty'],metrics['feasibility'],metrics['cost'],fingerprint,json.dumps({'frontier_id':frontier_id,'manual':True})]); db.execute("UPDATE hive_frontiers SET status='investigating',updated_at=NOW() WHERE id=?",[frontier_id]); db.execute("UPDATE hive_research_branches SET status='active',priority=1,next_review_at=NOW(),updated_at=NOW() WHERE id=?",[bid]); after={'frontier_status':'investigating','queued_action':kind}
    elif action=='pause': db.execute("UPDATE hive_frontiers SET status='paused',updated_at=NOW() WHERE id=?",[frontier_id]); db.execute("UPDATE hive_research_branches SET status='scheduled_revisit',next_review_at=?,updated_at=NOW() WHERE id=?",[datetime.now()+timedelta(days=30),bid]); after={'frontier_status':'paused','next_review':'30 days'}
    elif action=='close': db.execute("UPDATE hive_frontiers SET status='resolved',updated_at=NOW() WHERE id=?",[frontier_id]); db.execute("UPDATE hive_research_branches SET status='resolved',updated_at=NOW() WHERE id=?",[bid]); after={'frontier_status':'resolved'}
    elif action=='refine':
        note=guidance.strip()[:2000]; db.execute("UPDATE hive_frontiers SET rationale=?,status='open',updated_at=NOW() WHERE id=?",[(f[1] or '')+(f'\nRefinement: {note}' if note else '\nRefinement requested: narrow this frontier into more observable and searchable sub-problems.'),frontier_id]); db.execute("UPDATE hive_research_branches SET status='watching',reason=?,no_progress_cycles=0,updated_at=NOW() WHERE id=?",[note or 'Refine into observable and searchable sub-problems',bid]); after={'frontier_status':'open','refinement':note or 'narrow scope'}
    else: db.close(); raise ValueError('Unsupported frontier action')
    _record_change(db,mission_id,'frontier_control',f'{action.replace("_"," ").title()}: {f[0]}',guidance or 'Manual frontier lifecycle control',before,after,.9,bid); db.close(); return {'id':frontier_id,'action':action,'status':after['frontier_status'],'queued':action=='research_now'}
