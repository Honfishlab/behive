"""Stateful continuous-research controller.

Turns a timer-driven discovery rerun into a branch-aware action loop with
durable memory, deduplication, ranked next actions, retries, stopping rules,
and a human-readable change ledger.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from hashlib import sha1

from behive.engine.db import connect

log = logging.getLogger(__name__)
MAX_ACTIVE_BRANCHES = 40
MAX_PENDING_ACTIONS = 40
MIN_OLDEST_ACTIONS = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_research_branches (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, subject_type VARCHAR NOT NULL,
 subject_id VARCHAR NOT NULL, title TEXT NOT NULL, status VARCHAR NOT NULL DEFAULT 'watching',
 reason TEXT, uncertainty DOUBLE PRECISION DEFAULT .5, evidence_coverage DOUBLE PRECISION DEFAULT 0,
 priority DOUBLE PRECISION DEFAULT .5, queries_attempted JSONB DEFAULT '[]'::jsonb,
 sources_seen JSONB DEFAULT '[]'::jsonb, retry_count INTEGER DEFAULT 0,
 duplicate_streak INTEGER DEFAULT 0, no_progress_cycles INTEGER DEFAULT 0,
 last_action VARCHAR, last_checked_at TIMESTAMP, next_review_at TIMESTAMP,
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,subject_type,subject_id)
);
CREATE TABLE IF NOT EXISTS hive_research_actions (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, branch_id VARCHAR,
 action_type VARCHAR NOT NULL, rationale TEXT, status VARCHAR NOT NULL DEFAULT 'queued',
 score DOUBLE PRECISION DEFAULT 0, information_gain DOUBLE PRECISION DEFAULT 0,
 source_quality_gain DOUBLE PRECISION DEFAULT 0, novelty DOUBLE PRECISION DEFAULT 0,
 feasibility DOUBLE PRECISION DEFAULT 0, cost DOUBLE PRECISION DEFAULT 0,
 fingerprint VARCHAR NOT NULL, attempt INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
 payload JSONB DEFAULT '{}'::jsonb, result JSONB DEFAULT '{}'::jsonb,
 error_message TEXT, queued_at TIMESTAMP DEFAULT NOW(), started_at TIMESTAMP,
 completed_at TIMESTAMP, next_attempt_at TIMESTAMP,
 UNIQUE(mission_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS hive_research_changes (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, branch_id VARCHAR,
 change_type VARCHAR NOT NULL, headline TEXT NOT NULL, detail TEXT,
 before_state JSONB DEFAULT '{}'::jsonb, after_state JSONB DEFAULT '{}'::jsonb,
 importance DOUBLE PRECISION DEFAULT .5, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hive_research_queries (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, branch_id VARCHAR,
 normalized_query TEXT NOT NULL, query_hash VARCHAR NOT NULL, result_count INTEGER DEFAULT 0,
 novel_source_count INTEGER DEFAULT 0, status VARCHAR DEFAULT 'attempted', attempted_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,query_hash)
);
CREATE INDEX IF NOT EXISTS idx_research_branches_state ON hive_research_branches(mission_id,status,priority DESC);
CREATE INDEX IF NOT EXISTS idx_research_actions_queue ON hive_research_actions(mission_id,status,score DESC,queued_at);
CREATE INDEX IF NOT EXISTS idx_research_changes_time ON hive_research_changes(mission_id,created_at DESC);
"""


def ensure_schema(db=None) -> None:
    owned = db is None
    db = db or connect()
    for statement in (part.strip() for part in SCHEMA.split(";") if part.strip()):
        db.execute(statement)
    db.execute("ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS project_id TEXT")
    db.execute("ALTER TABLE hive_missions ADD COLUMN IF NOT EXISTS question_id TEXT")
    db.execute("ALTER TABLE hive_questions ADD COLUMN IF NOT EXISTS evidence_completeness JSONB DEFAULT '{}'::jsonb")
    db.execute("ALTER TABLE hive_questions ADD COLUMN IF NOT EXISTS success_criteria JSONB DEFAULT '{}'::jsonb")
    db.execute("ALTER TABLE hive_questions ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0")
    db.execute("ALTER TABLE hive_questions ADD COLUMN IF NOT EXISTS last_researched_at TIMESTAMPTZ")
    if owned:
        db.close()


def _branch_id(mission_id: str, subject_type: str, subject_id: str) -> str:
    raw = f"{mission_id}|{subject_type}|{subject_id}".lower().encode()
    return f"rb_{sha1(raw).hexdigest()[:20]}"


def _controller_anchor(db, mission_id: str) -> str:
    """Return the single root-question mission that owns a project's queue."""
    row = db.execute(
        "SELECT m.project_id,q.parent_id FROM hive_missions m "
        "LEFT JOIN hive_questions q ON q.id=m.question_id WHERE m.id=?", [mission_id]
    ).fetchone()
    if not row or not row[0] or row[1] is None:
        return mission_id
    anchor = db.execute(
        "SELECT m.id FROM hive_missions m JOIN hive_questions q ON q.id=m.question_id "
        "WHERE m.project_id=? AND q.parent_id IS NULL ORDER BY m.created_at DESC LIMIT 1",
        [row[0]],
    ).fetchone()
    return anchor[0] if anchor else mission_id


