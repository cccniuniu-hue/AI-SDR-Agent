from .schemas import CompanyResolution, Extraction, IntentDecision, QualityAssessment


def assess_quality(
    extraction: Extraction, company: CompanyResolution, intent: IntentDecision
) -> QualityAssessment:
    parts = [company.quality_score, intent.evidence_score, extraction.fact_evidence_ratio]
    # Unknown evidence earns no contribution; keep the fixed weights and expose missing parts.
    score = round(
        sum((v if v is not None else 0.0) * w for v, w in zip(parts, (0.45, 0.40, 0.15))), 2
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
    if any(v is None for v in parts):
        reasons.append("评分分项存在未知值，未知分项按 0 贡献计算，需人工核查")
    if score < 0.8:
        reasons.append("综合可靠程度低于审核阈值 0.80")
    return QualityAssessment(
        confidence_score=score,
        need_human_review=bool(reasons),
        review_reasons=list(dict.fromkeys(reasons)),
        company_score=parts[0],
        intent_evidence_score=parts[1],
        fact_evidence_ratio=parts[2],
    )
