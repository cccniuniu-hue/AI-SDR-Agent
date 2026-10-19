from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sdr_agent.repository import Repository
from sdr_agent.schemas import AnalysisInput, AnalysisResult, Lead


def history_app(tmp_path, monkeypatch, lead=None, failed=False, error_code="timeout"):
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
            error_code=error_code if failed else None,
        ),
    )
    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15).run()
    at.sidebar.radio[0].set_value("线索与审核").run()
    return at, repo


def by_label(elements, label):
    return next(item for item in elements if item.label == label)


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
    by_label(at.text_input, "企业主体").set_value("合成测试有限公司")
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
    by_label(at.text_input, "企业主体").set_value("页面中的旧修改")
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
    by_label(at.text_input, "企业主体").set_value("已预览主体")
    by_label(at.text_input, "审核人").set_value("测试审核员")
    by_label(at.text_area, "修正 / 核实说明").set_value("确认此快照")
    by_label(at.button, "预览本条修改").click().run()
    by_label(at.text_input, "企业主体").set_value("尚未预览主体")
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
