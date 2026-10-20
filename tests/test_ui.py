from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sdr_agent.repository import Repository
from sdr_agent.schemas import AnalysisInput, AnalysisResult, Lead


def history_app(
    tmp_path, monkeypatch, lead=None, failed=False, error_code="timeout", intent=None, company=None
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    repo = Repository(path)
    repo.save_run(
        AnalysisInput(transcript="客户：这是一条合成测试通话。"),
        AnalysisResult(
            run_id="ui-test-run",
            run_status="failed" if failed else "succeeded",
            lead=None if failed else (lead or Lead()),
            intent=intent,
            company=company,
            error_code=error_code if failed else None,
        ),
    )
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15).run()
    at.sidebar.radio[0].set_value("线索与审核").run()
    return at, repo


def by_label(elements, label):
    return next(item for item in elements if item.label == label)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-10-20T03:37:35.237240+00:00", "2026-10-20 11:37:35"),
        ("2026-10-19T20:37:35.237240+00:00", "2026-10-20 04:37:35"),
        ("2026-10-20T11:37:35.237240+08:00", "2026-10-20 11:37:35"),
        ("2026-10-19T23:37:35-04:00", "2026-10-20 11:37:35"),
    ],
)
def test_timestamp_label_converts_to_beijing_without_fraction_or_offset(stored, expected):
    from sdr_agent.ui import timestamp_label

    assert timestamp_label(stored) == expected


def test_history_uses_beijing_time_without_rewriting_stored_times(tmp_path, monkeypatch):
    from sdr_agent.review import ReviewService

    stored = "2026-10-19T20:37:35.237240+00:00"
    expected = "2026-10-20 04:37:35"
    call_time = "2026-10-18T06:15:45.123456+00:00"
    monkeypatch.setattr("sdr_agent.repository.now", lambda: stored)
    monkeypatch.setattr("sdr_agent.review.now", lambda: stored)
    at, repo = history_app(tmp_path, monkeypatch, lead=Lead(call_time=call_time))
    ReviewService(repo).apply_batch(
        [{"run_id": "ui-test-run", "expected_version": 1, "updates": {}}],
        "时间测试审核员",
        "核对时间展示",
    )
    at.run()
    assert not at.exception
    assert at.dataframe[0].value.iloc[0]["创建时间"] == expected
    assert expected[:16] in by_label(at.selectbox, "查看并审核一条线索").options[0]
    assert any("创建于 " + expected in item.value for item in at.caption)
    assert any("时间测试审核员" in item.label and expected in item.label for item in at.expander)
    assert repo.get_run("ui-test-run")["created_at"] == stored
    assert repo.history("ui-test-run")[0]["created_at"] == stored
    assert repo.export(["ui-test-run"])[0]["call_time"] == call_time


def enter_company(at, name):
    picker = by_label(at.selectbox, "企业主体")
    assert picker.proto.accept_new_options
    # AppTest needs the frontend's newly entered option to serialize its string value.
    if picker.value is not None and picker.value not in picker.options:
        picker.options.append(picker.value)
    if name is not None and name not in picker.options:
        picker.options.append(name)
    return picker.set_value(name)


def test_app_starts_without_key_and_explains_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15).run()
    assert not at.exception
    assert any("DEEPSEEK_API_KEY" in str(item.value) for item in at.warning)


def test_empty_input_is_blocked_without_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15).run()
    assert not at.exception
    next(b for b in at.button if b.label == "开始分析").click().run()
    assert not at.exception
    assert any("通话文本" in str(item.value) for item in at.error)


