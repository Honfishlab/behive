"""Open-world discovery engine for probes, unknowns, and hypothesis portfolios."""

from __future__ import annotations

import json
import logging
import re
from hashlib import sha1

from behive.engine.db import connect
from behive.engine.llm import complete

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_discovery_cycles (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, cycle_number INTEGER NOT NULL,
 status VARCHAR NOT NULL DEFAULT 'running', trigger VARCHAR DEFAULT 'pipeline',
 observations_added INTEGER DEFAULT 0, unknowns_added INTEGER DEFAULT 0,
 hypotheses_added INTEGER DEFAULT 0, summary JSONB DEFAULT '{}'::jsonb,
 started_at TIMESTAMP DEFAULT NOW(), completed_at TIMESTAMP,
 UNIQUE(mission_id,cycle_number)
);
CREATE TABLE IF NOT EXISTS hive_discovery_nodes (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, label VARCHAR NOT NULL,
 node_type VARCHAR NOT NULL, description TEXT, epistemic_state VARCHAR NOT NULL,
 context JSONB DEFAULT '{}'::jsonb, novelty DOUBLE PRECISION DEFAULT 0.5,
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,label,node_type)
);
CREATE TABLE IF NOT EXISTS hive_discovery_edges (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, source_node_id VARCHAR NOT NULL,
 target_node_id VARCHAR NOT NULL, relation VARCHAR NOT NULL,
 epistemic_state VARCHAR NOT NULL, confidence DOUBLE PRECISION DEFAULT 0,
 evidence_url TEXT, evidence_excerpt TEXT, conditions JSONB DEFAULT '{}'::jsonb,
 cycle_id INTEGER, created_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,source_node_id,target_node_id,relation,epistemic_state,evidence_url)
);
CREATE TABLE IF NOT EXISTS hive_frontiers (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, title TEXT NOT NULL,
 frontier_type VARCHAR NOT NULL, rationale TEXT, source_node_id VARCHAR,
 target_node_id VARCHAR, absence_type VARCHAR, status VARCHAR DEFAULT 'open',
 evidence_search JSONB DEFAULT '{}'::jsonb, scores JSONB DEFAULT '{}'::jsonb,
 priority DOUBLE PRECISION DEFAULT 0.5, cycle_id INTEGER,
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,title)
);
CREATE TABLE IF NOT EXISTS hive_hypothesis_paths (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, name TEXT NOT NULL,
 hypothesis_type VARCHAR NOT NULL, statement TEXT NOT NULL, reasoning TEXT,
 path_nodes JSONB DEFAULT '[]'::jsonb, supporting_edges JSONB DEFAULT '[]'::jsonb,
 missing_links JSONB DEFAULT '[]'::jsonb, assumptions JSONB DEFAULT '[]'::jsonb,
 predictions JSONB DEFAULT '[]'::jsonb, discriminators JSONB DEFAULT '[]'::jsonb,
 safety_risks JSONB DEFAULT '[]'::jsonb, scores JSONB DEFAULT '{}'::jsonb,
 status VARCHAR DEFAULT 'proposed', cycle_id INTEGER,
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,name)
);
CREATE INDEX IF NOT EXISTS idx_discovery_nodes_mission ON hive_discovery_nodes(mission_id);
CREATE INDEX IF NOT EXISTS idx_discovery_edges_mission ON hive_discovery_edges(mission_id);
CREATE INDEX IF NOT EXISTS idx_frontiers_mission_priority ON hive_frontiers(mission_id,priority DESC);
CREATE INDEX IF NOT EXISTS idx_hypotheses_mission ON hive_hypothesis_paths(mission_id);
"""


def ensure_schema(db=None) -> None:
    owned = db is None
    db = db or connect()
    for statement in (part.strip() for part in SCHEMA.split(";") if part.strip()):
        db.execute(statement)
    if owned:
        db.close()


def _id(prefix: str, *values: str) -> str:
    return f"{prefix}_{sha1('|'.join(values).lower().encode()).hexdigest()[:18]}"


def _json(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(match.group(0)) if match else {}
    return parsed if isinstance(parsed, dict) else {}


def _node(db, mission_id: str, label: str, node_type: str, state: str,
          description: str = "", context: dict | None = None, novelty: float = 0.5) -> str:
    label = re.sub(r"\s+", " ", label or "").strip()[:300]
    node_type = (node_type or "concept").lower()[:60]
    state = (state or "observed").lower()[:40]
    node_id = _id("dn", mission_id, label, node_type)
    db.execute(
        """INSERT INTO hive_discovery_nodes
           (id,mission_id,label,node_type,description,epistemic_state,context,novelty)
           VALUES (?,?,?,?,?,?,?::jsonb,?) ON CONFLICT (id) DO UPDATE SET
           description=EXCLUDED.description,epistemic_state=EXCLUDED.epistemic_state,
           context=EXCLUDED.context,novelty=GREATEST(hive_discovery_nodes.novelty,EXCLUDED.novelty),updated_at=NOW()""",
        [node_id, mission_id, label, node_type, description[:2000], state,
         json.dumps(context or {}), max(0, min(1, float(novelty or 0.5)))],
    )
    return node_id


def _extract_batch(topic: str, documents: list[tuple]) -> dict:
    corpus = "\n\n".join(
        f"DOCUMENT {i+1}\nURL: {d[0]}\nTITLE: {d[1] or ''}\nMETHOD: {d[3]}\nTEXT: {(d[2] or '')[:6500]}"
        for i, d in enumerate(documents)
    )
    prompt = f"""EXPLORATORY PROBE
{topic}

