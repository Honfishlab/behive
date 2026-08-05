"""API for continuous, question-led research projects."""

from __future__ import annotations

import hashlib
import json
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from behive.discovery import QuestionSignal, connection_strength, rank_frontier, suggest_followups

router = APIRouter(prefix="/projects", tags=["living research"])


def _db():
    from behive.server import get_db
    return get_db()


def _id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"{value}:{time.time_ns()}".encode()).hexdigest()[:12]
    return f"{prefix}_{digest}"


class ProjectCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    root_question: str = Field(min_length=8, max_length=2000)
    cadence_minutes: int = Field(default=1440, ge=15, le=43200)


class QuestionCreate(BaseModel):
    question: str = Field(min_length=8, max_length=2000)
    parent_id: str | None = None
    kind: str = "followup"
    priority: float = Field(default=0.5, ge=0, le=1)


class QuestionUpdate(BaseModel):
    question: str | None = Field(default=None, min_length=8, max_length=2000)
    parent_id: str | None = None
    priority: float | None = Field(default=None, ge=0, le=1)
    status: str | None = Field(default=None, pattern="^(open|researching|answered|paused|archived)$")


class AgentPolicy(BaseModel):
    autonomy: str = Field(default="assisted", pattern="^(manual|assisted|guarded|continuous)$")
    status: str = Field(default="paused", pattern="^(paused|running)$")
    daily_budget: float = Field(default=10, ge=0, le=10000)
    max_agents: int = Field(default=3, ge=1, le=50)
    max_depth: int = Field(default=5, ge=1, le=20)
    min_confidence: float = Field(default=0.7, ge=0, le=1)
    primary_sources_required: bool = True
    approval_new_branches: bool = True
    freshness_days: int = Field(default=30, ge=1, le=3650)


class FindingCreate(BaseModel):
    question_id: str
    summary: str = Field(min_length=10, max_length=4000)
    source_url: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    novelty: float = Field(default=0.5, ge=0, le=1)
    status: str = Field(default="unreviewed", pattern="^(unreviewed|supported|contradicted|superseded)$")


@router.post("")
def create_project(payload: ProjectCreate):
    project_id, root_id = _id("project", payload.title), _id("question", payload.root_question)
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hive_projects (id,title,root_question,cadence_minutes) VALUES (%s,%s,%s,%s)",
            (project_id, payload.title, payload.root_question, payload.cadence_minutes),
        )
        cur.execute(
            "INSERT INTO hive_questions (id,project_id,question,depth,kind,priority,status) "
            "VALUES (%s,%s,%s,0,'root',1.0,'open')",
            (root_id, project_id, payload.root_question),
        )
    return {"id": project_id, "root_question_id": root_id, "status": "active"}


@router.get("")
def list_projects():
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.id,p.title,p.root_question,p.status,p.updated_at,COUNT(DISTINCT q.id),COUNT(DISTINCT f.id) "
            "FROM hive_projects p LEFT JOIN hive_questions q ON q.project_id=p.id "
            "LEFT JOIN hive_findings f ON f.project_id=p.id GROUP BY p.id ORDER BY p.updated_at DESC"
        )
        rows = cur.fetchall()
    return {"projects": [{"id": r[0], "title": r[1], "root_question": r[2], "status": r[3],
                           "updated_at": r[4], "questions": r[5], "findings": r[6]} for r in rows]}


@router.put("/{project_id}/policy")
def update_policy(project_id: str, payload: AgentPolicy):
    policy = payload.model_dump()
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE hive_projects SET agent_policy=%s::jsonb,updated_at=NOW() WHERE id=%s RETURNING id",
            (json.dumps(policy), project_id),
        )
        if not cur.fetchone():
            raise HTTPException(404, "Research project not found")
    return {"project_id": project_id, "policy": policy}


@router.post("/{project_id}/findings")
def add_finding(project_id: str, payload: FindingCreate):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM hive_questions WHERE id=%s AND project_id=%s", (payload.question_id, project_id))
        if not cur.fetchone():
            raise HTTPException(400, "Question is not in this project")
        cur.execute(
            "INSERT INTO hive_findings (project_id,question_id,summary,source_url,confidence,novelty,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (project_id, payload.question_id, payload.summary, payload.source_url,
             payload.confidence, payload.novelty, payload.status),
        )
        finding_id = cur.fetchone()[0]
    return {"id": finding_id, "status": payload.status}


@router.get("/{project_id}/workspace")
def project_workspace(project_id: str):
    graph = project_map(project_id)
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT agent_policy,cadence_minutes,updated_at FROM hive_projects WHERE id=%s", (project_id,))
        policy = cur.fetchone()
        cur.execute(
            "SELECT DISTINCT COALESCE(f.source_url,c.source_url),c.claim,f.confidence,f.created_at "
            "FROM hive_findings f LEFT JOIN hive_claims c ON c.id=f.claim_id "
            "WHERE f.project_id=%s AND COALESCE(f.source_url,c.source_url) IS NOT NULL ORDER BY f.created_at DESC",
            (project_id,),
        )
        sources = cur.fetchall()
    return {**graph, "policy": policy[0] or {}, "cadence_minutes": policy[1], "updated_at": policy[2],
            "library": [{"url": r[0], "claim": r[1], "confidence": r[2], "added_at": r[3]} for r in sources],
            "approvals": [n for n in graph["nodes"] if n.get("type") == "finding" and n.get("status") == "unreviewed"]}


