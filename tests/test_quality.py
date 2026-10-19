from sdr_agent.schemas import CompanyResolution, Extraction, IntentDecision


def test_company_ambiguity_requires_review_even_for_high_intent():
    from sdr_agent.quality import assess_quality

    result = assess_quality(
        Extraction(fact_evidence_ratio=1),
        CompanyResolution(status="ambiguous"),
        IntentDecision(level="high", reason="预算和时间明确", evidence_score=1, next_action="演示"),
    )
    assert result.need_human_review
    assert result.confidence_score is None
    assert result.review_reasons


def test_low_intent_is_not_low_confidence():
    from sdr_agent.quality import assess_quality

    result = assess_quality(
        Extraction(fact_evidence_ratio=1),
        CompanyResolution(status="confirmed", selected_name="测试公司", quality_score=1),
        IntentDecision(level="low", reason="明确拒绝", evidence_score=0.9, next_action="尊重拒绝"),
    )
    assert result.confidence_score == 0.96
    assert not result.need_human_review