@pytest.mark.parametrize(
    "selected",
    [None, datetime(2026, 10, 18, 14, 37), datetime(2024, 2, 29, 23, 59), datetime(2026, 10, 18)],
)
def test_analysis_call_time_picker_submits_beijing_time_or_null(tmp_path, monkeypatch, selected):
    from sdr_agent.workflow import Workflow

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    requests = []

    def analyze(self, request):
        requests.append(request)
        result = AnalysisResult(
            run_id="time-picker-test",
            run_status="succeeded",
            lead=Lead(call_time=request.call_time),
        )
        self.repository.save_run(request, result)
        return result

    monkeypatch.setattr(Workflow, "analyze", analyze)
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15).run()
    picker = at.date_input(key="analysis_call_time_date")
    assert picker.value is None
    picker.set_value(selected.date() if selected else None)
    if selected:
        at.selectbox(key="analysis_call_time_hour").set_value(selected.hour)
        at.selectbox(key="analysis_call_time_minute").set_value(selected.minute)
    else:
        # A missing date still means unknown even if a user has touched the clock fields.
        at.selectbox(key="analysis_call_time_hour").set_value(23)
        at.selectbox(key="analysis_call_time_minute").set_value(59)
    by_label(at.text_area, "粘贴通话文本").set_value("客户：这是一条合成测试通话。")
    by_label(at.button, "开始分析").click().run()
    assert not at.exception
    expected = selected.isoformat() + "+08:00" if selected else None
    assert requests[0].call_time == expected
    repo = Repository(tmp_path / "ui.sqlite3")
    assert repo.export(["time-picker-test"])[0]["call_time"] == expected


@pytest.mark.parametrize(
    ("original", "selected", "expected"),
    [
        (None, datetime(2026, 10, 18, 14, 37), "2026-10-18T14:37:00+08:00"),
        ("2026-10-17T06:30:45.123456+00:00", None, None),
        (
            "2026-10-17T06:30:45.123456+00:00",
            datetime(2026, 10, 18, 14, 37),
            "2026-10-18T14:37:00+08:00",
        ),
        (
            "2026-10-17T06:30:45.123456+00:00",
            datetime(2026, 10, 17, 14, 30),
            "2026-10-17T06:30:45.123456+00:00",
        ),
    ],
)
def test_review_call_time_picker_requires_confirmation_and_preserves_unchanged_precision(
    tmp_path, monkeypatch, original, selected, expected
):
    at, repo = history_app(tmp_path, monkeypatch, lead=Lead(call_time=original))
    key = "call_time_ui-test-run_1"
    picker = at.date_input(key=key + "_date")
    assert picker.value == (datetime(2026, 10, 17).date() if original else None)
    assert at.selectbox(key=key + "_hour").value == (14 if original else 0)
    assert at.selectbox(key=key + "_minute").value == (30 if original else 0)
    picker.set_value(selected.date() if selected else None)
    if selected:
        at.selectbox(key=key + "_hour").set_value(selected.hour)
        at.selectbox(key=key + "_minute").set_value(selected.minute)
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("核对通话时间")
    by_label(at.button, "预览本条修改").click().run()
    assert not at.exception
    assert repo.export(["ui-test-run"])[0]["call_time"] == original
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    assert repo.export(["ui-test-run"])[0]["call_time"] == expected
    key = "call_time_ui-test-run_2"
    assert at.date_input(key=key + "_date").value == (selected.date() if selected else None)
    if selected:
        assert at.selectbox(key=key + "_hour").value == selected.hour
        assert at.selectbox(key=key + "_minute").value == selected.minute


def test_review_requires_preview_then_confirmation(tmp_path, monkeypatch):
    at, repo = history_app(tmp_path, monkeypatch)
    assert not at.exception
    enter_company(at, "合成测试有限公司")
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("已核对原文及企业来源")
    by_label(at.button, "预览本条修改").click().run()
    assert not at.exception
    assert repo.get_run("ui-test-run")["current"]["company_name"] is None
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    saved = repo.get_run("ui-test-run")
    assert saved["current"]["company_name"] == "合成测试有限公司"
    assert saved["review_status"] == "edited"
    assert repo.history("ui-test-run")[0]["reviewer"] == "测试审核员"


