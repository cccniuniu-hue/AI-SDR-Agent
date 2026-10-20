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


def test_intent_reason_uses_multiline_evidence_with_single_semicolon_separators():
    from sdr_agent.intent import score_intent

    quotes = {
        "pain": "现在用的是别的软件，出图慢，而且跟工厂对接经常出错。",
        "timeline": "最好下个月就能试一下；",
        "budget": "预算的话，一年几万块我们能接受。； ",
        "engagement": "行，你加这个手机号就行",
        "scale": "设计部大概 15 个人。",
    }
    extraction = Extraction(
        signals={
            name: Signal(
                value=0.5 if name == "engagement" else 1,
                evidence=[
                    Evidence(turn_id=1, speaker="customer", quote=quote),
                ],
            )
            for name, quote in quotes.items()
        }
    )
    result = score_intent(extraction)
    assert result.reason == (
        "明确痛点、时间或预算及推进信号支持高意向。\n"
        "依据：\n"
        "痛点：现在用的是别的软件，出图慢，而且跟工厂对接经常出错；\n"
        "时间：最好下个月就能试一下；\n"
        "预算：预算的话，一年几万块我们能接受；\n"
        "行动：行，你加这个手机号就行；\n"
        "规模：设计部大概 15 个人"
    )
    assert result.signals["budget"].evidence[0].quote == quotes["budget"]
    assert result.score_lower == result.score_upper == 90


def test_multiline_reason_keeps_unknown_signals_separate():
    from sdr_agent.intent import score_intent

    result = score_intent(extracted({"pain": 1}))
    assert "\n依据：\n痛点：原句\n未知：时间、预算、行动、规模。" in result.reason
