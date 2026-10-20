import pytest

from sdr_agent.extraction import split_turns, validate_extraction
from sdr_agent.schemas import AnalysisInput, Evidence, Extraction, Fact, Signal


def ev(turn_id, quote, speaker="customer"):
    return Evidence(turn_id=turn_id, speaker=speaker, quote=quote)


def test_company_context_keeps_explicit_self_alias_from_the_validated_customer_turn():
    text = "我们叫清木，也叫青木，在杭州做定制家居。"
    result = validate_extraction(
        Extraction(company_mention="清木", company_evidence=[ev(1, "我们叫清木")]),
        AnalysisInput(transcript="客户：" + text),
    )
    assert result.company_mention == "清木"
    assert result.company_evidence[0].quote == text
    from sdr_agent.company import company_mentions

    assert company_mentions(result) == ["清木", "青木"]


def test_split_turns_keeps_unlabeled_text_unknown():
    turns = split_turns("通话开始\n销售：您是张总吗？\n客户：对，是我。\n我们在杭州。")
    assert [(t.turn_id, t.speaker, t.text) for t in turns] == [
        (1, "unknown", "通话开始"),
        (2, "sales", "您是张总吗？"),
        (3, "customer", "对，是我。\n我们在杭州。"),
    ]


def test_customer_fact_valid_but_sales_claim_and_forged_quote_are_null():
    data = AnalysisInput(transcript="销售：你们在上海，有20家门店吧？\n客户：我们在杭州。")
    extraction = Extraction(
        facts={
            "area": Fact(value="杭州", evidence=[ev(2, "我们在杭州。")]),
            "mendian": Fact(value=20, evidence=[ev(1, "有20家门店", "sales")]),
            "contact_title": Fact(value="总经理", evidence=[ev(2, "我是总经理")]),
        }
    )
    result = validate_extraction(extraction, data)
    assert result.facts["area"].value == "杭州"
    assert result.facts["mendian"].value is None
    assert result.facts["contact_title"].value is None
    assert result.fact_evidence_ratio == 1 / 3
    assert len(result.warnings) >= 2


def test_contact_name_can_use_sales_question_only_with_immediate_explicit_agreement():
    data = AnalysisInput(transcript="销售：您是张总吗？\n客户：对，是我。")
    fact = Fact(value="张总", evidence=[ev(1, "您是张总吗？", "sales"), ev(2, "对，是我。")])
    assert (
        validate_extraction(Extraction(facts={"contact_name": fact}), data)
        .facts["contact_name"]
        .value
        == "张总"
    )
    no = AnalysisInput(transcript="销售：您是张总吗？\n客户：你先说。")
    fact.evidence[1].quote = "你先说。"
    assert (
        validate_extraction(Extraction(facts={"contact_name": fact}), no)
        .facts["contact_name"]
        .value
        is None
    )


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("是的，你讲。", "张总"), ("你讲。", None), ("是的，你讲。不过我不是张总。", None)],
)
def test_contact_confirmation_accepts_pdf_reply_but_not_ambiguous_or_denied_identity(
    reply, expected
):
    question = "您好，请问是张总吗？我是酷家乐的小王。"
    data = AnalysisInput(transcript=f"销售：{question}\n客户：{reply}")
    fact = Fact(
        value="张总",
        evidence=[ev(1, "请问是张总吗？", "sales"), ev(2, reply)],
    )
    result = validate_extraction(Extraction(facts={"contact_name": fact}), data)
    assert result.facts["contact_name"].value == expected


def test_approximate_counts_are_null_even_if_model_forgets_flag():
    data = AnalysisInput(transcript="客户：我们大概20家门店，设计师有十来个。")
    extraction = Extraction(
        facts={
            "mendian": Fact(value=20, evidence=[ev(1, "大概20家门店")]),
            "shejishi": Fact(value=10, evidence=[ev(1, "设计师有十来个")]),
        }
    )
    result = validate_extraction(extraction, data)
    assert result.facts["mendian"].value is None
    assert result.facts["shejishi"].value is None


def test_invalid_roles_and_hallucinated_values_do_not_become_evidence():
    data = AnalysisInput(transcript="销售：预算已经确认了。\n客户：我们在杭州。")
    extraction = Extraction(
        facts={"area": Fact(value="上海", evidence=[ev(2, "我们在杭州。")])},
        signals={"budget": Signal(value=1, evidence=[ev(1, "预算已经确认了。")])},
        company_mention="甲公司",
        company_evidence=[ev(2, "我们在杭州。")],
        refusal=True,
        refusal_evidence=[ev(1, "预算已经确认了。", "sales")],
        demand_summary="客户预算充足",
        summary_evidence=[ev(1, "预算已经确认了。", "sales")],
    )
    result = validate_extraction(extraction, data)
    assert result.facts["area"].value is None
    assert result.signals["budget"].value is None
    assert result.company_mention is None
    assert result.refusal is False
    assert result.demand_summary is None


