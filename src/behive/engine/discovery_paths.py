"""Finding-to-question discovery paths with bounded autonomous activation."""

from __future__ import annotations

import json
import re
from hashlib import sha1

from behive.engine.db import connect

PATH_TYPES = (
    "adjacent_system", "downstream_consequence", "cross_domain", "intervention_opportunity",
    "convergence", "scale_shift", "emerging_signal", "population_context",
    "unexplained_link", "practical_direction", "mechanism", "boundary", "modifier",
    "contradiction", "translation", "combination", "measurement", "missing_link", "replication",
)
MAX_ACTIVE_PATHS = 6
EXPLORATION_STRATEGY_VERSION = 2
NARROW_REVIEW_TYPES = ("mechanism", "boundary", "contradiction", "translation", "measurement", "replication")

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


def _topic_label(topic: str) -> str:
    value = " ".join((topic or "the main research topic").strip().rstrip("?").split())
    value = re.sub(r"^(?:how|what|which|why|when|where)\s+(?:does|do|did|is|are|can|could|might|will|would)\s+",
                   "", value, flags=re.IGNORECASE)
    return value[:220]


def _path_specs(claim: str, index: int, topic: str = "") -> list[tuple[str, str, str]]:
    """Fan findings outward into a diverse frontier portfolio.

    Validation still happens inside the evidence pipeline. Discovery paths are
    deliberately optimized for reach: neighboring systems, consequences,
    combinations, translations, and opportunities that a curious researcher
    would inspect before narrowing back down.
    """
    claim = claim.strip().rstrip(".")
    subject = _topic_label(topic)
    observation = f'“{claim}”'
    frontiers = [
        ("adjacent_system", f"Which neighboring biological systems or processes could connect the observation {observation} to unexplored parts of {subject}?",
         "Searches one conceptual step beyond the finding for overlooked biological neighbors."),
        ("downstream_consequence", f"What downstream consequences could follow from the observation {observation}, and which might matter most for {subject}?",
         "Follows the finding forward into effects that the current literature may not connect explicitly."),
        ("cross_domain", f"What ideas from adjacent fields could open new ways to understand or use the observation {observation}?",
         "Imports useful models from neighboring disciplines instead of staying inside one literature silo."),
        ("intervention_opportunity", f"What compounds, behaviors, technologies, or environmental changes could influence the pathway implied by the observation {observation}?",
         "Turns the finding into a broad opportunity search without assuming one preferred intervention."),
        ("convergence", f"Where might the observation {observation} converge with other mechanisms, exposures, or risk factors in {subject} to create unexpected combined effects?",
         "Looks for multi-factor interactions that isolated studies commonly miss."),
        ("scale_shift", f"How could the observation {observation} connect molecular events to cells, tissues, whole-body function, or lived outcomes in {subject}?",
         "Moves across levels of explanation to find missing links between laboratory and real-world observations."),
        ("emerging_signal", f"What emerging datasets, technologies, or research communities point to new directions related to the observation {observation}?",
         "Scans the edge of the field for weak signals and newly possible lines of inquiry."),
        ("population_context", f"Which populations, life stages, disease stages, or physiological states could make the observation {observation} especially consequential for {subject}?",
         "Broadens the people and contexts considered rather than optimizing only for an average effect."),
        ("unexplained_link", f"What apparently separate findings about {subject} become connected if the observation {observation} is treated as a bridge?",
         "Uses the finding to join disconnected islands in the evidence graph."),
        ("practical_direction", f"What new prevention, diagnostic, monitoring, or treatment directions become plausible if the observation {observation} is important?",
         "Explores practical possibilities while keeping them explicitly provisional."),
    ]
    first = (index * 2) % len(frontiers)
    return [frontiers[first], frontiers[(first + 1) % len(frontiers)]]


