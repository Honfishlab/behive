"""Stateful continuous-research controller.

Turns a timer-driven discovery rerun into a branch-aware action loop with
durable memory, deduplication, ranked next actions, retries, stopping rules,
and a human-readable change ledger.
"""

from __future__ import annotations

import json
import logging
from hashlib import sha1

from behive.engine.db import connect

log = logging.getLogger(__name__)

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
    if owned:
        db.close()


def _branch_id(mission_id: str, subject_type: str, subject_id: str) -> str:
    raw = f"{mission_id}|{subject_type}|{subject_id}".lower().encode()
    return f"rb_{sha1(raw).hexdigest()[:20]}"


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
    frontiers = db.execute(
        "SELECT id,title,frontier_type,rationale,scores,priority,status FROM hive_frontiers "
        "WHERE mission_id=? AND status NOT IN ('archived','dismissed') ORDER BY priority DESC",
        [mission_id],
    ).fetchall()
    synced = 0
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
    if coverage < .3:
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


def reconcile(mission_id: str, trigger: str = "controller") -> dict:
    """Synchronize branches, record graph changes, and queue ranked actions."""
    db = connect(); ensure_schema(db)
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
    branches = db.execute(
        "SELECT id,subject_id,title,status,reason,uncertainty,evidence_coverage,priority,retry_count,"
        "duplicate_streak,no_progress_cycles FROM hive_research_branches WHERE mission_id=? "
        "AND status IN ('watching','searching','retrieving','extracting','verifying','hypothesis_testing','blocked') "
        "ORDER BY priority DESC", [mission_id]
    ).fetchall()
    queued = 0
    for branch in branches:
        bid, _, title, status, _, uncertainty, coverage, priority, retries, duplicates, stagnant = branch
        if stagnant >= 3 or duplicates >= 3:
            next_state = "exhausted" if stagnant >= 5 else "scheduled_revisit"
            db.execute("UPDATE hive_research_branches SET status=?,next_review_at=NOW()+INTERVAL '7 days',updated_at=NOW() WHERE id=?",
                       [next_state, bid])
            _record_change(db, mission_id, "branch_paused", f"Paused: {title}",
                           "Diminishing returns threshold reached; scheduled for later review.",
                           {"status": status}, {"status": next_state}, .65, bid)
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
    return {"mission_id": mission_id, "branches_synced": synced, "actions_queued": queued,
            "before": before, "after": after}


def run_next_action(mission_id: str, trigger: str = "continuous") -> dict:
    """Claim and execute the highest-value queued action for one mission."""
    reconcile_result = reconcile(mission_id, trigger)
    db = connect(); ensure_schema(db)
    row = db.execute(
        """SELECT a.id,a.branch_id,a.action_type,a.rationale,a.attempt
           FROM hive_research_actions a
           WHERE a.mission_id=? AND a.status IN ('queued','retrying')
             AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=NOW())
           ORDER BY a.score DESC,a.queued_at LIMIT 1 FOR UPDATE SKIP LOCKED""", [mission_id]
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
        if action_type == "refresh_evidence_map":
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
        db.execute("UPDATE hive_research_branches SET status='watching',no_progress_cycles=?,duplicate_streak=?,"
                   "retry_count=0,updated_at=NOW() WHERE id=?",
                   [0 if progress else 1, 0 if progress else 1, branch_id])
        _record_change(db, mission_id, "action_complete", f"{action_type.replace('_',' ').title()}: {title}",
                       f"{progress} new graph objects; {rationale}", before, after, .75 if progress else .4, branch_id)
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


def get_controller_state(mission_id: str) -> dict:
    ensure_schema(); reconcile(mission_id, "dashboard")
    db = connect()
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
    ).fetchall(); db.close()
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
    }
