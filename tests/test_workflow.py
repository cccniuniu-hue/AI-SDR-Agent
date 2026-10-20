import time

import pytest

from sdr_agent.config import Settings
from sdr_agent.repository import Repository
from sdr_agent.schemas import AnalysisInput, CompanyResolution, Evidence, Extraction, Fact, Signal


class Client:
    request_count = 1

    def extract(self, input, deadline):
        evidence = Evidence(
            turn_id=1,
            speaker="customer",
            quote="我们在杭州，出图慢，下个月试用，预算五万元，请安排演示",
        )
        return Extraction(
            facts={"area": Fact(value="杭州", evidence=[evidence])},
            fact_evidence_ratio=1,
            signals={
                k: Signal(value=1, evidence=[evidence])
                for k in ["pain", "timeline", "budget", "engagement"]
            },
        )


@pytest.mark.parametrize(
    ("analysis_timeout", "company_timeout"),
    [(180, 2), (0.5, 30)],
)
def test_company_phase_has_its_own_bounded_budget(tmp_path, analysis_timeout, company_timeout):
    from sdr_agent.workflow import Workflow

    seen = []
    messages = []

    def resolver(client, extraction, deadline, **kwargs):
        seen.append(deadline - time.monotonic())
        assert callable(kwargs["progress"])
        return CompanyResolution(status="unavailable", reason="企业核实超时")

    result = Workflow(
        Settings(analysis_timeout=analysis_timeout, company_timeout=company_timeout),
        Repository(tmp_path / "db.sqlite3"),
        client=Client(),
        company_resolver=resolver,
        progress=messages.append,
    ).analyze(AnalysisInput(transcript="客户：希望试用。"))
    assert 0 < seen[0] <= min(analysis_timeout, company_timeout)
    assert result.run_status == "partial"
    assert result.lead.intent_level == "high"
    assert result.lead.company_name is None
    assert any("最多" in message for message in messages)


def test_successful_workflow_persists_fifteen_fields(tmp_path):
    from sdr_agent.workflow import Workflow

    repo = Repository(tmp_path / "db.sqlite3")

    def resolver(*args, **kwargs):
        return CompanyResolution(selected_name="合成测试公司", status="confirmed", quality_score=1)

    service = Workflow(Settings(), repo, client=Client(), company_resolver=resolver)
    result = service.analyze(
        AnalysisInput(transcript="客户：我们在杭州，出图慢，下个月试用，预算五万元，请安排演示")
    )
    assert result.run_status == "succeeded"
    assert result.lead.intent_level == "high"
    assert result.lead.phone is None
    assert result.lead.confidence_score == 0.98
    assert repo.export([result.run_id])[0]["confidence_score"] == 0.98
    assert len(repo.export([result.run_id])[0]) == 15


def test_company_unavailable_is_partial_not_fabricated(tmp_path):
    from sdr_agent.workflow import Workflow

    def resolver(*args, **kwargs):
        return CompanyResolution(status="unavailable", reason="查询超时")

    service = Workflow(
        Settings(), Repository(tmp_path / "db.sqlite3"), client=Client(), company_resolver=resolver
    )
    result = service.analyze(
        AnalysisInput(transcript="客户：我们在杭州，出图慢，下个月试用，预算五万元，请安排演示")
    )
    assert result.run_status == "partial"
    assert result.lead.company_name is None
    assert result.lead.need_human_review
    assert result.lead.confidence_score == 0.53
    stored = service.repository.get_run(result.run_id)
    assert stored["analysis"]["quality"]["confidence_score"] == 0.53
    assert stored["analysis"]["quality"]["company_score"] is None
    assert stored["analysis"]["versions"]["quality"] == "2.0"
    assert service.repository.export([result.run_id])[0]["confidence_score"] == 0.53


def test_model_failure_is_not_a_successful_empty_lead(tmp_path):
    from sdr_agent.deepseek import DeepSeekError
    from sdr_agent.workflow import Workflow

    class BrokenClient:
        request_count = 1

        def extract(self, input, deadline):
            raise DeepSeekError("invalid_response", "模型返回格式错误")

    repo = Repository(tmp_path / "db.sqlite3")
    result = Workflow(Settings(), repo, client=BrokenClient()).analyze(
        AnalysisInput(transcript="客户：你好")
    )
    assert result.run_status == "failed"
    assert result.lead is None
    assert repo.get_run(result.run_id)["current"] is None


def test_budget_exhaustion_preserves_extracted_intent_without_search(tmp_path):
    from sdr_agent.workflow import Workflow

    def forbidden(*args, **kwargs):
        raise AssertionError("Expired budget must prevent company requests")

    result = Workflow(
        Settings(analysis_timeout=0),
        Repository(tmp_path / "db.sqlite3"),
        client=Client(),
        company_resolver=forbidden,
    ).analyze(AnalysisInput(transcript="客户：希望试用。"))
    assert result.run_status == "partial"
    assert result.lead.company_name is None
    assert result.lead.intent_level == "high"


def test_explicit_rerun_creates_new_record_and_preserves_approved_original(tmp_path):
    from sdr_agent.review import ReviewService
    from sdr_agent.workflow import Workflow

    repo = Repository(tmp_path / "db.sqlite3")
    service = Workflow(
        Settings(),
        repo,
        client=Client(),
        company_resolver=lambda *args, **kwargs: CompanyResolution(status="skipped"),
    )
    input = AnalysisInput(transcript="客户：希望试用。")
    first = service.analyze(input)
    ReviewService(repo).apply_batch(
        [
            {
                "run_id": first.run_id,
                "expected_version": 1,
                "updates": {},
                "approve": True,
                "accept_unknown": True,
            }
        ],
        "审核员",
        "已核查仍无法确认企业",
    )
    second = service.analyze(input)
    assert first.run_id != second.run_id
    assert repo.get_run(first.run_id)["review_status"] == "approved"
    assert repo.get_run(second.run_id)["review_status"] == "pending"
    assert len(repo.list_runs()) == 2


def test_absent_company_is_normal_success_with_review(tmp_path):
    from sdr_agent.workflow import Workflow

    result = Workflow(Settings(), Repository(tmp_path / "db.sqlite3"), client=Client()).analyze(
        AnalysisInput(transcript="客户：希望试用。")
    )
    assert result.company.status == "skipped"
    assert result.run_status == "succeeded"
    assert result.lead.need_human_review