def _plan(path_type: str, question: str, claim: str, topic: str = "") -> dict:
    terms = sorted(_words(claim), key=len, reverse=True)[:6]
    topic_terms = sorted(_words(topic), key=len, reverse=True)[:3]
    query_terms = list(dict.fromkeys(topic_terms + terms[:4]))
    suffixes = {
        "adjacent_system": ("pathway network review", "related processes emerging research", "systems biology dataset"),
        "downstream_consequence": ("downstream outcomes", "longitudinal observations", "clinical implications"),
        "cross_domain": ("cross disciplinary analogy", "shared mechanism other fields", "novel conceptual model"),
        "intervention_opportunity": ("interventions compounds technology", "modifiable factors", "therapeutic opportunities"),
        "convergence": ("combined mechanisms interaction", "multi factor model", "synergy comorbidity exposure"),
        "scale_shift": ("molecular cellular organism outcomes", "translational bridge", "multiscale model"),
        "emerging_signal": ("emerging research dataset", "preprint conference patent", "new technology measurement"),
        "population_context": ("population subgroup life stage", "disease stage phenotype", "precision medicine cohort"),
        "unexplained_link": ("knowledge graph connection", "shared pathway disconnected findings", "latent relationship"),
        "practical_direction": ("prevention diagnosis monitoring treatment", "clinical development", "real world application"),
    }.get(path_type, ("primary study", "systematic review", "authoritative dataset"))
    return {
        "objective": question,
        "queries": [" ".join(query_terms + [suffix]) for suffix in suffixes],
        "required_source_types": ["primary research", "authoritative dataset", "high-quality synthesis", "adjacent-field literature"],
        "minimum_independent_sources": 2,
        "support_criterion": "Two credible observations establish a meaningful connection worth mapping or deepening.",
        "refutation_criterion": "Evidence shows the proposed connection is biologically or contextually incompatible.",
        "completion_criterion": "Map at least one evidence-linked connection, consequence, opportunity, or explicit unknown.",
        "abandonment_criterion": "Pause after three distinct search angles produce only duplicates or no credible connection.",
        "revisit_when": "A new dataset, technology, neighboring mechanism, population signal, or intervention appears.",
        "max_attempts": 3, "path_type": path_type,
        "strategy": "frontier_fanout", "strategy_version": EXPLORATION_STRATEGY_VERSION,
    }


def _explanations(claim: str) -> list[dict]:
    subject = claim.strip().rstrip(".")
    return [
        {"type":"network", "statement":f"The observation may be one node in a wider biological network: {subject}.", "weight":.3},
        {"type":"downstream", "statement":"The most important discovery may be a consequence several steps beyond the measured effect.", "weight":.25},
        {"type":"context", "statement":"A population, life stage, exposure, or disease stage may reveal a stronger connection.", "weight":.25},
        {"type":"opportunity", "statement":"A new measurement or intervention may make a previously hidden relationship observable.", "weight":.2},
    ]


def retire_unstarted_narrow_paths(project_id: str) -> dict:
    """Archive v1 review-style paths only when no research mission started.

    This preserves completed/in-flight evidence while removing repetitive queued
    questions that would otherwise crowd out the new frontier portfolio.
    """
    db = connect(); ensure_schema(db)
    rows = db.execute(
        "SELECT p.id,p.child_question_id FROM hive_discovery_paths p "
        "LEFT JOIN hive_questions q ON q.id=p.child_question_id "
        "WHERE p.project_id=? AND p.path_type=ANY(?) AND p.status IN ('proposed','queued') "
        "AND COALESCE((p.research_plan->>'strategy_version')::integer,1) < ? "
        "AND (p.child_question_id IS NULL OR NOT EXISTS (SELECT 1 FROM hive_missions m WHERE m.question_id=p.child_question_id))",
        [project_id, list(NARROW_REVIEW_TYPES), EXPLORATION_STRATEGY_VERSION],
    ).fetchall()
    path_ids = [row[0] for row in rows]
    question_ids = [row[1] for row in rows if row[1]]
    if path_ids:
        db.execute("UPDATE hive_discovery_paths SET status='deferred',outcome=?::jsonb,updated_at=NOW() WHERE id=ANY(?)",
                   [json.dumps({"reason":"superseded by frontier-fanout strategy","strategy_version":2}), path_ids])
    if question_ids:
        db.execute("UPDATE hive_questions SET status='archived',updated_at=NOW() WHERE id=ANY(?) AND status IN ('open','queued','paused','evidence_boundary')",
                   [question_ids])
        db.execute(
            "UPDATE hive_research_actions a SET status='superseded',completed_at=NOW(),error_message='Superseded by frontier-fanout discovery strategy' "
            "FROM hive_research_branches b WHERE a.branch_id=b.id AND b.subject_id=ANY(?) "
            "AND a.status IN ('queued','retrying')", [question_ids],
        )
        db.execute("UPDATE hive_research_branches SET status='satisfied',updated_at=NOW() WHERE subject_id=ANY(?)", [question_ids])
    db.commit(); db.close()
    return {"paths_deferred": len(path_ids), "questions_archived": len(question_ids)}


