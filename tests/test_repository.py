from sdr_agent.schemas import AnalysisInput, AnalysisResult, Lead


def seed(repo, run_id="one"):
    data = AnalysisInput(transcript="客户：暂无计划")
    result = AnalysisResult(run_id=run_id, run_status="succeeded", lead=Lead(intent_level="low"))
    repo.save_run(data, result)
    return result


def test_saved_run_survives_reopen_and_exports_null(tmp_path):
    from sdr_agent.repository import Repository

    path = tmp_path / "test.sqlite3"
    repo = Repository(path)
    seed(repo)
    reopened = Repository(path)
    run = reopened.get_run("one")
    assert run["current"]["company_name"] is None
    assert run["version"] == 1
    assert len(reopened.export(["one"])[0]) == 15


def test_explicit_rerun_is_a_new_record(tmp_path):
    from sdr_agent.repository import Repository

    repo = Repository(tmp_path / "test.sqlite3")
    seed(repo, "one")
    seed(repo, "two")
    assert len(repo.list_runs()) == 2
