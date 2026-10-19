import json
from pathlib import Path

import pytest

from sdr_agent.config import Settings
from sdr_agent.deepseek import DeepSeekError
from sdr_agent.evaluation import (
    EvaluationSample,
    evaluate,
    load_dataset,
    main,
    run_baseline,
    summarize,
)
from sdr_agent.schemas import Extraction, Lead


def sample(sample_id="x", intent=None, company=None, **expected):
    return EvaluationSample(
        id=sample_id,
        split="dev",
        scenario_group="test",
        provenance="synthetic",
        transcript="客户：您好。",
        company_source="https://example.com/company" if company else None,
        expected={"intent_level": intent, "company_name": company, **expected},
    )


def record(item, predicted=None, status="succeeded", review=True, **extra):
    return {
        "sample_id": item.id,
        "method": "workflow",
        "expected": item.expected,
        "prediction": {
            "intent_level": predicted,
            "company_name": None,
            "need_human_review": review,
        },
        "status": status,
        "elapsed_ms": 100,
        "request_count": 2,
        **extra,
    }


def test_four_class_accuracy_known_macro_f1_and_null_retention():
    rows = [
        record(sample("h", "high"), None),
        record(sample("m", "medium"), "medium"),
        record(sample("l", "low"), "low"),
        record(sample("n", None), None),
    ]
    metrics = summarize(rows)
    assert metrics["intent"]["four_class_accuracy"] == 0.75
    assert metrics["intent"]["known_macro_f1"] == pytest.approx(2 / 3)
    assert metrics["intent"]["per_class"]["high"]["fn"] == 1
    assert metrics["intent"]["null_retention"] == 1
    assert metrics["intent"]["null_denominator"] == 1
    assert metrics["requests"]["total"] == 8
    assert metrics["latency_ms"]["mean"] == 100


def test_failures_are_errors_not_successful_null_predictions():
    row = record(sample(), status="failed", prediction=None)
    metrics = summarize([row])
    assert metrics["failures"] == 1
    assert metrics["intent"]["four_class_accuracy"] == 0
    assert metrics["intent"]["null_retention"] == 0
    assert metrics["fields"]["errors"] == 2
    assert metrics["review"]["rate"] is None


def test_company_precision_coverage_and_denominators_are_separate():
    rows = [
        record(
            sample("a", company="甲有限公司"),
            prediction={"company_name": "甲有限公司", "intent_level": None},
        ),
        record(sample("b", company="乙有限公司")),
        record(sample("c"), prediction={"company_name": "幻觉有限公司", "intent_level": None}),
    ]
    metrics = summarize(rows)["company"]
    assert metrics["precision"] == 0.5
    assert metrics["coverage"] == 0.5
    assert metrics["precision_denominator"] == 2
    assert metrics["coverage_denominator"] == 2
    assert metrics["unknown_gold_confirmations"] == 1


def test_company_metrics_without_known_gold_are_null_even_if_prediction_hallucinates():
    rows = [record(sample(), prediction={"company_name": "幻觉有限公司", "intent_level": None})]
    metrics = summarize(rows)["company"]
    assert metrics["precision"] is None
    assert metrics["coverage"] is None
    assert metrics["coverage_denominator"] == 0
    assert metrics["unknown_gold_confirmations"] == 1


def test_field_errors_count_null_invention_and_missing_known_values():
    row = record(
        sample(phone=None, area="杭州"),
        prediction={
            "intent_level": None,
            "company_name": None,
            "phone": "123",
            "area": None,
            "need_human_review": False,
        },
    )
    metrics = summarize([row])
    assert metrics["fields"]["errors"] == 2
    assert metrics["fields"]["compared"] == 4
    assert metrics["fields"]["invented_nonnull"] == 1
    assert metrics["fields"]["missing_known"] == 1
    assert metrics["review"]["rate"] == 0


def test_datasets_have_balanced_labels_and_disjoint_scenario_groups():
    root = Path(__file__).resolve().parents[1]
    dev = load_dataset(root / "evaluation/dev.jsonl")
    holdout = load_dataset(root / "evaluation/holdout.jsonl")
    for split, name in [(dev, "dev"), (holdout, "holdout")]:
        assert len(split) == 12
        assert {item.split for item in split} == {name}
        assert all(item.provenance == "synthetic" for item in split)
        assert {
            label: sum(item.expected["intent_level"] == label for item in split)
            for label in ["high", "medium", "low", None]
        } == {"high": 3, "medium": 3, "low": 3, None: 3}
    assert not {x.scenario_group for x in dev} & {x.scenario_group for x in holdout}
    assert any(x.expected["company_name"] is not None and x.company_source for x in dev)
    assert all(x.expected["company_name"] is None for x in holdout)