@pytest.mark.parametrize("choice", ["合成候选乙有限公司", "人工输入主体有限公司", None])
def test_review_company_accepts_candidates_custom_input_and_clear(tmp_path, monkeypatch, choice):
    from sdr_agent.schemas import CompanyCandidate, CompanyResolution

    company = CompanyResolution(
        candidates=[
            CompanyCandidate(name=name, source_url="https://example.com/company")
            for name in ["合成候选甲有限公司", "合成候选乙有限公司", "合成候选甲有限公司", " "]
        ]
    )
    at, repo = history_app(
        tmp_path,
        monkeypatch,
        lead=Lead(company_name="原人工核实主体有限公司"),
        company=company,
    )
    picker = by_label(at.selectbox, "企业主体")
    assert picker.value == "原人工核实主体有限公司"
    assert picker.options == ["原人工核实主体有限公司", "合成候选甲有限公司", "合成候选乙有限公司"]
    original_analysis = repo.get_run("ui-test-run")["analysis"]
    enter_company(at, choice)
    assert by_label(at.selectbox, "企业主体").value == choice
    assert not at.exception
    assert repo.export(["ui-test-run"])[0]["company_name"] == "原人工核实主体有限公司"
    by_label(at.text_input, "审核人").set_value("企业审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("已逐项核对企业候选与原文")
    by_label(at.button, "预览本条修改").click().run()
    assert not at.exception
    assert repo.export(["ui-test-run"])[0]["company_name"] == "原人工核实主体有限公司"
    enter_company(at, choice)
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    saved = repo.get_run("ui-test-run")
    assert saved["current"]["company_name"] == choice
    assert saved["review_status"] == "edited"
    assert saved["analysis"] == original_analysis
    assert repo.history("ui-test-run")[0]["reviewer"] == "企业审核员"
    assert by_label(at.selectbox, "企业主体").value == choice


def test_review_company_does_not_default_to_first_candidate_when_unknown(tmp_path, monkeypatch):
    from sdr_agent.schemas import CompanyCandidate, CompanyResolution

    company = CompanyResolution(
        candidates=[
            CompanyCandidate(
                name="未核实候选有限公司",
                source_url="https://example.com/company",
            )
        ]
    )
    at, repo = history_app(tmp_path, monkeypatch, company=company)
    picker = by_label(at.selectbox, "企业主体")
    assert picker.value is None
    assert picker.options == ["未核实候选有限公司"]
    assert repo.export(["ui-test-run"])[0]["company_name"] is None


def test_review_company_candidates_are_scoped_to_selected_record(tmp_path, monkeypatch):
    from sdr_agent.schemas import CompanyCandidate, CompanyResolution

    def candidates(name):
        return CompanyResolution(
            candidates=[
                CompanyCandidate(
                    name=name,
                    source_url="https://example.com/company",
                )
            ]
        )

    at, repo = history_app(tmp_path, monkeypatch, company=candidates("第一条候选有限公司"))
    repo.save_run(
        AnalysisInput(transcript="客户：第二条合成测试通话。"),
        AnalysisResult(
            run_id="second-company-run",
            run_status="succeeded",
            lead=Lead(),
            company=candidates("第二条候选有限公司"),
        ),
    )
    at.run()
    by_label(at.selectbox, "查看并审核一条线索").set_value("second-company-run").run()
    assert not at.exception
    picker = by_label(at.selectbox, "企业主体")
    assert picker.value is None
    assert picker.options == ["第二条候选有限公司"]
    by_label(at.selectbox, "查看并审核一条线索").set_value("ui-test-run").run()
    assert not at.exception
    picker = by_label(at.selectbox, "企业主体")
    assert picker.value is None
    assert picker.options == ["第一条候选有限公司"]


def test_approval_requires_evidence_and_unknown_acknowledgment(tmp_path, monkeypatch):
    at, repo = history_app(tmp_path, monkeypatch)
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("核查后无法确认企业")
    by_label(at.checkbox, "同时标记为已审核").check()
    by_label(at.button, "预览本条修改").click().run()
    assert any("核查" in str(item.value) for item in at.error)
    assert not any(b.label == "确认保存本条修改" for b in at.button)
    by_label(at.checkbox, "我已核查原文和证据").check()
    by_label(at.button, "预览本条修改").click().run()
    assert any("未知" in str(item.value) for item in at.error)
    by_label(at.checkbox, "已检查并接受企业或意向的未知值（null）").check()
    by_label(at.button, "预览本条修改").click().run()
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    assert repo.get_run("ui-test-run")["review_status"] == "approved"
    assert repo.get_run("ui-test-run")["current"]["company_name"] is None


def test_failed_record_has_no_edit_or_export(tmp_path, monkeypatch):
    at, _ = history_app(tmp_path, monkeypatch, failed=True)
    assert not at.exception
    assert not any(item.label == "企业主体" for item in at.text_input)
    assert not any(item.label == "企业主体" for item in at.selectbox)
    assert not any(item.label == "预览本条修改" for item in at.button)
    assert any("失败" in str(item.value) for item in at.error)


def test_batch_review_only_preserves_existing_values(tmp_path, monkeypatch):
    at, repo = history_app(
        tmp_path, monkeypatch, lead=Lead(company_name="合成测试公司", intent_level="high")
    )
    by_label(at.multiselect, "选择待处理记录").set_value(["ui-test-run"]).run()
    by_label(at.selectbox, "批量修改字段").set_value("review_only")
    by_label(at.text_input, "批量审核人").set_value("审核员")
    by_label(at.text_area, "批量修正 / 核实说明").set_value("逐条核对保持原值")
    by_label(at.checkbox, "将所选记录同时标记为已审核").check()
    by_label(at.checkbox, "已逐条核查所选记录的原文和证据").check()
    by_label(at.button, "预览批量修改").click().run()
    by_label(at.button, "确认保存整批修改").click().run()
    assert not at.exception
    saved = repo.get_run("ui-test-run")
    assert saved["review_status"] == "approved"
    assert saved["current"]["company_name"] == "合成测试公司"
    assert saved["current"]["intent_level"] == "high"


def test_rerendering_history_does_not_call_model(tmp_path, monkeypatch):
    from sdr_agent.workflow import Workflow

    def forbidden(*args, **kwargs):
        raise AssertionError("历史展示不得调用模型")

    monkeypatch.setattr(Workflow, "analyze", forbidden)
    at, _ = history_app(tmp_path, monkeypatch)
    at.run()
    assert not at.exception


def test_stale_preview_cannot_overwrite_newer_review(tmp_path, monkeypatch):
    from sdr_agent.review import ReviewService

    at, repo = history_app(tmp_path, monkeypatch)
    enter_company(at, "页面中的旧修改")
    by_label(at.text_input, "审核人").set_value("页面审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("页面预览")
    by_label(at.button, "预览本条修改").click().run()
    ReviewService(repo).apply_batch(
        [
            {
                "run_id": "ui-test-run",
                "expected_version": 1,
                "updates": {"company_name": "另一页面的新修改"},
            }
        ],
        "另一审核员",
        "并发修改",
    )
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    assert any("已被其他页面修改" in str(item.value) for item in at.error)
    assert repo.get_run("ui-test-run")["current"]["company_name"] == "另一页面的新修改"


def test_review_does_not_save_negative_count(tmp_path, monkeypatch):
    at, repo = history_app(tmp_path, monkeypatch)
    by_label(at.text_input, "门店数量").set_value("-2")
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("人数核查")
    by_label(at.button, "预览本条修改").click().run()
    assert not at.exception
    assert any("非负整数" in str(item.value) for item in at.error)
    assert not any(b.label == "确认保存本条修改" for b in at.button)
    assert repo.get_run("ui-test-run")["version"] == 1


def test_confirmation_saves_displayed_snapshot(tmp_path, monkeypatch):
    at, repo = history_app(tmp_path, monkeypatch)
    enter_company(at, "已预览主体")
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("确认此快照")
    by_label(at.button, "预览本条修改").click().run()
    enter_company(at, "尚未预览主体")
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    assert repo.get_run("ui-test-run")["current"]["company_name"] == "已预览主体"


def test_batch_clears_company_to_null_only_after_confirmation(tmp_path, monkeypatch):
    at, repo = history_app(tmp_path, monkeypatch, lead=Lead(company_name="原主体"))
    repo.save_run(
        AnalysisInput(transcript="合成通话"),
        AnalysisResult(
            run_id="ui-test-second", run_status="succeeded", lead=Lead(company_name="第二主体")
        ),
    )
    at.run()
    by_label(at.multiselect, "选择待处理记录").set_value(["ui-test-run", "ui-test-second"]).run()
    by_label(at.text_input, "批量审核人").set_value("批量审核员")
    by_label(at.text_area, "批量修正 / 核实说明").set_value("证据不足，清空主体")
    by_label(at.button, "预览批量修改").click().run()
    assert not at.exception
    assert repo.get_run("ui-test-run")["current"]["company_name"] == "原主体"
    by_label(at.button, "确认保存整批修改").click().run()
    assert not at.exception
    assert all(
        row["company_name"] is None for row in repo.export(["ui-test-run", "ui-test-second"])
    )


def test_analyze_is_disabled_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15).run()
    assert by_label(at.button, "开始分析").disabled


def test_source_links_only_allow_http_https():
    from sdr_agent.ui import safe_source

    assert safe_source("https://example.com/evidence")
    assert not safe_source("javascript:alert(1)")
    assert not safe_source("file:///etc/passwd")
    assert not safe_source("data:text/html,<script>alert(1)</script>")


def test_authentication_failure_explains_configuration_without_raw_error(tmp_path, monkeypatch):
    at, _ = history_app(tmp_path, monkeypatch, failed=True, error_code="authentication_failed")
    assert not at.exception
    assert any("认证失败" in str(item.value) and "密钥配置" in str(item.value) for item in at.error)


def test_result_cards_escape_customer_content_without_changing_export(tmp_path, monkeypatch):
    value = '<img src=x onerror="alert(1)"> & 合成公司'
    at, repo = history_app(
        tmp_path, monkeypatch, lead=Lead(company_name=value, demand_summary=value)
    )
    assert not at.exception
    cards = "\n".join(item.value for item in at.markdown if item.allow_html)
    assert value not in cards
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; 合成公司" in cards
    assert repo.export(["ui-test-run"])[0]["company_name"] == value


def test_evidence_quotes_escape_customer_text(monkeypatch):
    from sdr_agent.ui import quotes

    rendered = []
    monkeypatch.setattr("sdr_agent.ui.st.markdown", lambda body, **kwargs: rendered.append(body))
    quotes([{"speaker": "customer", "turn_id": "<1>", "quote": "<script>alert(1)</script> & 原话"}])
    assert "<script>" not in rendered[0]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; 原话" in rendered[0]
    assert "&lt;1&gt;" in rendered[0]


def test_intent_reason_table_uses_structured_quotes_and_preserves_export(tmp_path, monkeypatch):
    from sdr_agent.intent import score_intent
    from sdr_agent.schemas import Evidence, Extraction, Signal

    quotes = {
        "pain": "出图反复修改，影响交付。",
        "timeline": "下个月试用。",
        "budget": "预算还没有定。",
        "engagement": "请安排演示。",
        "scale": "有18位设计师。",
    }
    intent = score_intent(
        Extraction(
            signals={
                name: Signal(
                    value=1, evidence=[Evidence(turn_id=1, speaker="customer", quote=text)]
                )
                for name, text in quotes.items()
            }
        )
    )
    at, repo = history_app(
        tmp_path,
        monkeypatch,
        lead=Lead(intent_level=intent.level, intent_reason=intent.reason),
        intent=intent,
    )
    assert not at.exception
    cards = "\n".join(item.value for item in at.markdown if item.allow_html)
    assert '<table class="intent-reason-table"' in cards
    assert all(text in cards for text in quotes.values())
    assert all(label in cards for label in ("痛点", "时间计划", "预算", "推进意愿", "业务规模"))
    assert " 依据：" not in cards
    assert repo.export(["ui-test-run"])[0]["intent_reason"] == intent.reason


def test_intent_table_keeps_manual_reason_and_escapes_quotes(tmp_path, monkeypatch):
    from sdr_agent.schemas import Evidence, IntentDecision, Signal

    quote = '<img src=x onerror="alert(1)"> & 原文'
    reason = "人工复核：依据：预算已获批准，改为高意向。"
    intent = IntentDecision(
        level="medium",
        reason="机器原判断。",
        next_action="补充核实。",
        signals={
            "budget": Signal(
                value=0.5,
                evidence=[
                    Evidence(
                        turn_id=1,
                        speaker="customer",
                        quote=quote,
                    )
                ],
            )
        },
    )
    at, repo = history_app(
        tmp_path,
        monkeypatch,
        lead=Lead(intent_level="high", intent_reason=reason),
        intent=intent,
    )
    assert not at.exception
    cards = "\n".join(item.value for item in at.markdown if item.allow_html)
    assert '<table class="intent-reason-table"' in cards
    assert "人工核实说明" in cards and reason in cards
    assert "原始通话依据" in cards
    assert quote not in cards
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; 原文" in cards
    assert repo.export(["ui-test-run"])[0]["intent_reason"] == reason


def test_intent_table_marks_unknown_and_conflicting_evidence(tmp_path, monkeypatch):
    from sdr_agent.schemas import Evidence, IntentDecision, Signal

    intent = IntentDecision(
        reason="采购时间存在冲突。",
        next_action="核查。",
        signals={
            "timeline": Signal(
                value=None,
                conflict=True,
                evidence=[
                    Evidence(turn_id=1, speaker="customer", quote="本月采购。"),
                    Evidence(turn_id=2, speaker="customer", quote="今年不采购。"),
                ],
            )
        },
    )
    at, _ = history_app(
        tmp_path, monkeypatch, lead=Lead(intent_reason=intent.reason), intent=intent
    )
    assert not at.exception
    cards = "\n".join(item.value for item in at.markdown if item.allow_html)
    assert '<table class="intent-reason-table"' in cards
    assert "未提供明确依据" in cards and "存在冲突" in cards
    assert "本月采购。" in cards and "今年不采购。" in cards


def test_intent_table_can_show_a_reason_without_signal_metadata(tmp_path, monkeypatch):
    at, _ = history_app(tmp_path, monkeypatch, lead=Lead(intent_reason="仅保留人工核实说明。"))
    assert not at.exception
    cards = "\n".join(item.value for item in at.markdown if item.allow_html)
    assert '<table class="intent-reason-table"' in cards
    assert "仅保留人工核实说明。" in cards


def test_evidence_intent_table_shows_values_and_source_turns_without_long_paragraph(
    tmp_path, monkeypatch
):
    from sdr_agent.intent import score_intent
    from sdr_agent.schemas import Evidence, Extraction, Signal

    quote = "<script>alert(1)</script> & 预算待审批"
    intent = score_intent(
        Extraction(
            signals={
                "pain": Signal(
                    value=1, evidence=[Evidence(turn_id=2, speaker="customer", quote="出图慢。")]
                ),
                "budget": Signal(
                    value=0.5, evidence=[Evidence(turn_id=5, speaker="customer", quote=quote)]
                ),
                "engagement": Signal(
                    value=0, evidence=[Evidence(turn_id=6, speaker="customer", quote="暂不推进。")]
                ),
            }
        )
    )
    at, _ = history_app(
        tmp_path,
        monkeypatch,
        lead=Lead(intent_level=intent.level, intent_reason=intent.reason),
        intent=intent,
    )
    assert not at.exception
    cards = "\n".join(item.value for item in at.markdown if item.allow_html)
    assert 'aria-label="意向信号"' in cards
    assert "信号值" in cards
    assert "客户 · 第2段" in cards and "客户 · 第5段" in cards
    assert "<td>0.5</td>" in cards and "<td>0</td>" in cards
    assert "未知 / null" in cards
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; 预算待审批" in cards
    assert quote not in cards
    assert intent.reason not in [item.value for item in at.text]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 1, 1, 0.5, 1], "意向评分：90 / 100"),
        ([1, 1, None, 0.5, 1], "意向评分区间：70 – 90 / 100"),
        ([1, 0.5, 1, 1, 1], "意向评分：87.5 / 100"),
        ([0, 0, 0, 0, 0], "意向评分：0 / 100"),
        ([None, None, None, None, None], None),
    ],
)
def test_intent_score_display_distinguishes_exact_and_unknown_dimensions(
    tmp_path, monkeypatch, values, expected
):
    from sdr_agent.intent import WEIGHTS, score_intent
    from sdr_agent.schemas import Extraction, Signal

    intent = score_intent(
        Extraction(signals={name: Signal(value=value) for name, value in zip(WEIGHTS, values)})
    )
    at, repo = history_app(tmp_path, monkeypatch, intent=intent)
    assert not at.exception
    captions = [item.value for item in at.caption]
    if expected:
        assert any(text.startswith(expected) for text in captions)
        if "区间" not in expected:
            assert not any("得分区间" in text or "评分区间" in text for text in captions)
    else:
        assert not any("意向评分" in text or "得分区间" in text for text in captions)
    saved = repo.get_run("ui-test-run")["analysis"]["intent"]
    assert saved["score_lower"] == intent.score_lower
    assert saved["score_upper"] == intent.score_upper
    assert saved["signals"]["budget"]["value"] == values[2]


