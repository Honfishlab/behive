from behive.engine.dispatch_drain import pause_dispatch, restore_dispatch


class Result:
    def __init__(self, value=0, rowcount=0): self.value, self.rowcount = value, rowcount
    def fetchone(self): return (self.value,)


class DB:
    def __init__(self): self.calls=[]; self.counts=iter((2,2)); self.commits=0
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "COUNT(*)" in sql: return Result(next(self.counts))
        if "UPDATE hive_projects" in sql: return Result(rowcount=5)
        return Result()
    def commit(self): self.commits += 1


def test_pause_preserves_active_jobs_and_only_updates_projects():
    db=DB(); result=pause_dispatch("deploy", db)
    assert result == {"drain_id":"deploy","paused_projects":5,"active_before":2,"active_after":2}
    assert not any("UPDATE hive_research_actions" in sql for sql,_ in db.calls)
    assert db.commits == 1


def test_restore_reinstates_snapshot_policy():
    db=DB(); result=restore_dispatch("deploy", db)
    assert result == {"drain_id":"deploy","restored_projects":5}
    assert any("agent_policy=s.agent_policy" in sql for sql,_ in db.calls)
    assert db.commits == 1