def test_baseline_receives_only_input_without_gold_or_scenario_metadata():
    class Client:
        request_count = 0

        def chat_json(self, prompt, payload, schema, deadline):
            assert payload == {"transcript": "客户：您好。", "call_time": None}
            assert schema is Lead
            assert "expected" not in prompt
            self.request_count += 1
            return Lead()

    row = run_baseline(
        sample("secret-case", intent="high", company="标签公司"), Settings(), Client()
    )
    assert row["prediction"]["intent_level"] is None
    assert row["request_count"] == 1
    assert row["status"] == "succeeded"


def test_fixture_is_explicitly_not_model_quality_and_resume_never_duplicates(tmp_path):
    dataset = tmp_path / "samples.jsonl"
    dataset.write_text(sample(intent="high").model_dump_json() + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    report = evaluate(dataset, "fixture", output, baseline=True)
    assert report["mode"] == "fixture"
    assert report["is_model_evaluation"] is False
    assert "不代表模型效果" in report["notice"]
    assert set(report["metrics"]) == {"workflow", "baseline"}
    assert report["metrics"]["workflow"]["intent"]["four_class_accuracy"] == 0
    assert report["metrics"]["workflow"]["requests"]["total"] == 0
    ledger = output.with_suffix(".samples.jsonl")
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    resumed = evaluate(dataset, "fixture", output, baseline=True, resume=True)
    assert resumed["completed_records"] == 2
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_resume_rejects_changed_dataset_instead_of_mixing_runs(tmp_path):
    dataset = tmp_path / "samples.jsonl"
    dataset.write_text(sample().model_dump_json() + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    evaluate(dataset, "fixture", output)
    dataset.write_text(sample(intent="high").model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="数据集|dataset"):
        evaluate(dataset, "fixture", output, resume=True)


def test_cli_fixture_writes_report_and_nonresume_will_not_overwrite(tmp_path):
    dataset = tmp_path / "samples.jsonl"
    dataset.write_text(sample().model_dump_json() + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    assert main(["--dataset", str(dataset), "--mode", "fixture", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["completed_records"] == 1
    with pytest.raises(FileExistsError):
        evaluate(dataset, "fixture", output)


def test_live_runs_real_workflow_and_direct_baseline_without_sending_labels(tmp_path, monkeypatch):
    clients = []

    class Client:
        request_count = 0
        closed = False

        def __init__(self, settings):
            clients.append(self)

        def extract(self, input, deadline):
            assert input.transcript == "客户：您好。"
            self.request_count += 1
            return Extraction()

        def chat_json(self, prompt, payload, schema, deadline):
            assert set(payload) == {"transcript", "call_time"}
            self.request_count += 1
            return Lead(intent_level="high")

        def close(self):
            self.closed = True

    monkeypatch.setattr("sdr_agent.evaluation.DeepSeekClient", Client)
    dataset = tmp_path / "samples.jsonl"
    dataset.write_text(sample(intent="high").model_dump_json() + "\n", encoding="utf-8")
    output = tmp_path / "report.json"
    report = evaluate(
        dataset, "live", output, baseline=True, settings=Settings(api_key="not-a-real-key")
    )
    assert report["metrics"]["workflow"]["intent"]["four_class_accuracy"] == 0
    assert report["metrics"]["baseline"]["intent"]["four_class_accuracy"] == 1
    assert report["metrics"]["workflow"]["requests"]["total"] == 1
    assert report["metrics"]["baseline"]["requests"]["total"] == 1
    assert output.with_suffix(".sqlite3").exists()
    assert clients[0].closed
    assert "not-a-real-key" not in output.read_text(encoding="utf-8")


def test_baseline_failure_records_safe_code_and_request_delta():
    class Client:
        request_count = 5

        def chat_json(self, *args):
            self.request_count += 3
            raise DeepSeekError("provider_unavailable", "sensitive upstream detail")

    row = run_baseline(sample(), Settings(), Client())
    assert row["status"] == "failed"
    assert row["request_count"] == 3
    assert row["error_code"] == "provider_unavailable"
    assert "sensitive" not in json.dumps(row)


def test_resume_fingerprint_changes_when_scoring_code_changes(tmp_path, monkeypatch):
    from sdr_agent.evaluation import _fingerprint

    settings = Settings()
    before = _fingerprint(settings, "live")
    original = Path.read_text

    def changed(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        return text + "\n# changed scoring logic" if path.name == "intent.py" else text

    monkeypatch.setattr(Path, "read_text", changed)
    assert _fingerprint(settings, "live") != before
