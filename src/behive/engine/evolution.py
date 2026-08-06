"""Evolutionary hypothesis reasoning for BeHive's open-world discovery graph."""

from __future__ import annotations

import json
import math
import re
from hashlib import sha1

from behive.engine.db import connect
from behive.engine.llm import complete

SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_hypothesis_versions (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, hypothesis_id VARCHAR,
 parent_version_id VARCHAR, generation INTEGER NOT NULL DEFAULT 0,
 mutation_operator VARCHAR NOT NULL, name TEXT NOT NULL, statement TEXT NOT NULL,
 rationale TEXT, hypothesis_graph JSONB DEFAULT '{}'::jsonb,
 predictions JSONB DEFAULT '[]'::jsonb, discriminators JSONB DEFAULT '[]'::jsonb,
 assumptions JSONB DEFAULT '[]'::jsonb, safety_risks JSONB DEFAULT '[]'::jsonb,
 scores JSONB DEFAULT '{}'::jsonb, prior_plausibility DOUBLE PRECISION DEFAULT 0.5,
 posterior_plausibility DOUBLE PRECISION DEFAULT 0.5, pareto_survivor BOOLEAN DEFAULT FALSE,
 status VARCHAR DEFAULT 'candidate', created_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,statement,generation)
);
CREATE TABLE IF NOT EXISTS hive_hypothesis_updates (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, version_id VARCHAR NOT NULL,
 evidence_edge_id VARCHAR, evidence_url TEXT, direction VARCHAR NOT NULL,
 weight DOUBLE PRECISION NOT NULL, prior DOUBLE PRECISION, posterior DOUBLE PRECISION,
 reason TEXT, conditions JSONB DEFAULT '{}'::jsonb, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hive_discovery_experiments (
 id VARCHAR PRIMARY KEY, mission_id VARCHAR NOT NULL, title TEXT NOT NULL,
 experiment_type VARCHAR NOT NULL, description TEXT, hypotheses_distinguished JSONB DEFAULT '[]'::jsonb,
 possible_outcomes JSONB DEFAULT '[]'::jsonb, affected_edges JSONB DEFAULT '[]'::jsonb,
 required_data JSONB DEFAULT '[]'::jsonb, cost_score DOUBLE PRECISION DEFAULT 0.5,
 duration_score DOUBLE PRECISION DEFAULT 0.5, safety_risk DOUBLE PRECISION DEFAULT 0.5,
 feasibility DOUBLE PRECISION DEFAULT 0.5, information_gain DOUBLE PRECISION DEFAULT 0.5,
 utility DOUBLE PRECISION DEFAULT 0.5, status VARCHAR DEFAULT 'proposed',
 created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(mission_id,title)
);
CREATE TABLE IF NOT EXISTS hive_graph_events (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, event_type VARCHAR NOT NULL,
 subject_type VARCHAR NOT NULL, subject_id VARCHAR, before_state JSONB,
 after_state JSONB, reason TEXT, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hive_graph_audits (
 id SERIAL PRIMARY KEY, mission_id VARCHAR NOT NULL, severity VARCHAR NOT NULL,
 audit_type VARCHAR NOT NULL, subject_id VARCHAR, message TEXT NOT NULL,
 remediation TEXT, status VARCHAR DEFAULT 'open', created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_versions_mission ON hive_hypothesis_versions(mission_id,generation);
CREATE INDEX IF NOT EXISTS idx_experiments_mission_utility ON hive_discovery_experiments(mission_id,utility DESC);
CREATE INDEX IF NOT EXISTS idx_graph_events_mission ON hive_graph_events(mission_id,created_at DESC);
"""


def ensure_schema(db=None) -> None:
    owned = db is None
    db = db or connect()
    for statement in (part.strip() for part in SCHEMA.split(";") if part.strip()):
        db.execute(statement)
    if owned:
        db.close()


def _id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{sha1('|'.join(parts).lower().encode()).hexdigest()[:18]}"


def _parse(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        value = json.loads(match.group(0)) if match else {}
    return value if isinstance(value, dict) else {}


def _score(scores: dict, key: str, default: float = .5) -> float:
    try:
        return max(0, min(1, float(scores.get(key, default))))
    except (TypeError, ValueError):
        return default


def _utility(scores: dict) -> float:
    return round(.18*_score(scores,"explanatory_reach") + .17*_score(scores,"evidence_compatibility") +
                 .15*_score(scores,"novelty") + .17*_score(scores,"testability") +
                 .16*_score(scores,"information_value") + .12*_score(scores,"impact") +
                 .05*(1-_score(scores,"contradiction_burden")), 4)


def _dominates(a: dict, b: dict) -> bool:
    beneficial = ("explanatory_reach","evidence_compatibility","novelty","testability","information_value","impact")
    adverse = ("contradiction_burden","experiment_cost","safety_risk","assumption_dependence")
    av = [_score(a,k) for k in beneficial] + [1-_score(a,k) for k in adverse]
    bv = [_score(b,k) for k in beneficial] + [1-_score(b,k) for k in adverse]
    return all(x >= y for x,y in zip(av,bv)) and any(x > y for x,y in zip(av,bv))


def _pareto(rows: list[tuple[str, dict]]) -> set[str]:
    return {rid for rid, scores in rows if not any(_dominates(other_scores, scores)
            for other_id, other_scores in rows if other_id != rid)}


def _evolution_prompt(topic: str, hypotheses: list[tuple], frontiers: list[tuple], nodes: list[tuple], edges: list[tuple]) -> dict:
    hypothesis_text = "\n".join(
        f"H{i+1} id={h[0]} type={h[2]} name={h[1]}\nSTATEMENT: {h[3]}\nREASONING: {h[4]}\n"
        f"PREDICTIONS: {json.dumps(h[5] or [])}\nMISSING: {json.dumps(h[6] or [])}"
        for i,h in enumerate(hypotheses)
    )
    frontier_text = "\n".join(f"F{i+1}: {f[0]} [{f[1]}/{f[2]}] {f[3]}" for i,f in enumerate(frontiers))
    node_text = ", ".join(f"{n[0]}({n[1]})" for n in nodes[:100])
    edge_text = "\n".join(f"{e[0]} --{e[2]}/{e[3]}--> {e[1]}" for e in edges[:140])
    prompt = f"""EXPLORATORY PROBE
{topic}

CURRENT HYPOTHESES
{hypothesis_text}

OPEN FRONTIERS
{frontier_text}

GRAPH NODES
{node_text}

EVIDENCE RELATIONSHIPS
{edge_text}

Evolve this hypothesis population; do not answer the probe. Generate 12-18 candidate versions spanning all
operators at least once: expand, contract, substitute, contextualize, invert, transfer, combine, split, nullify.
Every candidate must name a parent_hypothesis_id when applicable. Preserve uncertainty. A graph edge may be
observed only if present above; proposed links must be state=hypothesis or missing.

Return JSON:
- variants: name, parent_hypothesis_id, mutation_operator, hypothesis_type, statement, rationale,
  graph {{nodes:[labels],edges:[{{source,target,relation,state}}]}}, assumptions, predictions,
  discriminators, safety_risks, scores containing explanatory_reach,evidence_compatibility,
  contradiction_burden,novelty,testability,information_value,experiment_cost,impact,safety_risk,
  assumption_dependence (all 0-1), prior_plausibility 0.05-0.85
- experiments: title, experiment_type, description, hypothesis_names_distinguished, possible_outcomes,
  affected_edges, required_data, cost_score,duration_score,safety_risk,feasibility,information_gain (0-1)
Experiments must distinguish at least two competing candidates or strongly test a null. Prefer tests that
collapse many branches per unit cost. JSON only."""
    return _parse(complete(prompt, stage="synth", system="You manage evolutionary scientific hypotheses and information-gain experiments.",
                           max_tokens=9000, temperature=.45, json_mode=True))


def _coverage_completion(topic: str, hypotheses: list[tuple], missing: set[str], experiment_shortfall: int) -> dict:
    seeds = "\n".join(f"id={h[0]} name={h[1]} type={h[2]} statement={h[3]}" for h in hypotheses)
    prompt = f"""EXPLORATORY PROBE
{topic}

SEED HYPOTHESES
{seeds}

The current evolutionary generation omitted required mutation families: {', '.join(sorted(missing)) or 'none'}.
Generate exactly one scientifically distinct variant for every missing operator and {experiment_shortfall} additional
experiments that discriminate at least two hypotheses or a hypothesis from its null. Do not present any hypothesis
as a finding. Return the same JSON shape as below:
{{"variants":[{{"name":"","parent_hypothesis_id":"","mutation_operator":"","hypothesis_type":"",
"statement":"","rationale":"","graph":{{"nodes":[],"edges":[]}},"assumptions":[],"predictions":[],
"discriminators":[],"safety_risks":[],"scores":{{"explanatory_reach":0.5,"evidence_compatibility":0.5,
"contradiction_burden":0.5,"novelty":0.5,"testability":0.5,"information_value":0.5,
"experiment_cost":0.5,"impact":0.5,"safety_risk":0.5,"assumption_dependence":0.5}},"prior_plausibility":0.3}}],
"experiments":[{{"title":"","experiment_type":"","description":"","hypothesis_names_distinguished":[],
"possible_outcomes":[],"affected_edges":[],"required_data":[],"cost_score":0.5,"duration_score":0.5,
"safety_risk":0.5,"feasibility":0.5,"information_gain":0.5}}]}} JSON only."""
    return _parse(complete(prompt,stage="synth",system="You enforce complete hypothesis mutation coverage.",
                           max_tokens=6000,temperature=.35,json_mode=True))


def evolve_hypotheses(mission_id: str, trigger: str = "frontier_cycle") -> dict:
    db = connect(); ensure_schema(db)
    topic = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()[0]
    hypotheses = db.execute(
        "SELECT id,name,hypothesis_type,statement,reasoning,predictions,missing_links FROM hive_hypothesis_paths WHERE mission_id=?",
        [mission_id]).fetchall()
    if not hypotheses:
        db.close(); return {"versions":0,"survivors":0,"experiments":0,"reason":"no seed hypotheses"}
    frontiers = db.execute(
        "SELECT title,frontier_type,absence_type,rationale FROM hive_frontiers WHERE mission_id=? ORDER BY priority DESC LIMIT 20",
        [mission_id]).fetchall()
    nodes = db.execute("SELECT label,node_type FROM hive_discovery_nodes WHERE mission_id=? ORDER BY novelty DESC",[mission_id]).fetchall()
    edges = db.execute(
        """SELECT s.label,t.label,e.relation,e.epistemic_state FROM hive_discovery_edges e
           JOIN hive_discovery_nodes s ON s.id=e.source_node_id JOIN hive_discovery_nodes t ON t.id=e.target_node_id
           WHERE e.mission_id=?""",[mission_id]).fetchall()
    generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM hive_hypothesis_versions WHERE mission_id=?",[mission_id]).fetchone()[0]
    data = _evolution_prompt(topic,hypotheses,frontiers,nodes,edges)
    required_operators={"expand","contract","substitute","contextualize","invert","transfer","combine","split","nullify"}
    present={str(v.get("mutation_operator") or "").lower() for v in data.get("variants",[]) if isinstance(v,dict)}
    missing=required_operators-present
    experiment_shortfall=max(0,5-len(data.get("experiments",[])))
    if missing or experiment_shortfall:
        supplement=_coverage_completion(topic,hypotheses,missing,experiment_shortfall)
        data.setdefault("variants",[]).extend(supplement.get("variants",[]))
        data.setdefault("experiments",[]).extend(supplement.get("experiments",[]))
    ids_by_name, candidates = {}, []
    seed_ids = {h[0] for h in hypotheses}
    for raw in data.get("variants",[]):
        if not isinstance(raw,dict) or not raw.get("name") or not raw.get("statement"): continue
        parent = str(raw.get("parent_hypothesis_id") or "")
        parent = parent if parent in seed_ids or parent.startswith("hv_") else None
        scores = raw.get("scores") if isinstance(raw.get("scores"),dict) else {}
        scores["utility"] = _utility(scores)
        version_id = _id("hv",mission_id,str(generation),str(raw["statement"]))
        prior = max(.05,min(.85,float(raw.get("prior_plausibility",.35) or .35)))
        db.execute(
            """INSERT INTO hive_hypothesis_versions
               (id,mission_id,hypothesis_id,parent_version_id,generation,mutation_operator,name,statement,rationale,
                hypothesis_graph,predictions,discriminators,assumptions,safety_risks,scores,prior_plausibility,posterior_plausibility)
               VALUES (?,?,?,?,?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?,?) ON CONFLICT DO NOTHING""",
            [version_id,mission_id,parent,parent,generation,str(raw.get("mutation_operator") or "expand")[:60],
             str(raw["name"])[:1000],str(raw["statement"])[:4000],str(raw.get("rationale") or "")[:5000],
             json.dumps(raw.get("graph") or {}),json.dumps(raw.get("predictions") or []),
             json.dumps(raw.get("discriminators") or []),json.dumps(raw.get("assumptions") or []),
             json.dumps(raw.get("safety_risks") or []),json.dumps(scores),prior,prior])
        ids_by_name[str(raw["name"]).lower()] = version_id; candidates.append((version_id,scores))
    survivors = _pareto(candidates)
    for version_id,_scores in candidates:
        db.execute("UPDATE hive_hypothesis_versions SET pareto_survivor=?,status=? WHERE id=?",
                   [version_id in survivors,"survivor" if version_id in survivors else "dominated",version_id])
    experiments=0
    for raw in data.get("experiments",[]):
        if not isinstance(raw,dict) or not raw.get("title"): continue
        distinguished=[ids_by_name.get(str(name).lower(),str(name)) for name in raw.get("hypothesis_names_distinguished",[])]
        ig=_score(raw,"information_gain"); cost=_score(raw,"cost_score"); duration=_score(raw,"duration_score")
        safety=_score(raw,"safety_risk"); feasibility=_score(raw,"feasibility")
        utility=round(ig*feasibility*(1-.45*cost)*(1-.25*duration)*(1-.35*safety),4)
        experiment_id=_id("ex",mission_id,str(raw["title"]))
        db.execute(
            """INSERT INTO hive_discovery_experiments
               (id,mission_id,title,experiment_type,description,hypotheses_distinguished,possible_outcomes,
                affected_edges,required_data,cost_score,duration_score,safety_risk,feasibility,information_gain,utility)
               VALUES (?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?,?,?,?,?,?)
               ON CONFLICT (id) DO UPDATE SET description=EXCLUDED.description,hypotheses_distinguished=EXCLUDED.hypotheses_distinguished,
               information_gain=EXCLUDED.information_gain,utility=EXCLUDED.utility,updated_at=NOW()""",
            [experiment_id,mission_id,str(raw["title"])[:1000],str(raw.get("experiment_type") or "analysis")[:80],
             str(raw.get("description") or "")[:5000],json.dumps(distinguished),json.dumps(raw.get("possible_outcomes") or []),
             json.dumps(raw.get("affected_edges") or []),json.dumps(raw.get("required_data") or []),
             cost,duration,safety,feasibility,ig,utility])
        experiments+=1
    db.execute("INSERT INTO hive_graph_events (mission_id,event_type,subject_type,reason,after_state) VALUES (?,?,?,?,?::jsonb)",
               [mission_id,"hypothesis_generation","population",trigger,json.dumps({"generation":generation,"candidates":len(candidates),"survivors":len(survivors),"experiments":experiments})])
    db.commit(); db.close()
    audit_graph(mission_id)
    return {"generation":generation,"versions":len(candidates),"survivors":len(survivors),"experiments":experiments}


def update_hypothesis(mission_id: str, version_id: str, direction: str, weight: float, reason: str,
                      evidence_edge_id: str = "", evidence_url: str = "", conditions: dict | None = None) -> dict:
    if direction not in {"supports","weakens","contradicts","non_discriminating","context_limits"}:
        raise ValueError("Invalid evidence direction")
    db=connect(); ensure_schema(db)
    row=db.execute("SELECT posterior_plausibility FROM hive_hypothesis_versions WHERE id=? AND mission_id=?",[version_id,mission_id]).fetchone()
    if not row: db.close(); raise ValueError("Hypothesis version not found")
    prior=max(.01,min(.99,float(row[0] or .5))); weight=max(0,min(1,float(weight)))
    multiplier={"supports":1,"weakens":-1,"contradicts":-1.7,"non_discriminating":0,"context_limits":-.45}[direction]
    logit=math.log(prior/(1-prior)); posterior=1/(1+math.exp(-(logit+multiplier*weight*1.8)))
    db.execute("UPDATE hive_hypothesis_versions SET posterior_plausibility=? WHERE id=?",[posterior,version_id])
    db.execute("""INSERT INTO hive_hypothesis_updates
       (mission_id,version_id,evidence_edge_id,evidence_url,direction,weight,prior,posterior,reason,conditions)
       VALUES (?,?,?,?,?,?,?,?,?,?::jsonb)""",[mission_id,version_id,evidence_edge_id or None,evidence_url,direction,weight,prior,posterior,reason[:3000],json.dumps(conditions or {})])
    db.execute("INSERT INTO hive_graph_events (mission_id,event_type,subject_type,subject_id,before_state,after_state,reason) VALUES (?,?,?,?,?::jsonb,?::jsonb,?)",
               [mission_id,"evidence_update","hypothesis",version_id,json.dumps({"plausibility":prior}),json.dumps({"plausibility":posterior,"direction":direction}),reason[:3000]])
    db.commit(); db.close(); return {"version_id":version_id,"prior":round(prior,4),"posterior":round(posterior,4),"direction":direction}


def counterfactual(mission_id: str, subject_id: str) -> dict:
    db=connect(); ensure_schema(db)
    edge=db.execute("""SELECT s.label,t.label,e.relation FROM hive_discovery_edges e
      JOIN hive_discovery_nodes s ON s.id=e.source_node_id JOIN hive_discovery_nodes t ON t.id=e.target_node_id
      WHERE e.id=? AND e.mission_id=?""",[subject_id,mission_id]).fetchone()
    node=db.execute("SELECT label FROM hive_discovery_nodes WHERE id=? AND mission_id=?",[subject_id,mission_id]).fetchone()
    needle=[subject_id]+([edge[0],edge[1],edge[2]] if edge else [])+([node[0]] if node else [])
    versions=db.execute("SELECT id,name,statement,hypothesis_graph,posterior_plausibility FROM hive_hypothesis_versions WHERE mission_id=? AND pareto_survivor=TRUE",[mission_id]).fetchall()
    affected=[]
    for row in versions:
        blob=json.dumps(row[3] or {}).lower()
        if any(str(value).lower() in blob for value in needle):
            affected.append({"id":row[0],"name":row[1],"statement":row[2],"plausibility":row[4]})
    db.close()
    return {"subject_id":subject_id,"assumption":"relationship_or_node_is_false","affected_hypotheses":affected,
            "collapse_count":len(affected),"remaining_survivors":max(0,len(versions)-len(affected)),
            "recommended_action":"Prioritize an experiment that directly perturbs or measures this dependency."}


def audit_graph(mission_id: str) -> dict:
    db=connect(); ensure_schema(db)
    db.execute("DELETE FROM hive_graph_audits WHERE mission_id=? AND status='open'",[mission_id])
    edges=db.execute("SELECT id,epistemic_state,evidence_url,conditions,confidence FROM hive_discovery_edges WHERE mission_id=?",[mission_id]).fetchall()
    findings=[]
    for edge_id,state,url,conditions,confidence in edges:
        if state in {"observed","reported"} and not url:
            findings.append(("high","missing_provenance",edge_id,"Observed edge has no source URL","Attach evidence or downgrade the edge to inferred."))
        if state=="observed" and not conditions:
            findings.append(("medium","missing_context",edge_id,"Observed edge lacks experimental conditions","Add organism, tissue, dose, timing, and method where available."))
        if float(confidence or 0)>.8 and state not in {"observed","replicated"}:
            findings.append(("medium","confidence_state_collision",edge_id,"High confidence is assigned to a non-observed edge","Reduce confidence or provide direct evidence."))
    for finding in findings:
        db.execute("INSERT INTO hive_graph_audits (mission_id,severity,audit_type,subject_id,message,remediation) VALUES (?,?,?,?,?,?)",[mission_id,*finding])
    db.commit(); db.close(); return {"mission_id":mission_id,"open_findings":len(findings)}


def get_evolution(mission_id: str) -> dict:
    db=connect(); ensure_schema(db)
    versions=db.execute("""SELECT id,hypothesis_id,parent_version_id,generation,mutation_operator,name,statement,rationale,
      hypothesis_graph,predictions,discriminators,assumptions,safety_risks,scores,prior_plausibility,posterior_plausibility,pareto_survivor,status,created_at
      FROM hive_hypothesis_versions WHERE mission_id=? ORDER BY generation DESC,pareto_survivor DESC,(scores->>'utility')::float DESC""",[mission_id]).fetchall()
    experiments=db.execute("""SELECT id,title,experiment_type,description,hypotheses_distinguished,possible_outcomes,affected_edges,required_data,
      cost_score,duration_score,safety_risk,feasibility,information_gain,utility,status FROM hive_discovery_experiments WHERE mission_id=? ORDER BY utility DESC""",[mission_id]).fetchall()
    events=db.execute("SELECT id,event_type,subject_type,subject_id,before_state,after_state,reason,created_at FROM hive_graph_events WHERE mission_id=? ORDER BY created_at DESC LIMIT 100",[mission_id]).fetchall()
    audits=db.execute("SELECT id,severity,audit_type,subject_id,message,remediation,status FROM hive_graph_audits WHERE mission_id=? ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,id DESC",[mission_id]).fetchall()
    db.close()
    return {"mission_id":mission_id,
      "versions":[{"id":r[0],"hypothesis_id":r[1],"parent":r[2],"generation":r[3],"operator":r[4],"name":r[5],"statement":r[6],"rationale":r[7] or "","graph":r[8] or {},"predictions":r[9] or [],"discriminators":r[10] or [],"assumptions":r[11] or [],"safety_risks":r[12] or [],"scores":r[13] or {},"prior":r[14],"posterior":r[15],"survivor":r[16],"status":r[17],"created_at":r[18]} for r in versions],
      "experiments":[{"id":r[0],"title":r[1],"type":r[2],"description":r[3] or "","hypotheses":r[4] or [],"outcomes":r[5] or [],"affected_edges":r[6] or [],"required_data":r[7] or [],"cost":r[8],"duration":r[9],"safety_risk":r[10],"feasibility":r[11],"information_gain":r[12],"utility":r[13],"status":r[14]} for r in experiments],
      "events":[{"id":r[0],"type":r[1],"subject_type":r[2],"subject_id":r[3],"before":r[4],"after":r[5],"reason":r[6],"created_at":r[7]} for r in events],
      "audits":[{"id":r[0],"severity":r[1],"type":r[2],"subject_id":r[3],"message":r[4],"remediation":r[5],"status":r[6]} for r in audits]}