Map this evidence without answering the probe and without writing a conclusion. Separate observations from
interpretations. Extract concepts, measured observations, experimental conditions, boundaries, contradictions,
and explicit limitations. Do not invent a relationship. A relationship is observed only when its supporting text
states or measures it. Return JSON with:
- nodes: array of label, node_type (compound,receptor,pathway,cell_type,disease,phenotype,process,measurement,
  condition,observation), description, epistemic_state (observed or reported), novelty 0-1
- edges: array of source, target, relation, epistemic_state (observed,reported,contradicted), confidence 0-1,
  document_index, evidence_excerpt, conditions object
- boundaries: array of concise limitations or contexts that block generalization
Use each document only for what it directly supports. JSON only.

{corpus}"""
    return _json(complete(prompt, stage="process", system="You are an evidence cartographer, not a question-answering assistant.",
                          max_tokens=5000, temperature=0.1, json_mode=True))


def _save_evidence_map(db, mission_id: str, cycle_id: int, documents: list[tuple], data: dict) -> tuple[int, int]:
    node_map, added_nodes, added_edges = {}, 0, 0
    for raw in data.get("nodes", []):
        if not isinstance(raw, dict) or not raw.get("label"):
            continue
        node_id = _node(db, mission_id, raw["label"], raw.get("node_type", "concept"),
                        raw.get("epistemic_state", "observed"), raw.get("description", ""),
                        novelty=raw.get("novelty", 0.4))
        node_map[raw["label"].strip().lower()] = node_id
        added_nodes += 1
    for raw in data.get("edges", []):
        if not isinstance(raw, dict) or not raw.get("source") or not raw.get("target"):
            continue
        source = node_map.get(str(raw["source"]).strip().lower()) or _node(
            db, mission_id, str(raw["source"]), "concept", "reported")
        target = node_map.get(str(raw["target"]).strip().lower()) or _node(
            db, mission_id, str(raw["target"]), "concept", "reported")
        try:
            doc_index = int(raw.get("document_index", 0)) - 1
        except (TypeError, ValueError):
            doc_index = -1
        evidence_url = documents[doc_index][0] if 0 <= doc_index < len(documents) else ""
        relation = str(raw.get("relation") or "related_to")[:120]
        state = str(raw.get("epistemic_state") or "reported").lower()[:40]
        edge_id = _id("de", mission_id, source, target, relation, state, evidence_url)
        db.execute(
            """INSERT INTO hive_discovery_edges
               (id,mission_id,source_node_id,target_node_id,relation,epistemic_state,confidence,
                evidence_url,evidence_excerpt,conditions,cycle_id)
               VALUES (?,?,?,?,?,?,?,?,?,?::jsonb,?) ON CONFLICT (id) DO NOTHING""",
            [edge_id, mission_id, source, target, relation, state,
             max(0, min(1, float(raw.get("confidence", 0.5) or 0.5))), evidence_url,
             str(raw.get("evidence_excerpt") or "")[:1200], json.dumps(raw.get("conditions") or {}), cycle_id],
        )
        added_edges += 1
    return added_nodes, added_edges


def _generate_frontiers(topic: str, nodes: list[tuple], edges: list[tuple], boundaries: list[str]) -> dict:
    node_text = "\n".join(f"N{i+1}: {n[1]} [{n[2]}; {n[3]}]" for i, n in enumerate(nodes[:120]))
    edge_text = "\n".join(f"{e[1]} --{e[3]}/{e[4]}--> {e[2]}" for e in edges[:180])
    prompt = f"""EXPLORATORY PROBE
{topic}