def test_review_formats_legacy_machine_reason_and_only_saves_after_confirmation(
    tmp_path, monkeypatch
):
    from sdr_agent.schemas import Evidence, IntentDecision, Signal

    legacy = "已有正向需求。 依据：痛点：出图慢。；行动：先发介绍。 未知：时间、预算、规模。"
    expected = "已有正向需求。\n依据：\n痛点：出图慢；\n行动：先发介绍\n未知：时间、预算、规模。"
    intent = IntentDecision(
        level="medium",
        reason=legacy,
        next_action="补充核实",
        signals={
            "pain": Signal(
                value=1, evidence=[Evidence(turn_id=1, speaker="customer", quote="出图慢。")]
            ),
            "engagement": Signal(
                value=0.5, evidence=[Evidence(turn_id=2, speaker="customer", quote="先发介绍。")]
            ),
            "timeline": Signal(),
            "budget": Signal(),
            "scale": Signal(),
        },
    )
    at, repo = history_app(
        tmp_path,
        monkeypatch,
        lead=Lead(intent_level="medium", intent_reason=legacy),
        intent=intent,
    )
    assert not at.exception
    assert by_label(at.text_area, "意向依据").value == expected
    assert repo.export(["ui-test-run"])[0]["intent_reason"] == legacy
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("整理意向依据格式")
    by_label(at.button, "预览本条修改").click().run()
    assert not at.exception
    assert repo.export(["ui-test-run"])[0]["intent_reason"] == legacy
    by_label(at.button, "确认保存本条修改").click().run()
    assert not at.exception
    assert repo.export(["ui-test-run"])[0]["intent_reason"] == expected
    assert repo.get_run("ui-test-run")["original"]["intent_reason"] == legacy


