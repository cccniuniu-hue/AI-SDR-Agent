from .schemas import CompanyResolution, Extraction, IntentDecision, QualityAssessment


def assess_quality(
    extraction: Extraction, company: CompanyResolution, intent: IntentDecision
) -> QualityAssessment:
    parts = [company.quality_score, intent.evidence_score, extraction.fact_evidence_ratio]
    score = (
        round(sum(v * w for v, w in zip(parts, (0.45, 0.40, 0.15))), 2)
        if all(v is not None for v in parts)
        else None
    )
    reasons = []
    if company.selected_name is None:
        reasons.append("企业主体尚未确认：" + company.reason)
    if intent.level is None:
        reasons.append("客户意向证据不足或存在冲突")
    if company.status == "unavailable":
        reasons.append("企业查询服务不可用，结果已降级")
    if extraction.conflicted or extraction.role_uncertain:
        reasons.append("需要核对对话角色或冲突信息")
    reasons.extend(extraction.warnings)
    if score is None or score < 0.8:
        reasons.append("综合可靠程度未知或低于审核阈值 0.80")
    return QualityAssessment(
        confidence_score=score,
        need_human_review=bool(reasons),
        review_reasons=list(dict.fromkeys(reasons)),
        company_score=parts[0],
        intent_evidence_score=parts[1],
        fact_evidence_ratio=parts[2],
    )
