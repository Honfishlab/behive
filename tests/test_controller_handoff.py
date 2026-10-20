from unittest.mock import patch

from behive.engine import controller
from behive.engine import discovery_paths


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeDB:
    def __init__(self, *, linked=True, finalized=False):
        self.linked = linked
        self.finalized = finalized
        self.calls = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params or []))
        if "SELECT project_id,question_id,status" in sql:
            return Result(("project_1", "question_1", "done") if self.linked else (None, None, "done"))
        if "SELECT question,mission_id,last_researched_at" in sql:
            return Result(("What changed?", "mission_1", object() if self.finalized else None))
        if "SELECT COUNT(*) FROM hive_findings" in sql:
            return Result((3,))
        return Result()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_finalize_question_mission_promotes_and_seeds_queue():
    db = FakeDB()
    completeness = {"score": .6, "status": "evidence_found", "gaps": []}
    queue = {"actions_queued": 4, "branches_synced": 5}
    with patch.object(controller, "connect", return_value=db), \
         patch.object(controller, "ensure_schema"), \
         patch.object(controller, "_score_question", return_value=completeness), \
         patch.object(controller, "_expand_residual_unknowns", return_value=0), \
         patch.object(controller, "_propagate_question_state"), \
         patch("behive.engine.citation_auditor.audit_mission", return_value={"audited": 0}), \
         patch.object(discovery_paths, "generate_paths", return_value={"generated": 2}), \
         patch.object(discovery_paths, "activate_ranked", return_value={"activated": 1}), \
         patch.object(controller, "reconcile", return_value=queue):
        result = controller.finalize_question_mission("mission_1")

    assert result["status"] == "finalized"
    assert result["findings_promoted"] == 3
    assert result["actions_queued"] == 4
    assert db.committed and db.closed
    updates = [params for sql, params in db.calls if sql.startswith("UPDATE hive_questions SET")]
    assert updates[0][4] == 1


def test_finalize_question_mission_is_idempotent_per_question_mission():
    db = FakeDB(finalized=True)
    completeness = {"score": .6, "status": "evidence_found", "gaps": []}
    with patch.object(controller, "connect", return_value=db), \
         patch.object(controller, "ensure_schema"), \
         patch.object(controller, "_score_question", return_value=completeness), \
         patch.object(controller, "_expand_residual_unknowns") as expand, \
         patch.object(controller, "_propagate_question_state"), \
         patch("behive.engine.citation_auditor.audit_mission", return_value={"audited": 0}), \
         patch.object(discovery_paths, "generate_paths", return_value={"generated": 0}), \
         patch.object(discovery_paths, "activate_ranked", return_value={"activated": 0}), \
         patch.object(controller, "reconcile", return_value={}):
        controller.finalize_question_mission("mission_1")

    updates = [params for sql, params in db.calls if sql.startswith("UPDATE hive_questions SET")]
    assert updates[0][4] == 0
    expand.assert_not_called()


def test_finalize_question_mission_ignores_unlinked_missions():
    db = FakeDB(linked=False)
    with patch.object(controller, "connect", return_value=db), patch.object(controller, "ensure_schema"):
        result = controller.finalize_question_mission("mission_1")
    assert result["status"] == "not_linked"
    assert db.closed


def test_question_score_sql_does_not_confuse_adapter_placeholders():
    class Rows(Result):
        def fetchall(self):
            return self.row

    class ScoreDB:
        def execute(self, sql, params=None):
            assert sql.count("?") == len(params or [])
            if "SELECT project_id,question_id" in sql:
                return Result(("project_1", "question_1"))
            if "SELECT id FROM hive_missions" in sql:
                return Rows([("mission_1",)])
            if "FROM hive_claims WHERE mission_id=ANY" in sql:
                return Rows([
                    ("supported", "https://doi.org/10.1002/one", {"evidence_class":"direct"}, {}, "observation", "measured"),
                    ("supported", "https://doi.org/10.3389/two", {}, {}, "finding", "evidence"),
                    ("supported", "https://example.org/three", {}, {}, "finding", "evidence"),
                ])
            raise AssertionError(sql)

    score = controller._score_question(ScoreDB(), "question_1", "mission_1")
    assert score["status"] == "covered"
    assert score["domains"] == 3


def test_child_mission_resolves_to_root_controller_anchor():
    class AnchorDB:
        def execute(self, sql, params=None):
            if "SELECT m.project_id,q.parent_id" in sql:
                return Result(("project_1", "root_question"))
            if "q.parent_id IS NULL" in sql:
                return Result(("root_mission",))
            raise AssertionError(sql)

    assert controller._controller_anchor(AnchorDB(), "child_mission") == "root_mission"