KNOWN EVIDENCE GRAPH NODES
{node_text}

OBSERVED/REPORTED EDGES
{edge_text}

GENERALIZATION BOUNDARIES
{json.dumps(boundaries[:40])}

Do not answer the probe. Re-dissect this knowledge landscape to identify what is not connected, not measured,
contradictory, terminology-separated, population-limited, combination-untested, or translation-limited.
Generate a diverse portfolio, including direct-benefit, indirect, context-dependent, null-transfer, combination,
and harm hypotheses when scientifically applicable. Hypotheses are possibilities, never findings.

Return JSON with:
- frontiers: array of title, frontier_type (bridge,analogy,contradiction,boundary,missing_link,measurement_gap),
  rationale, source_label, target_label, absence_type (search_absence,literature_absence,measurement_absence,
  population_gap,combination_gap,translation_gap,terminology_gap,contradiction_gap), search_strategy,
  scores object containing novelty,explanatory_reach,testability,impact,safety_risk,evidence_strength each 0-1
- hypotheses: array of name, hypothesis_type, statement, reasoning, path_nodes labels, supporting_relationships,
  missing_links, assumptions, predictions, discriminators, safety_risks, scores with the same dimensions
Produce 8-14 frontiers and 5-8 competing hypotheses. Each prediction must be observable and each discriminator
must distinguish at least two hypotheses. JSON only."""
    return _json(complete(prompt, stage="synth", system="You are a frontier director for open-world scientific discovery.",
                          max_tokens=7000, temperature=0.35, json_mode=True))


def _priority(scores: dict) -> float:
    get = lambda key, default=.5: max(0, min(1, float(scores.get(key, default) or default)))
    return round(.22*get("novelty") + .2*get("explanatory_reach") + .2*get("testability") +
                 .2*get("impact") + .1*(1-get("safety_risk")) + .08*(1-get("evidence_strength")), 4)


def run_frontier_cycle(mission_id: str, trigger: str = "pipeline") -> dict:
    """Build or extend a discovery map and rank the next uncertainties to attack."""
    db = connect()
    ensure_schema(db)
    topic_row = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    if not topic_row:
        db.close()
        raise ValueError(f"Mission {mission_id} not found")
    topic = topic_row[0]
    cycle_number = db.execute(
        "SELECT COALESCE(MAX(cycle_number),0)+1 FROM hive_discovery_cycles WHERE mission_id=?", [mission_id]
    ).fetchone()[0]
    cycle_id = db.execute(
        "INSERT INTO hive_discovery_cycles (mission_id,cycle_number,trigger) VALUES (?,?,?) RETURNING id",
        [mission_id, cycle_number, trigger],
    ).fetchone()[0]
    try:
        documents = db.execute(
            """SELECT c.url,c.title,c.raw_text,c.harvest_method
               FROM hive_content c JOIN hive_sources s ON s.mission_id=c.mission_id AND s.url=c.url
               WHERE c.mission_id=? AND c.word_count>=80 AND COALESCE(c.harvest_method,'')<>'snippet_fallback'
               ORDER BY COALESCE(s.evidence_tier,9),c.quality_score DESC,c.word_count DESC LIMIT 32""",
            [mission_id],
        ).fetchall()
        if not documents:
            raise RuntimeError("No analysis-ready documents for discovery mapping")
        observations = edges_added = 0
        boundaries: list[str] = []
        for offset in range(0, len(documents), 6):
            batch = documents[offset:offset+6]
            mapped = _extract_batch(topic, batch)
            n, e = _save_evidence_map(db, mission_id, cycle_id, batch, mapped)
            observations += n
            edges_added += e
            boundaries.extend(str(x)[:800] for x in mapped.get("boundaries", []) if x)
        nodes = db.execute(
            "SELECT id,label,node_type,epistemic_state FROM hive_discovery_nodes WHERE mission_id=? ORDER BY novelty DESC",
            [mission_id],
        ).fetchall()
        edges = db.execute(
            """SELECT e.id,s.label,t.label,e.relation,e.epistemic_state FROM hive_discovery_edges e
               JOIN hive_discovery_nodes s ON s.id=e.source_node_id
               JOIN hive_discovery_nodes t ON t.id=e.target_node_id WHERE e.mission_id=?""", [mission_id]
        ).fetchall()
        portfolio = _generate_frontiers(topic, nodes, edges, boundaries)
        label_ids = {n[1].lower(): n[0] for n in nodes}
        frontiers_added = hypotheses_added = 0
        for raw in portfolio.get("frontiers", []):
            if not isinstance(raw, dict) or not raw.get("title"):
                continue
            scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
            frontier_id = _id("fr", mission_id, str(raw["title"]))
            db.execute(
                """INSERT INTO hive_frontiers
                   (id,mission_id,title,frontier_type,rationale,source_node_id,target_node_id,absence_type,
                    evidence_search,scores,priority,cycle_id) VALUES (?,?,?,?,?,?,?,?,?::jsonb,?::jsonb,?,?)
                   ON CONFLICT (id) DO UPDATE SET rationale=EXCLUDED.rationale,evidence_search=EXCLUDED.evidence_search,
                   scores=EXCLUDED.scores,priority=EXCLUDED.priority,cycle_id=EXCLUDED.cycle_id,updated_at=NOW()""",
                [frontier_id, mission_id, str(raw["title"])[:1000], str(raw.get("frontier_type") or "missing_link")[:60],
                 str(raw.get("rationale") or "")[:3000], label_ids.get(str(raw.get("source_label") or "").lower()),
                 label_ids.get(str(raw.get("target_label") or "").lower()), str(raw.get("absence_type") or "unclassified")[:80],
                 json.dumps(raw.get("search_strategy") or {}), json.dumps(scores), _priority(scores), cycle_id],
            )
            frontiers_added += 1
        for raw in portfolio.get("hypotheses", []):
            if not isinstance(raw, dict) or not raw.get("name") or not raw.get("statement"):
                continue
            hypothesis_id = _id("hp", mission_id, str(raw["name"]))
            scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
            path_nodes = [label_ids.get(str(label).lower(), str(label)) for label in raw.get("path_nodes", [])]
            db.execute(
                """INSERT INTO hive_hypothesis_paths
                   (id,mission_id,name,hypothesis_type,statement,reasoning,path_nodes,supporting_edges,
                    missing_links,assumptions,predictions,discriminators,safety_risks,scores,cycle_id)
                   VALUES (?,?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?)
                   ON CONFLICT (id) DO UPDATE SET statement=EXCLUDED.statement,reasoning=EXCLUDED.reasoning,
                   predictions=EXCLUDED.predictions,discriminators=EXCLUDED.discriminators,scores=EXCLUDED.scores,
                   cycle_id=EXCLUDED.cycle_id,updated_at=NOW()""",
                [hypothesis_id, mission_id, str(raw["name"])[:1000], str(raw.get("hypothesis_type") or "exploratory")[:80],
                 str(raw["statement"])[:3000], str(raw.get("reasoning") or "")[:5000], json.dumps(path_nodes),
                 json.dumps(raw.get("supporting_relationships") or []), json.dumps(raw.get("missing_links") or []),
                 json.dumps(raw.get("assumptions") or []), json.dumps(raw.get("predictions") or []),
                 json.dumps(raw.get("discriminators") or []), json.dumps(raw.get("safety_risks") or []),
                 json.dumps({**scores, "priority": _priority(scores)}), cycle_id],
            )
            hypotheses_added += 1
        summary = {"documents_mapped": len(documents), "nodes_seen": observations,
                   "edges_seen": edges_added, "frontiers_added": frontiers_added,
                   "hypotheses_added": hypotheses_added, "mode": "open_world_discovery"}
        db.execute(
            """UPDATE hive_discovery_cycles SET status='complete',observations_added=?,unknowns_added=?,
               hypotheses_added=?,summary=?::jsonb,completed_at=NOW() WHERE id=?""",
            [observations, frontiers_added, hypotheses_added, json.dumps(summary), cycle_id],
        )
        db.commit()
        try:
            from behive.engine.evolution import evolve_hypotheses
            evolution = evolve_hypotheses(mission_id, trigger=f"discovery_cycle_{cycle_number}")
            summary["evolution"] = evolution
            db.execute("UPDATE hive_discovery_cycles SET summary=?::jsonb WHERE id=?",
                       [json.dumps(summary), cycle_id])
            db.commit()
        except Exception as exc:
            log.warning("Hypothesis evolution failed without invalidating discovery cycle: %s", exc)
            summary["evolution_error"] = str(exc)[:500]
        return {"mission_id": mission_id, "cycle_id": cycle_id, "cycle_number": cycle_number, **summary}
    except Exception as exc:
        db.execute("UPDATE hive_discovery_cycles SET status='failed',summary=?::jsonb,completed_at=NOW() WHERE id=?",
                   [json.dumps({"error": str(exc)[:1000]}), cycle_id])
        db.commit()
        raise
    finally:
        db.close()


def get_discovery_map(mission_id: str) -> dict:
    db = connect()
    ensure_schema(db)
    nodes = db.execute(
        "SELECT id,label,node_type,description,epistemic_state,context,novelty FROM hive_discovery_nodes WHERE mission_id=?",
        [mission_id],
    ).fetchall()
    edges = db.execute(
        "SELECT id,source_node_id,target_node_id,relation,epistemic_state,confidence,evidence_url,evidence_excerpt,conditions FROM hive_discovery_edges WHERE mission_id=?",
        [mission_id],
    ).fetchall()
    frontiers = db.execute(
        "SELECT id,title,frontier_type,rationale,source_node_id,target_node_id,absence_type,status,evidence_search,scores,priority FROM hive_frontiers WHERE mission_id=? ORDER BY priority DESC",
        [mission_id],
    ).fetchall()
    hypotheses = db.execute(
        "SELECT id,name,hypothesis_type,statement,reasoning,path_nodes,supporting_edges,missing_links,assumptions,predictions,discriminators,safety_risks,scores,status FROM hive_hypothesis_paths WHERE mission_id=? ORDER BY COALESCE((scores->>'priority')::float,0) DESC",
        [mission_id],
    ).fetchall()
    cycles = db.execute(
        "SELECT id,cycle_number,status,trigger,summary,started_at,completed_at FROM hive_discovery_cycles WHERE mission_id=? ORDER BY cycle_number DESC LIMIT 20",
        [mission_id],
    ).fetchall()
    db.close()
    return {
        "mission_id": mission_id,
        "nodes": [{"id":r[0],"label":r[1],"type":r[2],"description":r[3] or "","state":r[4],"context":r[5] or {},"novelty":r[6]} for r in nodes],
        "edges": [{"id":r[0],"source":r[1],"target":r[2],"relation":r[3],"state":r[4],"confidence":r[5],"evidence_url":r[6] or "","excerpt":r[7] or "","conditions":r[8] or {}} for r in edges],
        "frontiers": [{"id":r[0],"title":r[1],"type":r[2],"rationale":r[3] or "","source":r[4],"target":r[5],"absence_type":r[6],"status":r[7],"search_strategy":r[8] or {},"scores":r[9] or {},"priority":r[10]} for r in frontiers],
        "hypotheses": [{"id":r[0],"name":r[1],"type":r[2],"statement":r[3],"reasoning":r[4] or "","path_nodes":r[5] or [],"supporting_edges":r[6] or [],"missing_links":r[7] or [],"assumptions":r[8] or [],"predictions":r[9] or [],"discriminators":r[10] or [],"safety_risks":r[11] or [],"scores":r[12] or {},"status":r[13]} for r in hypotheses],
        "cycles": [{"id":r[0],"number":r[1],"status":r[2],"trigger":r[3],"summary":r[4] or {},"started_at":r[5],"completed_at":r[6]} for r in cycles],
    }
