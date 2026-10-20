from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

IntentLevel = Literal["high", "medium", "low"]
Speaker = Literal["customer", "sales", "unknown"]
SignalName = Literal["pain", "timeline", "budget", "engagement", "scale"]
Count = Annotated[int, Field(strict=True, ge=0)]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
FACT_FIELDS = (
    "industry",
    "area",
    "contact_name",
    "contact_title",
    "phone",
    "mendian",
    "shejishi",
    "call_time",
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=True)


def aware_time(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("通话时间必须包含时区")
    return parsed.isoformat()


class Lead(Model):
    company_name: str | None = None
    industry: str | None = None
    area: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    phone: str | None = None
    intent_level: IntentLevel | None = None
    intent_reason: str | None = None
    demand_summary: str | None = None
    mendian: Count | None = None
    shejishi: Count | None = None
    call_time: str | None = None
    next_action: str | None = None
    confidence_score: Score = 0.0
    need_human_review: bool = True

    @field_validator("*", mode="before")
    @classmethod
    def normalize_blank(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value

    _time = field_validator("call_time")(aware_time)


class AnalysisInput(Model):
    transcript: str = Field(min_length=1, max_length=20000)
    call_time: str | None = None

    @field_validator("transcript", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    _time = field_validator("call_time")(aware_time)


class Turn(Model):
    turn_id: int
    speaker: Speaker
    text: str


class Evidence(Model):
    turn_id: int
    speaker: Speaker
    quote: str = Field(min_length=1)


class Fact(Model):
    value: str | Count | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    approximate: bool = False


class Signal(Model):
    value: Literal[0, 0.5, 1] | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    conflict: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("信号分值不能是布尔值")
        return value


class Extraction(Model):
    facts: dict[str, Fact] = Field(default_factory=dict)
    company_mention: str | None = None
    company_evidence: list[Evidence] = Field(default_factory=list)
    signals: dict[SignalName, Signal] = Field(default_factory=dict)
    refusal: bool = False
    refusal_evidence: list[Evidence] = Field(default_factory=list)
    conflicted: bool = False
    role_uncertain: bool = False
    demand_summary: str | None = None
    summary_evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    model_notes: list[str] = Field(default_factory=list)
    fact_evidence_ratio: Score | None = None


class SearchResult(Model):
    url: str
    title: str = ""
    content: str = ""
    retrieved_at: str = ""


class CompanyCandidate(Model):
    name: str
    source_url: str
    quote: str = ""
    relation_quote: str = ""
    relation_source_url: str | None = None
    scope_evidence: list[SearchResult] = Field(default_factory=list, max_length=5)
    area_match: bool = False
    industry_match: bool = False
    relation_supported: bool = False
    score: Score = 0.0


class CompanyResolution(Model):
    selected_name: str | None = None
    candidates: list[CompanyCandidate] = Field(default_factory=list)
    status: Literal["confirmed", "unresolved", "ambiguous", "unavailable", "skipped"] = "unresolved"
    evidence: list[SearchResult] = Field(default_factory=list)
    quality_score: Score | None = None
    reason: str = "尚无足够证据确认企业主体"


class IntentDecision(Model):
    level: IntentLevel | None = None
    reason: str
    signals: dict[SignalName, Signal] = Field(default_factory=dict)
    score_lower: float | None = None
    score_upper: float | None = None
    evidence_score: Score | None = None
    next_action: str


class QualityAssessment(Model):
    confidence_score: Score = 0.0
    need_human_review: bool = True
    review_reasons: list[str] = Field(default_factory=list)
    company_score: Score | None = None
    intent_evidence_score: Score | None = None
    fact_evidence_ratio: Score | None = None


class AnalysisResult(Model):
    run_id: str
    run_status: Literal["running", "succeeded", "partial", "failed"]
    lead: Lead | None = None
    extraction: Extraction | None = None
    company: CompanyResolution | None = None
    intent: IntentDecision | None = None
    quality: QualityAssessment | None = None
    review_reasons: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    error_code: str | None = None
    elapsed_ms: int = 0
    request_count: int = 0
