import time
import uuid

from pydantic import ValidationError

from .company import resolve_company
from .config import Settings
from .deepseek import DeepSeekClient, DeepSeekError
from .intent import score_intent
from .quality import assess_quality
from .repository import Repository
from .schemas import FACT_FIELDS, AnalysisInput, AnalysisResult, CompanyResolution, Lead


class Workflow:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        client=None,
        company_resolver=None,
        progress=None,
    ):
        self.settings = settings
        self.repository = repository
        self.client = client
        self.company_resolver = company_resolver or resolve_company
        self.progress = progress or (lambda message: None)

    def analyze(self, input: AnalysisInput) -> AnalysisResult:
        start = time.monotonic()
        deadline = start + self.settings.analysis_timeout
        result = AnalysisResult(
            run_id=str(uuid.uuid4()),
            run_status="running",
            versions={"model": self.settings.model, "prompt": "1.0", "scoring": "1.0", "quality": "2.0"},
        )
        client = self.client or DeepSeekClient(self.settings)
        before = getattr(client, "request_count", 0)
        try:
            self.progress("提取事实与客户意向信号")
            extraction = client.extract(input, deadline)
            result.extraction = extraction
            intent = score_intent(extraction)
            result.intent = intent
            self.progress("核实企业主体与候选来源")
            if time.monotonic() >= deadline:
                company = CompanyResolution(status="unavailable", reason="分析时间预算已用尽")
            elif self.settings.search_enabled:
                company = self.company_resolver(
                    client, extraction, deadline, aliases=self.repository.aliases()
                )
            else:
                company = CompanyResolution(
                    status="skipped", reason="企业查询已关闭，主体需人工核实"
                )
            result.company = company
            quality = assess_quality(extraction, company, intent)
            result.quality = quality
            values = {
                name: fact.value for name, fact in extraction.facts.items() if name in FACT_FIELDS
            }
            if input.call_time is not None:
                values["call_time"] = input.call_time
            result.lead = Lead(
                **values,
                company_name=company.selected_name,
                intent_level=intent.level,
                intent_reason=intent.reason,
                demand_summary=extraction.demand_summary,
                next_action=intent.next_action,
                confidence_score=quality.confidence_score,
                need_human_review=quality.need_human_review,
            )
            result.review_reasons = quality.review_reasons
            result.run_status = (
                "partial"
                if company.status == "unavailable" or not self.settings.search_enabled
                else "succeeded"
            )
        except DeepSeekError as exc:
            result.run_status = "failed"
            result.error_code = exc.code
        except ValidationError:
            result.run_status = "failed"
            result.error_code = "invalid_output"
        finally:
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
            result.request_count = getattr(client, "request_count", 0) - before
            if self.client is None:
                client.close()
        self.progress("保存结果与审核依据")
        self.repository.save_run(input, result)
        return result
