import pytest
from pydantic import ValidationError


def test_export_keeps_null_and_exactly_fifteen_fields():
    from sdr_agent.schemas import Lead

    data = Lead(need_human_review=True).model_dump(mode="json")
    assert len(data) == 15
    assert data["company_name"] is None
    assert data["phone"] is None
    assert data["need_human_review"] is True


@pytest.mark.parametrize("value", [-1, True, 1.2, "15"])
def test_people_count_rejects_invalid_values(value):
    from sdr_agent.schemas import Lead

    with pytest.raises(ValidationError):
        Lead(shejishi=value)


def test_empty_strings_normalize_but_phone_stays_masked():
    from sdr_agent.schemas import Lead

    lead = Lead(company_name="  ", phone="138xxxx1234", mendian=0)
    assert lead.company_name is None
    assert lead.phone == "138xxxx1234"
    assert lead.mendian == 0


def test_export_rejects_extra_fields_and_naive_time():
    from sdr_agent.schemas import Lead

    with pytest.raises(ValidationError):
        Lead(evidence=[])
    with pytest.raises(ValidationError):
        Lead(call_time="2026-10-19T09:00:00")


def test_input_has_size_limit_and_keeps_optional_date():
    from sdr_agent.schemas import AnalysisInput

    with pytest.raises(ValidationError):
        AnalysisInput(transcript="   ")
    with pytest.raises(ValidationError):
        AnalysisInput(transcript="字" * 20001)
    assert AnalysisInput(transcript="客户：你好").call_time is None


@pytest.mark.parametrize(
    "payload",
    [{"need_human_review": "false"}, {"confidence_score": "0.9"}, {"confidence_score": True}],
)
def test_system_fields_do_not_coerce_strings_or_booleans(payload):
    from sdr_agent.schemas import Lead

    with pytest.raises(ValidationError):
        Lead(**payload)


def test_boolean_is_not_a_signal_score():
    from sdr_agent.schemas import Signal

    with pytest.raises(ValidationError):
        Signal(value=True)
