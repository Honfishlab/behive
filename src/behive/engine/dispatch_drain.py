"""Transactional maintenance drain for the continuous-research dispatcher."""
from __future__ import annotations

from behive.engine.db import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS hive_dispatch_drain_snapshot (
  drain_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  agent_policy JSONB NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  restored_at TIMESTAMPTZ,
  PRIMARY KEY (drain_id, project_id)
)
"""


def pause_dispatch(drain_id: str, db=None) -> dict:
    """Pause new continuous dispatch while leaving active actions untouched."""
    owns = db is None
    db = db or connect()
    try:
        db.execute(SCHEMA)
        active_before = db.execute(
            "SELECT COUNT(*) FROM hive_research_actions WHERE status='running'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO hive_dispatch_drain_snapshot(drain_id,project_id,agent_policy) "
            "SELECT ?,id,agent_policy FROM hive_projects "
            "WHERE COALESCE(agent_policy->>'autonomy','assisted')='continuous' "
            "AND COALESCE(agent_policy->>'status','paused')='running' ON CONFLICT DO NOTHING",
            [drain_id],
        )
        paused = db.execute(
            "UPDATE hive_projects SET agent_policy=agent_policy || jsonb_build_object('status','paused'),"
            "updated_at=NOW() WHERE id IN (SELECT project_id FROM hive_dispatch_drain_snapshot "
            "WHERE drain_id=? AND restored_at IS NULL) RETURNING id", [drain_id],
        ).fetchall()
        active_after = db.execute(
            "SELECT COUNT(*) FROM hive_research_actions WHERE status='running'"
        ).fetchone()[0]
        db.commit()
        return {"drain_id": drain_id, "paused_projects": len(paused),
                "active_before": active_before, "active_after": active_after}
    finally:
        if owns:
            db.close()


def restore_dispatch(drain_id: str, db=None) -> dict:
    """Restore the exact policies captured by a completed maintenance drain."""
    owns = db is None
    db = db or connect()
    try:
        db.execute(SCHEMA)
        restored = db.execute(
            "UPDATE hive_projects p SET agent_policy=s.agent_policy,updated_at=NOW() "
            "FROM hive_dispatch_drain_snapshot s WHERE s.drain_id=? AND s.project_id=p.id "
            "AND s.restored_at IS NULL RETURNING p.id", [drain_id],
        ).fetchall()
        db.execute(
            "UPDATE hive_dispatch_drain_snapshot SET restored_at=NOW() "
            "WHERE drain_id=? AND restored_at IS NULL", [drain_id],
        )
        db.commit()
        return {"drain_id": drain_id, "restored_projects": len(restored)}
    finally:
        if owns:
            db.close()
