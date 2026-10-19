import pytest

from sdr_agent.schemas import CompanyResolution, Extraction, IntentDecision


def test_company_ambiguity_requires_review_even_for_high_intent():
    from sdr_agent.quality import assess_quality

    result = assess_quality(
        Extraction(fact_evidence_ratio=1),
        CompanyResolution(status="ambiguous"),
        IntentDecision(level="high", reason="预算和时间明确", evidence_score=1, next_action="演示"),
    )
    assert result.need_human_review
    assert result.confidence_score == 0.55
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


@pytest.mark.parametrize(
    "company_score,intent_score,fact_ratio,expected",
    [(None, None, None, 0.0), (None, 1, 6 / 7, 0.53), (1, 1, None, 0.85), (0, 0, 0, 0.0)],
)
def test_unknown_parts_contribute_zero_without_renormalizing_weights(
    company_score, intent_score, fact_ratio, expected
):
    from sdr_agent.quality import assess_quality

    result = assess_quality(
        Extraction(fact_evidence_ratio=fact_ratio),
        CompanyResolution(selected_name="测试公司", status="confirmed", quality_score=company_score),
        IntentDecision(level="high", reason="测试依据", evidence_score=intent_score, next_action="核查"),
    )
    assert result.confidence_score == expected
    assert result.need_human_review
    if None in (company_score, intent_score, fact_ratio):
        assert any("分项" in reason and "未知" in reason for reason in result.review_reasons)


def test_fully_supported_result_can_reach_one():
    from sdr_agent.quality import assess_quality

    result = assess_quality(
        Extraction(fact_evidence_ratio=1),
        CompanyResolution(selected_name="测试公司", status="confirmed", quality_score=1),
        IntentDecision(level="high", reason="测试依据", evidence_score=1, next_action="演示"),
    )
    assert result.confidence_score == 1.0
    assert not result.need_human_review