def _retire_child_controllers(db, mission_id: str) -> None:
    """Supersede recursive queues accidentally created under child missions."""
    project = db.execute("SELECT project_id FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    if not project or not project[0]: return
    db.execute(
        "UPDATE hive_research_actions SET status='superseded',completed_at=NOW(),"
        "error_message='Retired: project queue is owned by root mission' "
        "WHERE mission_id<>? AND mission_id IN (SELECT id FROM hive_missions WHERE project_id=?) "
        "AND status IN ('queued','running','retrying')", [mission_id,project[0]],
    )
    db.execute(
        "UPDATE hive_research_branches SET status='satisfied',updated_at=NOW() "
        "WHERE mission_id<>? AND mission_id IN (SELECT id FROM hive_missions WHERE project_id=?) "
        "AND status IN ('watching','searching','retrieving','extracting','verifying','hypothesis_testing','blocked')",
        [mission_id,project[0]],
    )


def _fingerprint(branch_id: str, action_type: str, generation: int) -> str:
    return sha1(f"{branch_id}|{action_type}|{generation}".encode()).hexdigest()[:24]


def _snapshot(db, mission_id: str) -> dict:
    counts = {}
    for key, table in (("sources", "hive_sources"), ("documents", "hive_content"),
                       ("findings", "hive_claims"), ("nodes", "hive_discovery_nodes"),
                       ("edges", "hive_discovery_edges"), ("frontiers", "hive_frontiers"),
                       ("hypotheses", "hive_hypothesis_paths")):
        counts[key] = db.execute(f"SELECT COUNT(*) FROM {table} WHERE mission_id=?", [mission_id]).fetchone()[0]
    return counts


def _record_change(db, mission_id: str, change_type: str, headline: str, detail: str,
                   before: dict, after: dict, importance: float = .5, branch_id: str | None = None) -> None:
    db.execute(
        "INSERT INTO hive_research_changes (mission_id,branch_id,change_type,headline,detail,before_state,after_state,importance) "
        "VALUES (?,?,?,?,?,?::jsonb,?::jsonb,?)",
        [mission_id, branch_id, change_type, headline, detail, json.dumps(before), json.dumps(after), importance],
    )


def _sync_branches(db, mission_id: str) -> int:
    mission = db.execute("SELECT project_id FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    project_id = mission[0] if mission else None
    if project_id:
        db.execute(
            "UPDATE hive_research_branches b SET status='satisfied',updated_at=NOW() "
            "FROM hive_questions q WHERE b.mission_id=? AND b.subject_type='question' "
            "AND b.subject_id=q.id AND q.project_id=? "
            "AND q.status IN ('evidence_found','covered','answered','paused','archived')",
            [mission_id, project_id],
        )
        db.execute(
            "UPDATE hive_research_actions a SET status='superseded',completed_at=NOW(),"
            "error_message='Question already reached a terminal tree status' "
            "FROM hive_research_branches b WHERE a.branch_id=b.id AND b.mission_id=? "
            "AND b.status='satisfied' AND a.status IN ('queued','retrying')",
            [mission_id],
        )
    # evidence_found is terminal for this exact question attempt. Any explicit
    # evidence gaps have already been expanded into their own child questions;
    # reactivating the parent here causes duplicate missions and starves them.
    questions = db.execute(
        "SELECT id,question,priority,confidence,status FROM hive_questions WHERE project_id=? "
        "AND status IN ('open','queued','researching','blocked','evidence_boundary') "
        "ORDER BY priority DESC,depth,id",
        [project_id],
    ).fetchall() if project_id else []
    synced = 0
    for qid, title, priority, confidence, status in questions:
        bid = _branch_id(mission_id, "question", qid)
        coverage = max(0, min(1, float(confidence or 0)))
        branch_status = "watching" if status != "blocked" else "blocked"
        db.execute(
            """INSERT INTO hive_research_branches
               (id,mission_id,subject_type,subject_id,title,status,reason,uncertainty,evidence_coverage,priority)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title,status=EXCLUDED.status,
               uncertainty=EXCLUDED.uncertainty,evidence_coverage=EXCLUDED.evidence_coverage,
               priority=EXCLUDED.priority,updated_at=NOW()""",
            [bid, mission_id, "question", qid, title, branch_status,
             "Open question-tree branch awaiting targeted evidence", 1-coverage, coverage, float(priority or .5)],
        )
        synced += 1
    frontiers = db.execute(
        "SELECT id,title,frontier_type,rationale,scores,priority,status FROM hive_frontiers "
        "WHERE mission_id=? AND status NOT IN ('archived','dismissed') ORDER BY priority DESC",
        [mission_id],
    ).fetchall()
    for fid, title, ftype, rationale, scores, priority, status in frontiers:
        scores = scores or {}
        evidence = float(scores.get("evidence_strength", 0) or 0)
        uncertainty = max(0, min(1, 1 - evidence))
        bid = _branch_id(mission_id, "frontier", fid)
        db.execute(
            """INSERT INTO hive_research_branches
               (id,mission_id,subject_type,subject_id,title,status,reason,uncertainty,evidence_coverage,priority)
               VALUES (?,?,?,?,?,'watching',?,?,?,?)
               ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title,reason=EXCLUDED.reason,
               uncertainty=EXCLUDED.uncertainty,evidence_coverage=EXCLUDED.evidence_coverage,
               priority=EXCLUDED.priority,updated_at=NOW()""",
            [bid, mission_id, "frontier", fid, title, rationale or f"Open {ftype} frontier",
             uncertainty, evidence, float(priority or .5)],
        )
        synced += 1
    return synced


def _choose_action(branch: tuple) -> tuple[str, str, dict]:
    bid, subject_id, title, status, reason, uncertainty, coverage, priority, retries, duplicates, stagnant = branch
    if str(bid).startswith("rb_") and reason == "Open question-tree branch awaiting targeted evidence":
        kind = "research_question"
        rationale = "This ranked question-tree branch has no linked evidence mission; run targeted research and attach its results."
    elif coverage < .3:
        kind = "refresh_evidence_map"
        rationale = "Evidence coverage is low; inspect newly available mission documents and missing connections."
    elif stagnant > 0:
        kind = "audit_graph"
        rationale = "The branch made no progress; audit unsupported links and hidden contradictions before spending more."
    else:
        kind = "evolve_hypotheses"
        rationale = "Evidence exists but uncertainty remains; evolve competing explanations and discriminating tests."
    metrics = {
        "information_gain": min(1, .35 + uncertainty * .55),
        "source_quality_gain": min(1, .2 + (1 - coverage) * .65),
        "novelty": min(1, .4 + uncertainty * .45),
        "feasibility": .82 if kind != "refresh_evidence_map" else .68,
        "cost": .2 if kind == "audit_graph" else .45,
    }
    return kind, rationale, metrics


def _score_question(db, question_id: str, mission_id: str | None) -> dict:
    """Score evidence adequacy without mistaking narrative generation for coverage."""
    if not mission_id:
        return {"score": 0, "findings": 0, "audited": 0, "sources": 0, "domains": 0,
                "direct": 0, "contradictions": 0, "status": "queued", "gaps": ["No research mission has run"]}
    row = db.execute(
        "SELECT COUNT(*),COUNT(*) FILTER (WHERE integrity_status='supported'),"
        "COUNT(DISTINCT source_url),COUNT(DISTINCT NULLIF(split_part(split_part(source_url,'://',2),'/',1),'')),"
        "COUNT(*) FILTER (WHERE COALESCE(quality_details->>'evidence_class','')='direct'),"
        "COUNT(*) FILTER (WHERE claim_type ILIKE ?) "
        "FROM hive_claims WHERE mission_id=? AND COALESCE(is_garbage,FALSE)=FALSE",
        ["%contradict%", mission_id]
    ).fetchone()
    findings, audited, sources, domains, direct, contradictions = [int(v or 0) for v in row]
    audit_ratio = audited / findings if findings else 0
    score = min(1, .25*min(findings/3,1)+.25*audit_ratio+.2*min(sources/3,1)+
                .15*min(domains/2,1)+.15*min(direct/1,1))
    gaps = []
    if findings < 3: gaps.append("Fewer than 3 supported findings")
    if audit_ratio < .75: gaps.append("Citation integrity coverage below 75%")
    if domains < 2: gaps.append("Insufficient independent source diversity")
    if direct < 1: gaps.append("No direct evidence identified")
    status = "covered" if score >= .75 and not gaps else "evidence_found" if findings else "evidence_boundary"
    return {"score": round(score,4), "findings": findings, "audited": audited, "sources": sources,
            "domains": domains, "direct": direct, "contradictions": contradictions,
            "status": status, "gaps": gaps}


def _propagate_question_state(db, question_id: str) -> None:
    """Recompute parents from child coverage while preserving unresolved unknowns."""
    current = question_id
    for _ in range(20):
        parent = db.execute("SELECT parent_id FROM hive_questions WHERE id=?", [current]).fetchone()
        if not parent or not parent[0]: break
        parent_id = parent[0]
        rows = db.execute("SELECT status,evidence_completeness FROM hive_questions WHERE parent_id=? AND status<>'archived'", [parent_id]).fetchall()
        if not rows: break
        covered = sum(1 for status,_ in rows if status in ('covered','answered'))
        active = sum(1 for status,_ in rows if status == 'researching')
        scores = [float((details or {}).get('score',0) or 0) for _,details in rows]
        aggregate = round(sum(scores)/len(scores),4) if scores else 0
        parent_status = 'researching' if active else 'covered' if covered == len(rows) else 'evidence_found' if covered else 'open'
        db.execute("UPDATE hive_questions SET status=?,confidence=?,evidence_completeness="
                   "COALESCE(evidence_completeness,'{}'::jsonb)||?::jsonb,updated_at=NOW() WHERE id=?",
                   [parent_status, aggregate, json.dumps({"child_coverage": aggregate,"children_covered": covered,
                    "children_total": len(rows)}), parent_id])
        current = parent_id


def finalize_question_mission(mission_id: str) -> dict:
    """Attach a terminal mission to its question and seed follow-up work.

    This is deliberately idempotent: API completion, recovery, and startup
    reconciliation may all call it without duplicating project findings or
    incrementing a question's attempt count more than once per mission.
    """
    db = connect(); ensure_schema(db)
    row = db.execute(
        "SELECT project_id,question_id,status FROM hive_missions WHERE id=?", [mission_id]
    ).fetchone()
    if not row or not row[0] or not row[1]:
        db.close()
        return {"mission_id": mission_id, "status": "not_linked"}
    project_id, question_id, mission_status = row
    if mission_status not in ("done", "insufficient"):
        db.close()
        return {"mission_id": mission_id, "status": "not_terminal"}
    question = db.execute(
        "SELECT question,mission_id,last_researched_at FROM hive_questions "
        "WHERE id=? AND project_id=?", [question_id, project_id]
    ).fetchone()
    if not question:
        db.close()
        return {"mission_id": mission_id, "status": "question_missing"}

    completeness = _score_question(db, question_id, mission_id)
    already_finalized = question[1] == mission_id and question[2] is not None
    question_status = completeness["status"]
    db.execute(
        "UPDATE hive_questions SET mission_id=?,status=?,confidence=?,"
        "evidence_completeness=?::jsonb,attempts=attempts+?,last_researched_at=NOW(),"
        "updated_at=NOW() WHERE id=?",
        [mission_id, question_status, completeness["score"], json.dumps(completeness),
         0 if already_finalized else 1, question_id],
    )
    db.execute(
        "INSERT INTO hive_findings (project_id,question_id,claim_id,source_url,summary,confidence,novelty,status) "
        "SELECT ?,?,c.id,c.source_url,c.claim,c.confidence,COALESCE(c.quality_score,.5),'supported' "
        "FROM hive_claims c WHERE c.mission_id=? AND COALESCE(c.is_garbage,FALSE)=FALSE "
        "AND NOT EXISTS (SELECT 1 FROM hive_findings f WHERE f.question_id=? AND f.claim_id=c.id)",
        [project_id, question_id, mission_id, question_id],
    )
    promoted = db.execute(
        "SELECT COUNT(*) FROM hive_findings WHERE project_id=? AND question_id=?",
        [project_id, question_id],
    ).fetchone()[0]
    expanded = 0 if already_finalized else _expand_residual_unknowns(
        db, project_id, question_id, question[0], completeness
    )
    _propagate_question_state(db, question_id)
    db.commit(); db.close()

    # Findings become bounded, typed research paths. Continuous policy promotes
    # only the highest-value paths into executable question-tree branches.
    from behive.engine.discovery_paths import generate_paths, activate_ranked
    paths = generate_paths(mission_id, project_id)
    activated = activate_ranked(mission_id, limit=3, require_continuous=True)
    # Reconciliation creates durable branches/actions for every open question.
    queue = reconcile(mission_id, trigger="mission_complete")
    return {"mission_id": mission_id, "status": "finalized", "project_id": project_id,
            "question_id": question_id, "question_status": question_status,
            "findings_promoted": int(promoted or 0), "new_questions": expanded,
            "paths_generated": paths.get("generated", 0),
            "paths_activated": activated.get("activated", 0),
            "actions_queued": queue.get("actions_queued", 0),
            "branches_synced": queue.get("branches_synced", 0)}


def _expand_residual_unknowns(db, project_id: str, question_id: str, question: str, completeness: dict) -> int:
    """Turn explicit evidence deficits into bounded, researchable child probes."""
    policy_row = db.execute("SELECT agent_policy FROM hive_projects WHERE id=?", [project_id]).fetchone()
    policy = policy_row[0] if policy_row and policy_row[0] else {}
    depth_row = db.execute("SELECT depth FROM hive_questions WHERE id=?", [question_id]).fetchone()
    depth = int(depth_row[0] or 0) if depth_row else 0
    if not policy.get("approval_new_branches", True) or depth >= int(policy.get("max_depth",5)):
        return 0
    templates = {
        "Fewer than 3 supported findings": f"What additional independently sourced findings directly bear on: {question}",
        "Citation integrity coverage below 75%": f"Which primary-source passages directly support or refute: {question}",
        "Insufficient independent source diversity": f"What independent research groups or authoritative datasets provide evidence about: {question}",
        "No direct evidence identified": f"What direct observations or measurements exist for: {question}",
    }
    added = 0
    for gap in completeness.get("gaps", [])[:2]:
        child = templates.get(gap)
        if not child: continue
        child_id = f"question_{sha1((question_id+'|'+gap).encode()).hexdigest()[:16]}"
        db.execute("INSERT INTO hive_questions (id,project_id,parent_id,question,depth,kind,priority,status) "
                   "VALUES (?,?,?,?,?,'evidence gap',.85,'open') ON CONFLICT (id) DO NOTHING",
                   [child_id,project_id,question_id,child[:2000],depth+1])
        added += 1
    return added


def _research_question(mission_id: str, branch_id: str, action_id: int) -> dict:
    """Research one queued question and retain its own evidence mission."""
    db = connect()
    row = db.execute(
        "SELECT b.subject_id,b.title,m.project_id FROM hive_research_branches b "
        "JOIN hive_missions m ON m.id=b.mission_id WHERE b.id=? AND b.subject_type='question'",
        [branch_id],
    ).fetchone()
    if not row:
        db.close()
        raise RuntimeError("Question branch is no longer available")
    question_id, question, project_id = row
    child_id = f"question_action_{action_id}"
    existing = db.execute(
        "SELECT status,(SELECT COUNT(*) FROM hive_claims WHERE mission_id=?) FROM hive_missions WHERE id=?",
        [child_id, child_id],
    ).fetchone()
    if existing and existing[0] in ("done", "insufficient"):
        db.close()
        return {"child_mission_id": child_id, "question_id": question_id,
                "status": existing[0], "claims": existing[1], "reused": True}
    db.execute(
        "INSERT INTO hive_missions (id,topic,status,phase,config,project_id,question_id,created_at) "
        "VALUES (?,?,'queued','queued',?::jsonb,?,?,NOW()) ON CONFLICT (id) DO NOTHING",
        [child_id, question, json.dumps({"depth": 4, "scale": 80, "parent_mission": mission_id}),
         project_id, question_id],
    )
    db.execute("UPDATE hive_questions SET mission_id=?,status='researching',updated_at=NOW() WHERE id=?",
               [child_id, question_id])
    db.commit()
    db.close()
    env = {**os.environ, "BEHIVE_MISSION_ID": child_id, "PYTHONUNBUFFERED": "1",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "behive.engine", "run", question, "--mission-id", child_id,
         "--depth", "4", "--scale", "80", "--force"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=1800,
    )
    db = connect()
    result = db.execute(
        "SELECT status,(SELECT COUNT(*) FROM hive_claims WHERE mission_id=?) FROM hive_missions WHERE id=?",
        [child_id, child_id],
    ).fetchone() or ("error", 0)
    child_status, claims = result
    if child_status in ("done", "insufficient", "error", "cancelled", "interrupted"):
        db.execute("UPDATE hive_missions SET phase=? WHERE id=?", [child_status, child_id])
    completeness = _score_question(db, question_id, child_id)
    question_status = completeness["status"] if child_status in ("done", "insufficient") else "blocked"
    db.execute(
        "UPDATE hive_questions SET status=?,confidence=?,evidence_completeness=?::jsonb,"
        "attempts=attempts+1,last_researched_at=NOW(),updated_at=NOW() WHERE id=?",
        [question_status, completeness["score"], json.dumps(completeness), question_id],
    )
    db.execute(
        "INSERT INTO hive_findings (project_id,question_id,claim_id,source_url,summary,confidence,novelty,status) "
        "SELECT ?,?,c.id,c.source_url,c.claim,c.confidence,COALESCE(c.quality_score,.5),'supported' "
        "FROM hive_claims c WHERE c.mission_id=? AND COALESCE(c.is_garbage,FALSE)=FALSE "
        "AND NOT EXISTS (SELECT 1 FROM hive_findings f WHERE f.question_id=? AND f.claim_id=c.id)",
        [project_id, question_id, child_id, question_id],
    )
    expanded = _expand_residual_unknowns(db, project_id, question_id, question, completeness)
    _propagate_question_state(db, question_id)
    db.commit()
    db.close()
    from behive.engine.discovery_paths import sync_path_states
    sync_path_states(project_id)
    if proc.returncode != 0:
        raise RuntimeError(f"Question research worker exited {proc.returncode}: {proc.stdout[-600:]}")
    return {"child_mission_id": child_id, "question_id": question_id,
            "status": child_status, "claims": claims, "completeness": completeness,
            "new_questions": expanded}


def compact_pending_queue(db, mission_id: str) -> dict:
    """Bound pending work while retaining the oldest work and highest-value newer work."""
    rows = db.execute(
        "SELECT id,score,queued_at FROM hive_research_actions WHERE mission_id=? "
        "AND status IN ('queued','retrying') ORDER BY queued_at,id", [mission_id]
    ).fetchall()
    if len(rows) <= MAX_PENDING_ACTIONS:
        return {"pending_before": len(rows), "superseded": 0, "pending_after": len(rows)}
    now = datetime.now()
    oldest = [r[0] for r in rows[:MIN_OLDEST_ACTIONS]]
    # Age raises effective priority by up to .5, preventing indefinite starvation.
    ranked = sorted(rows, key=lambda r: float(r[1] or 0) + min(.5, max(0, (now-r[2]).total_seconds())/3600*.03), reverse=True)
    keep = list(dict.fromkeys(oldest + [r[0] for r in ranked]))[:MAX_PENDING_ACTIONS]
    discard = [r[0] for r in rows if r[0] not in set(keep)]
    if discard:
        db.execute("UPDATE hive_research_actions SET status='superseded',error_message='Queue compacted: lower value than retained pending work' WHERE id=ANY(?)", [discard])
    return {"pending_before": len(rows), "superseded": len(discard), "pending_after": len(keep)}


def reconcile(mission_id: str, trigger: str = "controller") -> dict:
    """Synchronize branches, record graph changes, and queue ranked actions."""
    db = connect(); ensure_schema(db)
    mission_id = _controller_anchor(db, mission_id)
    _retire_child_controllers(db, mission_id)
    before_row = db.execute(
        "SELECT after_state FROM hive_research_changes WHERE mission_id=? AND change_type='snapshot' "
        "ORDER BY id DESC LIMIT 1", [mission_id]
    ).fetchone()
    before = before_row[0] if before_row else {}
    after = _snapshot(db, mission_id)
    if before != after:
        deltas = {k: after[k] - int(before.get(k, 0) or 0) for k in after}
        changed = [f"{k} {v:+d}" for k, v in deltas.items() if v]
        _record_change(db, mission_id, "snapshot", "Research state changed",
                       ", ".join(changed) or "Initial controller baseline", before, after,
                       .8 if any(v > 0 for v in deltas.values()) else .3)
    synced = _sync_branches(db, mission_id)
    generation = db.execute(
        "SELECT COALESCE(MAX(id),0)+1 FROM hive_research_changes WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    queue = compact_pending_queue(db, mission_id)
    branches = db.execute(
        "SELECT id,subject_id,title,status,reason,uncertainty,evidence_coverage,priority,retry_count,"
        "duplicate_streak,no_progress_cycles FROM hive_research_branches WHERE mission_id=? "
        "AND status IN ('watching','searching','retrieving','extracting','verifying','hypothesis_testing','blocked') "
        "ORDER BY priority DESC,created_at LIMIT ?", [mission_id, MAX_ACTIVE_BRANCHES]
    ).fetchall()
    queued = 0
    for branch in branches:
        if queue["pending_after"] + queued >= MAX_PENDING_ACTIONS:
            break
        bid, _, title, status, _, uncertainty, coverage, priority, retries, duplicates, stagnant = branch
        if stagnant >= 3 or duplicates >= 3:
            next_state = "exhausted" if stagnant >= 5 else "scheduled_revisit"
            db.execute("UPDATE hive_research_branches SET status=?,next_review_at=NOW()+INTERVAL '7 days',updated_at=NOW() WHERE id=?",
                       [next_state, bid])
            _record_change(db, mission_id, "branch_paused", f"Paused: {title}",
                           "Diminishing returns threshold reached; scheduled for later review.",
                           {"status": status}, {"status": next_state}, .65, bid)
            from behive.engine.decision_ledger import record
            record(mission_id,"branch_disposition",f"Branch {next_state.replace('_',' ')}: {title}",
                   "Diminishing returns threshold reached after repeated duplicate or no-progress cycles.",
                   subject_key=bid,impact="stops",before={"status":status,"no_progress_cycles":stagnant,"duplicate_streak":duplicates},
                   after={"status":next_state,"next_review":"7 days"},
                   reversal_conditions="Resume early if a new authoritative source, contradiction, or materially different measurement becomes available.")
            continue
        active = db.execute(
            "SELECT 1 FROM hive_research_actions WHERE branch_id=? AND status IN ('queued','running','retrying') LIMIT 1",
            [bid],
        ).fetchone()
        if active:
            continue
        kind, rationale, metrics = _choose_action(branch)
        score = round(.34*metrics["information_gain"] + .2*metrics["source_quality_gain"] +
                      .18*metrics["novelty"] + .18*metrics["feasibility"] + .1*(1-metrics["cost"]), 4)
        fingerprint = _fingerprint(bid, kind, int(generation))
        db.execute(
            """INSERT INTO hive_research_actions
               (mission_id,branch_id,action_type,rationale,score,information_gain,source_quality_gain,
                novelty,feasibility,cost,fingerprint,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?::jsonb)
               ON CONFLICT (mission_id,fingerprint) DO NOTHING""",
            [mission_id,bid,kind,rationale,score,metrics["information_gain"],metrics["source_quality_gain"],
             metrics["novelty"],metrics["feasibility"],metrics["cost"],fingerprint,
             json.dumps({"trigger": trigger, "branch_title": title})],
        )
        queued += 1
    db.commit(); db.close()
    return {"mission_id": mission_id, "branches_synced": synced, "actions_queued": queued, "queue": queue,
            "before": before, "after": after}


def _run_next_action_locked(mission_id: str, trigger: str = "continuous") -> dict:
    """Claim and execute an action while the caller owns the mission lease."""
    reconcile_result = reconcile(mission_id, trigger)
    db = connect(); ensure_schema(db)
    row = db.execute(
        """SELECT a.id,a.branch_id,a.action_type,a.rationale,a.attempt
           FROM hive_research_actions a
           WHERE a.mission_id=? AND a.status IN ('queued','retrying')
             AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=NOW())
           ORDER BY (a.score + LEAST(.5,EXTRACT(EPOCH FROM (NOW()-a.queued_at))/3600*.03)) DESC,
                    a.queued_at LIMIT 1 FOR UPDATE SKIP LOCKED""", [mission_id]
    ).fetchone()
    if not row:
        db.close(); return {**reconcile_result, "status": "idle", "reason": "No actionable branch"}
    action_id, branch_id, action_type, rationale, attempt = row
    title_row = db.execute("SELECT title FROM hive_research_branches WHERE id=?", [branch_id]).fetchone()
    title = title_row[0] if title_row else action_type.replace("_", " ")
    db.execute("UPDATE hive_research_actions SET status='running',attempt=attempt+1,started_at=NOW() WHERE id=?", [action_id])
    db.execute("UPDATE hive_research_branches SET status=?,last_action=?,last_checked_at=NOW(),updated_at=NOW() WHERE id=?",
               ["hypothesis_testing" if action_type == "evolve_hypotheses" else "verifying", action_type, branch_id])
    db.commit(); db.close()
    before = reconcile_result["after"]
    try:
        if action_type == "research_question":
            result = _research_question(mission_id, branch_id, action_id)
        elif action_type == "refresh_evidence_map":
            from behive.engine.frontier import run_frontier_cycle
            result = run_frontier_cycle(mission_id, trigger=f"controller:{trigger}")
        elif action_type == "evolve_hypotheses":
            from behive.engine.evolution import evolve_hypotheses
            result = evolve_hypotheses(mission_id, trigger=f"controller:{trigger}")
        else:
            from behive.engine.evolution import audit_graph
            result = audit_graph(mission_id)
        db = connect(); after = _snapshot(db, mission_id)
        progress = sum(max(0, after[k]-before.get(k, 0)) for k in after)
        db.execute("UPDATE hive_research_actions SET status='complete',result=?::jsonb,completed_at=NOW() WHERE id=?",
                   [json.dumps(result), action_id])
        db.execute("UPDATE hive_research_branches SET status='watching',"
                   "no_progress_cycles=CASE WHEN ? THEN 0 ELSE no_progress_cycles+1 END,"
                   "duplicate_streak=CASE WHEN ? THEN 0 ELSE duplicate_streak+1 END,"
                   "retry_count=0,updated_at=NOW() WHERE id=?",
                   [bool(progress), bool(progress), branch_id])
        _record_change(db, mission_id, "action_complete", f"{action_type.replace('_',' ').title()}: {title}",
                       f"{progress} new graph objects; {rationale}", before, after, .75 if progress else .4, branch_id)
        from behive.engine.decision_ledger import record
        record(mission_id,"controller_action",f"{action_type.replace('_',' ').title()}: {title}",
               f"{progress} new graph objects. {rationale}",subject_key=branch_id,
               impact="changes" if progress else "no_change",before=before,after=after,
               reversal_conditions="Re-rank this branch if later evidence changes coverage, uncertainty, or contradiction state.")
        db.commit(); db.close()
        return {**reconcile_result, "status": "complete", "action_id": action_id,
                "action_type": action_type, "progress": progress, "result": result}
    except Exception as exc:
        db = connect()
        attempt_now = int(attempt or 0) + 1
        terminal = attempt_now >= 3
        db.execute("UPDATE hive_research_actions SET status=?,error_message=?,next_attempt_at="
                   "CASE WHEN ? THEN NULL ELSE NOW()+(POWER(2,?)||' minutes')::interval END WHERE id=?",
                   ["failed" if terminal else "retrying", str(exc)[:1200], terminal, attempt_now, action_id])
        db.execute("UPDATE hive_research_branches SET status=?,retry_count=retry_count+1,reason=?,updated_at=NOW() WHERE id=?",
                   ["blocked" if terminal else "watching", f"{action_type} failed: {str(exc)[:500]}", branch_id])
        _record_change(db, mission_id, "action_failed", f"Action failed: {title}", str(exc)[:1000],
                       {"attempt": attempt_now-1}, {"attempt": attempt_now, "terminal": terminal}, .8, branch_id)
        db.commit(); db.close()
        raise


def run_next_action(mission_id: str, trigger: str = "continuous") -> dict:
    """Execute at most one action across every Behive server sharing PostgreSQL."""
    anchor_db = connect()
    mission_id = _controller_anchor(anchor_db, mission_id)
    anchor_db.close()
    lease_db = connect()
    acquired = lease_db.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(?,0))", [f"behive-controller:{mission_id}"]
    ).fetchone()[0]
    if not acquired:
        lease_db.close()
        return {"mission_id": mission_id, "status": "busy",
                "reason": "Another Behive server owns this mission's controller lease"}
    try:
        return _run_next_action_locked(mission_id, trigger)
    finally:
        try:
            lease_db.execute("SELECT pg_advisory_unlock(hashtextextended(?,0))",
                             [f"behive-controller:{mission_id}"])
        finally:
            lease_db.close()


def get_controller_state(mission_id: str) -> dict:
    # Dashboard reads must never create branches or queue actions.
    ensure_schema()
    db = connect()
    mission_id = _controller_anchor(db, mission_id)
    branches = db.execute(
        "SELECT id,subject_type,subject_id,title,status,reason,uncertainty,evidence_coverage,priority,retry_count,"
        "duplicate_streak,no_progress_cycles,last_action,last_checked_at,next_review_at FROM hive_research_branches "
        "WHERE mission_id=? ORDER BY priority DESC", [mission_id]
    ).fetchall()
    actions = db.execute(
        "SELECT id,branch_id,action_type,rationale,status,score,information_gain,source_quality_gain,novelty,"
        "feasibility,cost,attempt,max_attempts,error_message,queued_at,started_at,completed_at FROM hive_research_actions "
        "WHERE mission_id=? ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'retrying' THEN 2 ELSE 3 END,score DESC LIMIT 100",
        [mission_id]
    ).fetchall()
    changes = db.execute(
        "SELECT id,branch_id,change_type,headline,detail,before_state,after_state,importance,created_at "
        "FROM hive_research_changes WHERE mission_id=? ORDER BY created_at DESC LIMIT 100", [mission_id]
    ).fetchall()
    status_rows = db.execute(
        "SELECT status,COUNT(*) FROM hive_research_actions WHERE mission_id=? GROUP BY status", [mission_id]
    ).fetchall()
    status_counts = {r[0]: r[1] for r in status_rows}
    oldest_pending = db.execute(
        "SELECT MIN(queued_at) FROM hive_research_actions WHERE mission_id=? AND status IN ('queued','retrying')",
        [mission_id],
    ).fetchone()[0]
    elapsed_row = db.execute(
        "SELECT EXTRACT(EPOCH FROM (NOW()-MAX(completed_at))) FROM hive_research_actions "
        "WHERE mission_id=? AND status='complete'", [mission_id]
    ).fetchone()
    elapsed = float(elapsed_row[0]) if elapsed_row and elapsed_row[0] is not None else None
    cooldown_seconds = max(0, int(300-elapsed)) if elapsed is not None else 0
    policy_row = db.execute(
        "SELECT p.agent_policy FROM hive_projects p JOIN hive_missions m "
        "ON m.project_id=p.id WHERE m.id=? LIMIT 1", [mission_id]
    ).fetchone()
    policy = policy_row[0] if policy_row and policy_row[0] else {}
    running_count = status_counts.get("running", 0)
    pending_count = status_counts.get("queued",0)+status_counts.get("retrying",0)
    if policy.get("autonomy") != "continuous" or policy.get("status") != "running":
        continuous_state = "paused"
    elif running_count:
        continuous_state = "running"
    elif pending_count and cooldown_seconds:
        continuous_state = "cooldown"
    elif pending_count:
        continuous_state = "ready"
    else:
        continuous_state = "monitoring"
    db.close()
    return {
        "mission_id": mission_id,
        "branches": [{"id":r[0],"subject_type":r[1],"subject_id":r[2],"title":r[3],"status":r[4],"reason":r[5],
                      "uncertainty":r[6],"evidence_coverage":r[7],"priority":r[8],"retry_count":r[9],
                      "duplicate_streak":r[10],"no_progress_cycles":r[11],"last_action":r[12],
                      "last_checked_at":r[13],"next_review_at":r[14]} for r in branches],
        "actions": [{"id":r[0],"branch_id":r[1],"type":r[2],"rationale":r[3],"status":r[4],"score":r[5],
                     "information_gain":r[6],"source_quality_gain":r[7],"novelty":r[8],"feasibility":r[9],
                     "cost":r[10],"attempt":r[11],"max_attempts":r[12],"error":r[13],"queued_at":r[14],
                     "started_at":r[15],"completed_at":r[16]} for r in actions],
        "changes": [{"id":r[0],"branch_id":r[1],"type":r[2],"headline":r[3],"detail":r[4],"before":r[5],
                     "after":r[6],"importance":r[7],"created_at":r[8]} for r in changes],
        "metrics": {"statuses": status_counts, "pending": pending_count,
                    "completed": status_counts.get("complete",0), "failed": status_counts.get("failed",0),
                    "superseded": status_counts.get("superseded",0), "oldest_pending_at": oldest_pending,
                    "queue_limit": MAX_PENDING_ACTIONS, "continuous_state": continuous_state,
                    "cooldown_seconds": cooldown_seconds,
                    "next_eligible_at": datetime.now()+timedelta(seconds=cooldown_seconds) if cooldown_seconds else None,
                    "policy": policy},
    }
