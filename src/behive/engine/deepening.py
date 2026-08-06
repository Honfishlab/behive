"""Checkpointed evidence-deepening support for Analyst research gaps."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from behive.engine.db import connect

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hive_gap_runs (
    id BIGSERIAL PRIMARY KEY,
    gap_id INTEGER NOT NULL REFERENCES hive_gaps(id),
    parent_mission_id TEXT NOT NULL REFERENCES hive_missions(id),
    child_mission_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    target_sources INTEGER NOT NULL DEFAULT 3,
    target_primary INTEGER NOT NULL DEFAULT 1,
    full_text_sources INTEGER NOT NULL DEFAULT 0,
    primary_sources INTEGER NOT NULL DEFAULT 0,
    merged_claims INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hive_analysis_revisions (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES hive_missions(id),
    gap_run_id BIGINT REFERENCES hive_gap_runs(id),
    previous_analysis JSONB,
    new_analysis JSONB NOT NULL,
    change_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
"""


def ensure_schema() -> None:
    with connect() as con:
        con.execute(SCHEMA_SQL)
        con.commit()


def is_primary(domain: str, source_type: str = "", title: str = "") -> bool:
    host = (domain or "").lower()
    text = f"{source_type} {title}".lower()
    return (host.endswith(".gov") or host.endswith(".edu") or "doi.org" in host or
            "arxiv.org" in host or any(x in text for x in ("dataset", "study", "research report", "official report", "filing")))


def create_run(gap_id: int, parent_mission_id: str, child_mission_id: str) -> int:
    ensure_schema()
    with connect() as con:
        row = con.execute(
            "INSERT INTO hive_gap_runs (gap_id,parent_mission_id,child_mission_id,status,message) "
            "VALUES (?,?,?,'queued','Waiting for deep research worker') RETURNING id",
            [gap_id, parent_mission_id, child_mission_id],
        ).fetchone()
        con.commit()
        return row[0]


def update_run(run_id: int, status: str, message: str, **metrics) -> None:
    allowed = {"full_text_sources", "primary_sources", "merged_claims"}
    fields = ["status=?", "message=?", "updated_at=NOW()"]
    values = [status, message]
    for key, value in metrics.items():
        if key in allowed:
            fields.append(f"{key}=?")
            values.append(int(value))
    if status in {"resolved", "exhausted", "failed", "paused", "dismissed"}:
        fields.append("finished_at=NOW()")
    values.append(run_id)
    with connect() as con:
        con.execute(f"UPDATE hive_gap_runs SET {','.join(fields)} WHERE id=?", values)
        con.commit()


def merge_qualified_evidence(run_id: int) -> dict:
    """Merge only full-text child evidence; snippets remain discovery records."""
    ensure_schema()
    with connect() as con:
        run = con.execute("SELECT gap_id,parent_mission_id,child_mission_id,target_sources,target_primary FROM hive_gap_runs WHERE id=?", [run_id]).fetchone()
        if not run:
            raise ValueError("Gap run not found")
        gap_id, parent_id, child_id, target_sources, target_primary = run
        docs = con.execute(
            "SELECT c.url,c.title,c.domain,c.raw_text,c.word_count,c.harvest_method,c.language,c.quality_score,c.author,c.published_date,s.source_type "
            "FROM hive_content c LEFT JOIN hive_sources s ON s.mission_id=c.mission_id AND s.url=c.url "
            "WHERE c.mission_id=? AND c.word_count>=300 AND COALESCE(c.harvest_method,'')<>'snippet_fallback'",
            [child_id],
        ).fetchall()
        primary_urls = {d[0] for d in docs if is_primary(d[2], d[10] or "", d[1] or "")}
        qualified_urls = {d[0] for d in docs}
        for d in docs:
            con.execute(
                "INSERT INTO hive_sources (mission_id,url,domain,title,source_type,scout_method,language,status) "
                "VALUES (?,?,?,?,?,'gap_deepening',?,'done') ON CONFLICT(mission_id,url) DO NOTHING",
                [parent_id, d[0], d[2], d[1], d[10], d[6]],
            )
            con.execute(
                "INSERT INTO hive_content (mission_id,url,domain,title,raw_text,word_count,harvest_method,language,quality_score,author,published_date) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(mission_id,url) DO NOTHING",
                [parent_id, d[0], d[2], d[1], d[3], d[4], "gap_deepening", d[6], d[7], d[8], d[9]],
            )
        merged_claims = 0
        if qualified_urls:
            claims = con.execute(
                "SELECT claim,evidence,source_url,claim_type,confidence,quality_score,verified FROM hive_claims "
                "WHERE mission_id=? AND source_url=ANY(?) AND (is_garbage=FALSE OR is_garbage IS NULL)",
                [child_id, list(qualified_urls)],
            ).fetchall()
            for claim in claims:
                exists = con.execute(
                    "SELECT 1 FROM hive_claims WHERE mission_id=? AND source_url=? AND lower(claim)=lower(?) LIMIT 1",
                    [parent_id, claim[2], claim[0]],
                ).fetchone()
                if exists:
                    continue
                con.execute(
                    "INSERT INTO hive_claims (mission_id,claim,evidence,source_url,claim_type,confidence,quality_score,source_operation,source_tier,verified) "
                    "VALUES (?,?,?,?,?,?,?,'gap_deepening',?,?)",
                    [parent_id, claim[0], claim[1], claim[2], claim[3], claim[4], claim[5], 1 if claim[2] in primary_urls else 2, claim[6]],
                )
                merged_claims += 1
        resolved = len(qualified_urls) >= target_sources and len(primary_urls) >= target_primary and merged_claims > 0
        con.execute("UPDATE hive_gaps SET resolved=? WHERE id=?", [resolved, gap_id])
        con.commit()
    return {"full_text_sources": len(qualified_urls), "primary_sources": len(primary_urls),
            "merged_claims": merged_claims, "resolved": resolved}


def save_revision(mission_id: str, run_id: int, previous: dict | None, current: dict, reason: str) -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            "INSERT INTO hive_analysis_revisions (mission_id,gap_run_id,previous_analysis,new_analysis,change_reason) VALUES (?,?,?,?,?)",
            [mission_id, run_id, json.dumps(previous) if previous else None, json.dumps(current), reason],
        )
        con.commit()