def test_metadata_time_is_authoritative_and_unknown_turns_are_uncertain():
    data = AnalysisInput(transcript="我们在杭州。", call_time="2026-10-19T10:00:00+08:00")
    result = validate_extraction(
        Extraction(facts={"area": Fact(value="杭州", evidence=[ev(1, "我们在杭州。", "unknown")])}),
        data,
    )
    assert result.facts["area"].value is None
    assert result.facts["call_time"].value == data.call_time
    assert result.role_uncertain


def test_valid_exact_chinese_count_and_customer_signals_survive():
    data = AnalysisInput(transcript="客户：我们有二十家门店，预算已经批了，下个月上线。")
    result = validate_extraction(
        Extraction(
            facts={"mendian": Fact(value=20, evidence=[ev(1, "我们有二十家门店")])},
            signals={"budget": Signal(value=1, evidence=[ev(1, "预算已经批了")])},
        ),
        data,
    )
    assert result.facts["mendian"].value == 20
    assert result.signals["budget"].value == 1


def test_counts_must_refer_to_the_correct_field():
    data = AnalysisInput(transcript="客户：我们有3家门店，20位设计师。")
    result = validate_extraction(
        Extraction(
            facts={
                "mendian": Fact(value=20, evidence=[ev(1, "我们有3家门店，20位设计师。")]),
                "shejishi": Fact(value=3, evidence=[ev(1, "我们有3家门店，20位设计师。")]),
            }
        ),
        data,
    )
    assert result.facts["mendian"].value is None
    assert result.facts["shejishi"].value is None


def test_refusal_requires_actual_refusal_words_not_arbitrary_customer_quote():
    data = AnalysisInput(transcript="客户：我们在杭州。我现在在忙，稍后联系。")
    result = validate_extraction(
        Extraction(refusal=True, refusal_evidence=[ev(1, "我现在在忙，稍后联系。")]), data
    )
    assert result.refusal is False


def test_metadata_time_is_included_in_evidence_denominator():
    data = AnalysisInput(transcript="客户：你好。", call_time="2026-10-19T10:00:00+08:00")
    result = validate_extraction(Extraction(), data)
    assert result.fact_evidence_ratio == 1


@pytest.mark.parametrize(
    "field,value,text",
    [
        ("area", "杭州", "我们不在杭州，我们在上海。"),
        ("mendian", 3, "我们有两三家门店。"),
        ("shejishi", 20, "设计师二十多人。"),
        ("mendian", 3, "我们有三家供应商。"),
        ("phone", 13800138000, "电话是13800138000。"),
    ],
)
def test_semantically_unsupported_facts_are_null(field, value, text):
    result = validate_extraction(
        Extraction(facts={field: Fact(value=value, evidence=[ev(1, text)])}),
        AnalysisInput(transcript="客户：" + text),
    )
    assert result.facts[field].value is None


def test_old_refusal_cannot_override_later_purchase_request():
    text = "之前不需要，但现在准备采购，请安排演示。"
    result = validate_extraction(
        Extraction(refusal=True, refusal_evidence=[ev(1, "之前不需要")]),
        AnalysisInput(transcript="客户：" + text),
    )
    assert not result.refusal
    assert result.conflicted


def test_header_is_not_unattributed_customer_speech():
    result = validate_extraction(
        Extraction(), AnalysisInput(transcript="通话记录\n客户：希望下个月采购。")
    )
    assert not result.role_uncertain


def test_model_optional_missing_notes_are_separate_from_validator_warnings():
    result = validate_extraction(
        Extraction(warnings=["未提供电话"]), AnalysisInput(transcript="客户：你好。")
    )
    assert result.warnings == []
    assert result.model_notes == ["未提供电话"]


def test_cropped_quote_cannot_hide_approximate_count():
    result = validate_extraction(
        Extraction(facts={"mendian": Fact(value=20, evidence=[ev(1, "20家门店")])}),
        AnalysisInput(transcript="客户：我们大概20家门店。"),
    )
    assert result.facts["mendian"].value is None


def test_whole_quote_cannot_hide_stance_reversal():
    text = "之前不需要，但现在准备采购，请安排演示。"
    result = validate_extraction(
        Extraction(refusal=True, refusal_evidence=[ev(1, text)]),
        AnalysisInput(transcript="客户：" + text),
    )
    assert not result.refusal


def test_honorific_is_not_an_asserted_job_title():
    result = validate_extraction(
        Extraction(facts={"contact_title": Fact(value="经理", evidence=[ev(1, "我叫陈经理")])}),
        AnalysisInput(transcript="客户：我叫陈经理。"),
    )
    assert result.facts["contact_title"].value is None
