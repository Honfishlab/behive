"""Finding-to-question discovery paths with bounded autonomous activation."""

from __future__ import annotations

import json
import re
from hashlib import sha1

from behive.engine.db import connect

PATH_TYPES = ("mechanism", "boundary", "modifier", "contradiction", "translation",
              "combination", "measurement", "missing_link", "replication")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_discovery_paths (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, project_id VARCHAR NOT NULL,
 finding_id BIGINT NOT NULL, claim_id BIGINT, source_question_id VARCHAR,
 child_question_id VARCHAR, path_type VARCHAR NOT NULL, question TEXT NOT NULL,
 rationale TEXT NOT NULL, status VARCHAR NOT NULL DEFAULT 'proposed',
 uncertainty DOUBLE PRECISION DEFAULT .5, impact DOUBLE PRECISION DEFAULT .5,
 connection_value DOUBLE PRECISION DEFAULT .5, testability DOUBLE PRECISION DEFAULT .5,
 source_availability DOUBLE PRECISION DEFAULT .5, cost DOUBLE PRECISION DEFAULT .5,
 duplication DOUBLE PRECISION DEFAULT 0, discovery_score DOUBLE PRECISION DEFAULT .5,
 competing_explanations JSONB DEFAULT '[]'::jsonb,
 research_plan JSONB DEFAULT '{}'::jsonb, outcome JSONB DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),
 activated_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
 UNIQUE(project_id,finding_id,path_type)
);
CREATE INDEX IF NOT EXISTS idx_discovery_paths_rank
 ON hive_discovery_paths(project_id,status,discovery_score DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_paths_question
 ON hive_discovery_paths(child_question_id);
"""


def ensure_schema(db=None) -> None:
    owned = db is None; db = db or connect()
    for statement in (part.strip() for part in SCHEMA.split(";") if part.strip()):
        db.execute(statement)
    if owned: db.close()


def _words(text: str) -> set[str]:
    stop = {"that","this","with","from","into","through","between","which","their",
            "have","has","does","what","when","where","under","finding","effect"}
    return {w for w in re.findall(r"[a-z0-9-]{4,}", (text or "").lower()) if w not in stop}


def _path_specs(claim: str, index: int) -> list[tuple[str, str, str]]:
    claim = claim.strip().rstrip(".")
    primary = ("mechanism",
        f"What mechanisms, intermediates, and conditions could explain the finding that {claim}?",
        "Tests causal structure instead of treating the reported association as a finished answer.")
    rotations = [
        ("boundary", f"Under what populations, contexts, doses, or time scales does the finding fail or reverse: {claim}?",
         "Locates the limits of generalization and conditions hidden by an aggregate result."),
        ("contradiction", f"What credible observations contradict or offer an alternative explanation for: {claim}?",
         "Actively searches for disconfirming evidence and confounding explanations."),
        ("translation", f"How well does the finding translate across models, tissues, species, or human populations: {claim}?",
         "Separates direct evidence from untested translation between research contexts."),
        ("measurement", f"Could measurement choice, sampling, or analysis method account for the finding: {claim}?",
         "Tests whether the observation is robust to how it was produced."),
        ("replication", f"Which independent groups or authoritative datasets replicate the finding: {claim}?",
         "Measures independence and reproducibility rather than citation volume."),
    ]
    return [primary, rotations[index % len(rotations)]]


def _plan(path_type: str, question: str, claim: str) -> dict:
    terms = sorted(_words(claim), key=len, reverse=True)[:6]
    return {
        "objective": question,
        "queries": [" ".join(terms[:4] + [suffix]) for suffix in
                    ("mechanism primary study", "systematic review", "contradictory results")],
        "required_source_types": ["primary research", "systematic review", "authoritative dataset"],
        "minimum_independent_sources": 3,
        "support_criterion": "At least two independent sources with directly comparable observations.",
        "refutation_criterion": "A credible direct observation under comparable conditions that reverses or nullifies the claim.",
        "completion_criterion": "Three independent sources, one primary record, and an explicit boundary or mechanism.",
        "abandonment_criterion": "Pause after three searches add no new direct evidence or only duplicate sources.",
        "revisit_when": "A new primary study, dataset, contradiction, or materially different measurement appears.",
        "max_attempts": 3, "path_type": path_type,
    }


def _explanations(claim: str) -> list[dict]:
    subject = claim.strip().rstrip(".")
    return [
        {"type":"primary", "statement":f"The reported relationship is causal: {subject}.", "weight":.4},
        {"type":"alternative", "statement":"A correlated upstream process explains both variables.", "weight":.25},
        {"type":"context", "statement":"The effect is real only under the reported biological or experimental conditions.", "weight":.2},
        {"type":"artifact", "statement":"Measurement, selection, or publication effects inflate the relationship.", "weight":.15},
    ]


def generate_paths(mission_id: str, project_id: str | None = None, limit: int = 12) -> dict:
    """Create two traceable, typed discovery paths per finding, within a hard cap."""
    db = connect(); ensure_schema(db)
    if not project_id:
        row = db.execute("SELECT project_id FROM hive_missions WHERE id=?", [mission_id]).fetchone()
        project_id = row[0] if row else None
    if not project_id:
        db.close(); return {"generated":0, "reason":"Mission is not linked to a project"}
    rows = db.execute(
        "SELECT f.id,f.claim_id,f.question_id,f.summary,f.confidence,f.novelty "
        "FROM hive_findings f WHERE f.project_id=? AND f.status NOT IN ('contradicted','superseded') "
        "ORDER BY COALESCE(f.novelty,0) DESC,COALESCE(f.confidence,0) DESC LIMIT ?",
        [project_id, max(1, limit)],
    ).fetchall()
    all_terms = [_words(r[3]) for r in rows]
    generated = 0
    for index, (finding_id, claim_id, question_id, claim, confidence, novelty) in enumerate(rows):
        overlap = max((len(all_terms[index] & terms) / max(1, len(all_terms[index] | terms))
                       for j, terms in enumerate(all_terms) if j != index), default=0)
        for path_type, question, rationale in _path_specs(claim, index):
            uncertainty = max(.2, 1-float(confidence or .5))
            impact = min(1, .55 + float(novelty or .5)*.35)
            connection = min(1, .35 + overlap)
            testability = .82 if path_type in ("mechanism","boundary","measurement","replication") else .7
            availability = .78 if path_type in ("replication","contradiction") else .65
            cost = .35 if path_type in ("replication","measurement") else .5
            duplication = overlap*.45
            score = round(.23*uncertainty+.22*impact+.2*connection+.17*testability+
                          .13*availability-.03*cost-.12*duplication, 4)
            path_id = "dp_" + sha1(f"{project_id}|{finding_id}|{path_type}".encode()).hexdigest()[:18]
            result = db.execute(
                "INSERT INTO hive_discovery_paths (id,mission_id,project_id,finding_id,claim_id,source_question_id,"
                "path_type,question,rationale,uncertainty,impact,connection_value,testability,source_availability,"
                "cost,duplication,discovery_score,competing_explanations,research_plan) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?::jsonb) "
                "ON CONFLICT (project_id,finding_id,path_type) DO UPDATE SET rationale=EXCLUDED.rationale,"
                "discovery_score=EXCLUDED.discovery_score,research_plan=EXCLUDED.research_plan,updated_at=NOW() "
                "RETURNING id",
                [path_id,mission_id,project_id,finding_id,claim_id,question_id,path_type,question,rationale,
                 uncertainty,impact,connection,testability,availability,cost,duplication,score,
                 json.dumps(_explanations(claim)),json.dumps(_plan(path_type,question,claim))],
            ).fetchone()
            generated += bool(result)
    db.commit(); db.close()
    return {"mission_id":mission_id,"project_id":project_id,"generated":generated}


def activate_path(path_id: str) -> dict:
    """Turn a proposed path into a question-tree branch exactly once."""
    db = connect(); ensure_schema(db)
    row = db.execute(
        "SELECT project_id,source_question_id,question,path_type,status,child_question_id "
        "FROM hive_discovery_paths WHERE id=?", [path_id]
    ).fetchone()
    if not row:
        db.close(); raise ValueError("Discovery path not found")
    project_id,parent_id,question,path_type,status,child_id = row
    if child_id:
        db.close(); return {"id":path_id,"status":status,"question_id":child_id,"reused":True}
    child_id = "question_" + sha1(f"discovery-path|{path_id}".encode()).hexdigest()[:16]
    depth = db.execute("SELECT COALESCE(depth,0)+1 FROM hive_questions WHERE id=?", [parent_id]).fetchone()
    depth = int(depth[0]) if depth else 1
    db.execute(
        "INSERT INTO hive_questions (id,project_id,parent_id,question,depth,kind,priority,status) "
        "VALUES (?,?,?,?,?,?,.9,'open') ON CONFLICT (id) DO NOTHING",
        [child_id,project_id,parent_id,question,depth,f"discovery path:{path_type}"],
    )
    db.execute("UPDATE hive_discovery_paths SET child_question_id=?,status='queued',activated_at=NOW(),updated_at=NOW() WHERE id=?",
               [child_id,path_id])
    db.commit(); db.close()
    return {"id":path_id,"status":"queued","question_id":child_id}


def activate_ranked(mission_id: str, limit: int = 3, require_continuous: bool = True) -> dict:
    db = connect(); ensure_schema(db)
    if require_continuous:
        policy = db.execute(
            "SELECT p.agent_policy FROM hive_projects p JOIN hive_missions m ON m.project_id=p.id WHERE m.id=?",
            [mission_id],
        ).fetchone()
        policy = policy[0] if policy and policy[0] else {}
        if policy.get("autonomy") != "continuous" or policy.get("status") != "running":
            db.close(); return {"activated":0,"reason":"Continuous policy is paused"}
    rows = db.execute(
        "SELECT id FROM hive_discovery_paths WHERE mission_id=? AND status='proposed' "
        "AND discovery_score>=.45 ORDER BY discovery_score DESC,created_at LIMIT ?",
        [mission_id, max(1, min(limit, 10))],
    ).fetchall(); db.close()
    activated = [activate_path(row[0]) for row in rows]
    return {"activated":len(activated),"paths":activated}


def sync_path_states(project_id: str) -> int:
    db = connect(); ensure_schema(db)
    db.execute(
        "UPDATE hive_discovery_paths p SET status=CASE q.status "
        "WHEN 'researching' THEN 'running' WHEN 'covered' THEN 'resolved' "
        "WHEN 'answered' THEN 'resolved' WHEN 'evidence_found' THEN 'evidence_found' "
        "WHEN 'blocked' THEN 'blocked' ELSE p.status END,"
        "completed_at=CASE WHEN q.status IN ('covered','answered') THEN NOW() ELSE p.completed_at END,updated_at=NOW() "
        "FROM hive_questions q WHERE p.project_id=? AND p.child_question_id=q.id",
        [project_id],
    )
    db.commit(); db.close(); return 1


def get_paths(mission_id: str) -> dict:
    db = connect(); ensure_schema(db)
    mission = db.execute("SELECT project_id FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    project_id = mission[0] if mission else None
    if project_id: sync_path_states(project_id)
    rows = db.execute(
        "SELECT p.id,p.finding_id,p.claim_id,p.source_question_id,p.child_question_id,p.path_type,p.question,"
        "p.rationale,p.status,p.uncertainty,p.impact,p.connection_value,p.testability,p.source_availability,"
        "p.cost,p.duplication,p.discovery_score,p.competing_explanations,p.research_plan,p.outcome,"
        "p.created_at,p.activated_at,p.completed_at,f.summary,COALESCE(f.source_url,c.source_url) "
        "FROM hive_discovery_paths p LEFT JOIN hive_findings f ON f.id=p.finding_id "
        "LEFT JOIN hive_claims c ON c.id=p.claim_id WHERE p.project_id=? ORDER BY "
        "CASE p.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'proposed' THEN 2 ELSE 3 END,"
        "p.discovery_score DESC", [project_id]
    ).fetchall() if project_id else []
    db.close()
    keys = ("id","finding_id","claim_id","source_question_id","child_question_id","path_type","question",
            "rationale","status","uncertainty","impact","connection_value","testability","source_availability",
            "cost","duplication","discovery_score","competing_explanations","research_plan","outcome",
            "created_at","activated_at","completed_at","finding_summary","source_url")
    paths = [dict(zip(keys,row)) for row in rows]
    counts = {}
    for path in paths: counts[path["status"]] = counts.get(path["status"],0)+1
    return {"mission_id":mission_id,"project_id":project_id,"paths":paths,
            "metrics":{"total":len(paths),"statuses":counts,"active":sum(counts.get(s,0) for s in ('queued','running'))}}


def control_path(path_id: str, action: str) -> dict:
    if action == "activate": return activate_path(path_id)
    states = {"defer":"deferred","resume":"proposed","resolve":"resolved"}
    if action not in states: raise ValueError("Unsupported discovery path action")
    db = connect(); ensure_schema(db)
    db.execute("UPDATE hive_discovery_paths SET status=?,updated_at=NOW(),completed_at=CASE WHEN ?='resolved' THEN NOW() ELSE completed_at END WHERE id=?",
               [states[action],states[action],path_id])
    db.commit(); db.close()
    return {"id":path_id,"status":states[action]}
