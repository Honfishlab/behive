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
    class ScoreDB:
        def execute(self, sql, params=None):
            assert sql.count("?") == len(params or [])
            return Result((3, 3, 3, 2, 1, 0))

    score = controller._score_question(ScoreDB(), "question_1", "mission_1")
    assert score["status"] == "covered"


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
