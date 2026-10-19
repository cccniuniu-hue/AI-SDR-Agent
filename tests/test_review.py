import pytest
from test_repository import seed


def test_review_keeps_machine_result_and_logs_change(tmp_path):
    from sdr_agent.repository import Repository
    from sdr_agent.review import ReviewService

    repo = Repository(tmp_path / "test.sqlite3")
    seed(repo)
    ReviewService(repo).apply_batch(
        [
            {
                "run_id": "one",
                "expected_version": 1,
                "updates": {"intent_level": "medium"},
                "approve": True,
                "accept_unknown": True,
            }
        ],
        "测试审核人",
        "补充确认需求；接受企业主体未知",
    )
    result = repo.get_run("one")
    assert result["original"]["intent_level"] == "low"
    assert result["current"]["intent_level"] == "medium"
    assert result["current"]["need_human_review"] is False
    assert result["version"] == 2
    assert repo.history("one")[0]["reviewer"] == "测试审核人"


def test_batch_conflict_rolls_back_all_changes(tmp_path):
    from sdr_agent.repository import Repository
    from sdr_agent.review import ReviewError, ReviewService

    repo = Repository(tmp_path / "test.sqlite3")
    seed(repo, "one")
    seed(repo, "two")
    changes = [
        {"run_id": "one", "expected_version": 1, "updates": {"intent_level": "high"}},
        {"run_id": "two", "expected_version": 99, "updates": {"intent_level": "high"}},
    ]
    with pytest.raises(ReviewError):
        ReviewService(repo).apply_batch(changes, "审核", "复核")
    assert repo.get_run("one")["version"] == 1
    assert not repo.history("one")


def test_review_requires_reason_and_does_not_edit_failed_runs(tmp_path):
    from sdr_agent.repository import Repository
    from sdr_agent.review import ReviewError, ReviewService

    repo = Repository(tmp_path / "test.sqlite3")
    seed(repo)
    with pytest.raises(ReviewError):
        ReviewService(repo).apply_batch(
            [{"run_id": "one", "expected_version": 1, "updates": {}}], "", ""
        )


def test_unknown_company_requires_explicit_acceptance(tmp_path):
    from sdr_agent.repository import Repository
    from sdr_agent.review import ReviewError, ReviewService

    repo = Repository(tmp_path / "test.sqlite3")
    seed(repo)
    with pytest.raises(ReviewError):
        ReviewService(repo).apply_batch(
            [{"run_id": "one", "expected_version": 1, "updates": {}, "approve": True}],
            "审核人",
            "已检查",
        )
    assert repo.get_run("one")["version"] == 1