def test_review_preserves_existing_manual_reason_verbatim(tmp_path, monkeypatch):
    from sdr_agent.schemas import IntentDecision

    manual = "人工补充：先核实预算。\n依据：\n预算：客户说需审批。"
    intent = IntentDecision(level="medium", reason="原始机器说明。", next_action="补充核实")
    at, _ = history_app(
        tmp_path,
        monkeypatch,
        lead=Lead(intent_level="medium", intent_reason=manual),
        intent=intent,
    )
    assert not at.exception
    assert by_label(at.text_area, "意向依据").value == manual


def test_company_scope_evidence_and_relation_source_are_visible(tmp_path, monkeypatch):
    from sdr_agent.schemas import CompanyCandidate, CompanyResolution, SearchResult

    quote = "合成测试主体有限公司\n总部地址：杭州\n主营定制家居。"
    company = CompanyResolution(
        candidates=[
            CompanyCandidate(
                name="合成测试主体有限公司",
                source_url="https://example.com/company",
                quote="合成测试主体有限公司",
                relation_quote="青木为合成测试主体有限公司的简称。",
                relation_source_url="https://example.com/legal",
                scope_evidence=[SearchResult(url="https://example.com/contact", content=quote)],
            )
        ]
    )
    at, _ = history_app(tmp_path, monkeypatch, company=company)
    assert not at.exception
    assert any(item.label == "查看范围核实依据" for item in at.expander)
    assert quote in [item.value for item in at.text]
    urls = [item.proto.url for item in at.get("link_button")]
    assert "https://example.com/legal" in urls
    assert "https://example.com/contact" in urls