def generate_paths(mission_id: str, project_id: str | None = None, limit: int = 12) -> dict:
    """Create two traceable, typed discovery paths per finding, within a hard cap."""
    db = connect(); ensure_schema(db)
    if not project_id:
        row = db.execute("SELECT project_id FROM hive_missions WHERE id=?", [mission_id]).fetchone()
        project_id = row[0] if row else None
    if not project_id:
        db.close(); return {"generated":0, "reason":"Mission is not linked to a project"}
    db.close()
    retired = retire_unstarted_narrow_paths(project_id)
    db = connect(); ensure_schema(db)
    topic_row = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    topic = topic_row[0] if topic_row else ""
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
        for path_type, question, rationale in _path_specs(claim, index, topic):
            uncertainty = max(.2, 1-float(confidence or .5))
            impact = min(1, .55 + float(novelty or .5)*.35)
            connection = min(1, .35 + overlap)
            testability = .68
            availability = .72 if path_type in ("adjacent_system","cross_domain","emerging_signal") else .65
            cost = .42 if path_type in ("emerging_signal","unexplained_link") else .5
            duplication = overlap*.45
            score = round(.16*uncertainty+.25*impact+.29*connection+.1*testability+
                          .13*availability-.03*cost-.10*duplication, 4)
            path_id = "dp_" + sha1(f"{project_id}|{finding_id}|{path_type}".encode()).hexdigest()[:18]
            result = db.execute(
                "INSERT INTO hive_discovery_paths (id,mission_id,project_id,finding_id,claim_id,source_question_id,"
                "path_type,question,rationale,uncertainty,impact,connection_value,testability,source_availability,"
                "cost,duplication,discovery_score,competing_explanations,research_plan) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?::jsonb) "
                "ON CONFLICT (project_id,finding_id,path_type) DO UPDATE SET question=EXCLUDED.question,"
                "rationale=EXCLUDED.rationale,discovery_score=EXCLUDED.discovery_score,"
                "competing_explanations=EXCLUDED.competing_explanations,"
                "research_plan=EXCLUDED.research_plan,updated_at=NOW() "
                "RETURNING id",
                [path_id,mission_id,project_id,finding_id,claim_id,question_id,path_type,question,rationale,
                 uncertainty,impact,connection,testability,availability,cost,duplication,score,
                 json.dumps(_explanations(claim)),json.dumps(_plan(path_type,question,claim,topic))],
            ).fetchone()
            generated += bool(result)
    db.commit(); db.close()
    return {"mission_id":mission_id,"project_id":project_id,"generated":generated,"retired":retired}


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
    active = db.execute(
        "SELECT COUNT(*) FROM hive_discovery_paths WHERE mission_id=? AND status IN ('queued','running')",
        [mission_id],
    ).fetchone()[0]
    available = max(0, min(max(1, limit), MAX_ACTIVE_PATHS) - int(active or 0))
    if not available:
        db.close(); return {"activated":0,"reason":"Active discovery-path limit reached","active":int(active or 0)}
    rows = db.execute(
        "SELECT id FROM hive_discovery_paths WHERE mission_id=? AND status='proposed' "
        "AND discovery_score>=.45 ORDER BY discovery_score DESC,created_at LIMIT ?",
        [mission_id, available],
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