@router.post("/{project_id}/questions")
def add_question(project_id: str, payload: QuestionCreate):
    question_id, depth = _id("question", payload.question), 0
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM hive_projects WHERE id=%s", (project_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Research project not found")
        if payload.parent_id:
            cur.execute(
                "SELECT depth FROM hive_questions WHERE id=%s AND project_id=%s",
                (payload.parent_id, project_id),
            )
            parent = cur.fetchone()
            if not parent:
                raise HTTPException(400, "Parent question is not in this project")
            depth = parent[0] + 1
        cur.execute(
            "INSERT INTO hive_questions (id,project_id,parent_id,question,depth,kind,priority) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (question_id, project_id, payload.parent_id, payload.question, depth, payload.kind, payload.priority),
        )
    return {"id": question_id, "depth": depth, "status": "open"}


@router.patch("/{project_id}/questions/{question_id}")
def update_question(project_id: str, question_id: str, payload: QuestionUpdate):
    changes = payload.model_dump(exclude_unset=True)
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT parent_id,depth FROM hive_questions WHERE id=%s AND project_id=%s",
            (question_id, project_id),
        )
        current = cur.fetchone()
        if not current:
            raise HTTPException(404, "Question not found")
        depth = current[1]
        if "parent_id" in changes:
            if changes["parent_id"] == question_id:
                raise HTTPException(400, "A question cannot be its own parent")
            if changes["parent_id"]:
                cur.execute(
                    "SELECT depth FROM hive_questions WHERE id=%s AND project_id=%s",
                    (changes["parent_id"], project_id),
                )
                parent = cur.fetchone()
                if not parent:
                    raise HTTPException(400, "Parent question is not in this project")
                depth = parent[0] + 1
            else:
                depth = 0
        cur.execute(
            "UPDATE hive_questions SET question=COALESCE(%s,question),parent_id=%s,priority=COALESCE(%s,priority),"
            "status=COALESCE(%s,status),depth=%s,updated_at=NOW() WHERE id=%s AND project_id=%s RETURNING id",
            (changes.get("question"), changes.get("parent_id", current[0]), changes.get("priority"),
             changes.get("status"), depth, question_id, project_id),
        )
        if depth == 0 and changes.get("question"):
            cur.execute(
                "UPDATE hive_projects SET root_question=%s,updated_at=NOW() WHERE id=%s",
                (changes["question"], project_id),
            )
    return {"id": question_id, "depth": depth, **changes}


@router.get("/{project_id}/map")
def project_map(project_id: str):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id,title,root_question,status FROM hive_projects WHERE id=%s", (project_id,))
        project = cur.fetchone()
        if not project:
            raise HTTPException(404, "Research project not found")
        cur.execute(
            "SELECT id,parent_id,question,depth,kind,priority,confidence,status,mission_id "
            "FROM hive_questions WHERE project_id=%s ORDER BY depth,created_at",
            (project_id,),
        )
        questions = cur.fetchall()
        cur.execute(
            "SELECT f.id,f.question_id,f.claim_id,f.summary,f.confidence,f.novelty,f.status,c.source_url "
            "FROM hive_findings f LEFT JOIN hive_claims c ON c.id=f.claim_id "
            "WHERE f.project_id=%s ORDER BY f.created_at DESC",
            (project_id,),
        )
        findings = cur.fetchall()
    nodes = [
        {"id": q[0], "type": "question", "parent_id": q[1], "label": q[2], "depth": q[3],
         "kind": q[4], "priority": q[5], "confidence": q[6], "status": q[7], "mission_id": q[8]}
        for q in questions
    ]
    nodes += [
        {"id": f[0], "type": "finding", "question_id": f[1], "claim_id": f[2], "label": f[3],
         "confidence": f[4], "novelty": f[5], "status": f[6], "source_url": f[7]}
        for f in findings
    ]
    edges = [
        {"source": q[1], "target": q[0], "type": "decomposes"} for q in questions if q[1]
    ] + [
        {"source": f[1], "target": f[0], "type": "supported_by"} for f in findings
    ]
    return {"project": {"id": project[0], "title": project[1], "root_question": project[2], "status": project[3]},
            "nodes": nodes, "edges": edges}


@router.get("/{project_id}/frontier")
def discovery_frontier(project_id: str, limit: int = Query(5, ge=1, le=20)):
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT q.id,q.question,q.depth,q.priority,q.confidence,COUNT(f.id),"
            "COUNT(f.id) FILTER (WHERE f.status='contradicted'),"
            "EXTRACT(day FROM NOW()-q.updated_at)::int "
            "FROM hive_questions q LEFT JOIN hive_findings f ON f.question_id=q.id "
            "WHERE q.project_id=%s AND q.status IN ('open','researching') GROUP BY q.id",
            (project_id,),
        )
        rows = cur.fetchall()
    ranked = rank_frontier([
        QuestionSignal(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows
    ], limit)
    return {"frontier": [{"id": q.id, "question": q.question, "score": q.discovery_score} for q in ranked]}


@router.get("/{project_id}/suggestions")
def discovery_suggestions(project_id: str):
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT q.id,q.question,COALESCE(array_agg(DISTINCT e.value) FILTER (WHERE e.value IS NOT NULL),'{}') "
            "FROM hive_questions q LEFT JOIN hive_missions m ON m.id=q.mission_id "
            "LEFT JOIN hive_entities e ON e.mission_id=m.id "
            "WHERE q.project_id=%s AND q.status='open' GROUP BY q.id ORDER BY q.priority DESC LIMIT 5",
            (project_id,),
        )
        rows = cur.fetchall()
    suggestions = []
    for question_id, question, entities in rows:
        for item in suggest_followups(question, entities):
            item.update({"parent_id": question_id, "connection_strength": connection_strength(question, item["question"])})
            suggestions.append(item)
    return {"suggestions": suggestions}
