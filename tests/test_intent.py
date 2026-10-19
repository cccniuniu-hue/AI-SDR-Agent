import pytest

from sdr_agent.schemas import Evidence, Extraction, Signal


def extracted(values=None, **kwargs):
    ev = Evidence(turn_id=1, speaker="customer", quote="原句")
    return Extraction(
        signals={k: Signal(value=v, evidence=[ev]) for k, v in (values or {}).items()}, **kwargs
    )


@pytest.mark.parametrize(
    "values, expected",
    [
        ({"pain": 1, "timeline": 1, "budget": 1, "engagement": 1}, "high"),
        ({"pain": 1, "engagement": 0.5}, "medium"),
        ({"pain": 0, "timeline": 0, "budget": 0, "engagement": 0}, "low"),
        ({"engagement": 0.5}, None),
        ({}, None),
    ],
)
def test_intent_rules(values, expected):
    from sdr_agent.intent import score_intent

    result = score_intent(extracted(values))
    assert result.level == expected
    assert result.reason
    assert result.next_action


def test_explicit_refusal_can_have_high_evidence_confidence():
    from sdr_agent.intent import score_intent

    result = score_intent(
        extracted(
            refusal=True, refusal_evidence=[Evidence(turn_id=1, speaker="customer", quote="不需要")]
        )
    )
    assert result.level == "low"
    assert result.evidence_score == 0.9


def test_conflict_and_unknown_stay_unknown():
    from sdr_agent.intent import score_intent

    assert score_intent(extracted({"pain": 1, "timeline": 1}, conflicted=True)).level is None
    result = score_intent(extracted())
    assert result.score_lower is None
    assert result.score_upper is None