def test_root_mission_remains_its_own_controller_anchor():
    class RootDB:
        def execute(self, sql, params=None):
            return Result(("project_1", None))

    assert controller._controller_anchor(RootDB(), "root_mission") == "root_mission"


def test_question_sync_does_not_reactivate_evidence_found():
    class SyncDB:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT project_id FROM hive_missions"):
                return Result(("project_1",))
            if normalized.startswith("SELECT id,question,priority"):
                assert "'evidence_found'" not in normalized
                return QueryResult([])
            if normalized.startswith("SELECT id,title,frontier_type"):
                return QueryResult([])
            return Result()

    class QueryResult(Result):
        def fetchall(self):
            return self.row

    assert controller._sync_branches(SyncDB(), "root_mission") == 0


def test_promote_supported_project_findings_repairs_missing_handoff():
    class Rows(Result):
        def fetchall(self):
            return self.row

    class PromoteDB:
        def __init__(self):
            self.insert_sql = ""

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT project_id FROM hive_missions"):
                return Result(("project_1",))
            if normalized.startswith("INSERT INTO hive_findings"):
                self.insert_sql = normalized
                assert params == ["project_1"]
                return Rows([(1,), (2,)])
            raise AssertionError(normalized)

    db = PromoteDB()
    assert controller._promote_supported_project_findings(db, "mission_1") == 2
    assert "integrity_status='supported'" in db.insert_sql
    assert "COALESCE(c.is_garbage,FALSE)=FALSE" in db.insert_sql


def test_stale_running_action_is_requeued_after_worker_restart():
    class Rows(Result):
        def fetchall(self):
            return self.row

    class RecoveryDB:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params or []))
            if normalized.startswith("UPDATE hive_research_actions SET status='retrying'"):
                assert params == ["mission_1", controller.STALE_ACTION_MINUTES]
                return Rows([(9, "branch_1")])
            return Result()

    db = RecoveryDB()
    assert controller._recover_stale_actions(db, "mission_1") == 1
    updates = [sql for sql, _ in db.calls if sql.startswith("UPDATE")]
    assert any("status='retrying'" in sql and "RETURNING id,branch_id" in sql for sql in updates)
    assert any("Controller worker restarted" in sql for sql in updates)


def test_global_stale_recovery_runs_before_scheduler_eligibility():
    class Rows(Result):
        def fetchall(self):
            return self.row

    class RecoveryDB:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params or []))
            if normalized.startswith("UPDATE hive_research_actions SET status='retrying'"):
                assert "mission_id=?" not in normalized
                assert "status='running'" in normalized
                assert "COALESCE(heartbeat_at,started_at) <" in normalized
                assert params == [controller.STALE_ACTION_MINUTES]
                return Rows([(51, "stale_branch")])
            return Result()

    db = RecoveryDB()
    assert controller._recover_stale_actions(db) == 1
    assert any("status='retrying'" in sql for sql, _ in db.calls)


def test_global_stale_recovery_preserves_live_running_actions():
    class EmptyRows(Result):
        def fetchall(self):
            return []

    class LiveDB:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.calls.append((normalized, params or []))
            if normalized.startswith("UPDATE hive_research_actions SET status='retrying'"):
                return EmptyRows()
            raise AssertionError("A live action must not be updated")

    db = LiveDB()
    assert controller._recover_stale_actions(db) == 0
    assert len(db.calls) == 1
    assert "COALESCE(heartbeat_at,started_at) <" in db.calls[0][0]
    assert "status='running'" in db.calls[0][0]


def test_recovery_is_single_atomic_compare_and_set():
    class Rows(Result):
        def fetchall(self):
            return self.row

    class StateDB:
        def __init__(self):
            self.actions = {1: "complete", 2: "running"}
            self.branches = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("UPDATE hive_research_actions SET status='retrying'"):
                # Action 1 represents a concurrent completion before the atomic
                # predicate executes; only the still-running stale row returns.
                assert "WHERE status='running'" in normalized
                assert "RETURNING id,branch_id" in normalized
                self.actions[2] = "retrying"
                return Rows([(2, "branch_2")])
            if normalized.startswith("UPDATE hive_research_branches"):
                self.branches.append(params[0])
                return Result()
            raise AssertionError(normalized)

    db = StateDB()
    assert controller._recover_stale_actions(db) == 1
    assert db.actions == {1: "complete", 2: "retrying"}
    assert db.branches == ["branch_2"]
