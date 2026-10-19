import json
import time

import httpx
import pytest

from sdr_agent.config import Settings
from sdr_agent.deepseek import DeepSeekClient, DeepSeekError
from sdr_agent.schemas import AnalysisInput, Extraction


def response(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def client_for(handler):
    return DeepSeekClient(
        Settings(api_key="secret-test-key"), transport=httpx.MockTransport(handler)
    )


def test_extract_uses_configured_model_json_mode_and_disabled_thinking():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/chat/completions"
        assert body["model"] == "deepseek-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert "secret-test-key" not in json.dumps(body)
        assert "schema" in body["messages"][1]["content"]
        return httpx.Response(200, json=response('{"facts": {}}'))

    client = client_for(handler)
    assert isinstance(
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 30), Extraction
    )
    assert client.request_count == 1


@pytest.mark.parametrize(
    "content,finish_reason", [("", "stop"), ("{bad", "stop"), ('{"facts": {}}', "length")]
)
def test_invalid_or_truncated_json_gets_only_one_repair(content, finish_reason):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=response(content, finish_reason)
            if len(requests) == 1
            else response('{"facts": {}}'),
        )

    client = client_for(handler)
    assert client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 30).facts == {}
    assert len(requests) == 2
    assert "修复" in requests[1]["messages"][-1]["content"]


def test_second_malformed_json_fails_with_safe_typed_error():
    client = client_for(lambda r: httpx.Response(200, json=response("secret-test-key {bad")))
    with pytest.raises(DeepSeekError) as caught:
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 30)
    assert caught.value.code == "invalid_response"
    assert "secret-test-key" not in str(caught.value)
    assert client.request_count == 2


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication_failed"),
        (402, "insufficient_balance"),
        (403, "permission_denied"),
        (404, "model_unavailable"),
    ],
)
def test_permanent_errors_are_not_retried_or_disclosed(status, code):
    client = client_for(
        lambda r: httpx.Response(status, json={"error": {"message": "secret-test-key"}})
    )
    with pytest.raises(DeepSeekError) as caught:
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 30)
    assert caught.value.code == code
    assert client.request_count == 1
    assert "secret-test-key" not in str(caught.value)


def test_transient_errors_retry_twice_with_bounded_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr("sdr_agent.deepseek.time.sleep", sleeps.append)
    client = client_for(lambda r: httpx.Response(503, text="upstream unavailable"))
    with pytest.raises(DeepSeekError) as caught:
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 30)
    assert caught.value.code == "provider_unavailable"
    assert client.request_count == 3
    assert sleeps == [1, 2]


def test_deadline_prevents_request_and_retry_sleep(monkeypatch):
    client = client_for(lambda r: httpx.Response(429))
    with pytest.raises(DeepSeekError) as caught:
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() - 1)
    assert caught.value.code == "deadline_exceeded"
    assert client.request_count == 0
    monkeypatch.setattr(
        "sdr_agent.deepseek.time.sleep", lambda _: pytest.fail("must not sleep past deadline")
    )
    with pytest.raises(DeepSeekError) as caught:
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 0.1)
    assert caught.value.code == "deadline_exceeded"
    assert client.request_count == 1


def test_search_and_fetch_use_documented_tools_payloads():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("search"):
            return httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.com", "title": "公司", "content": "正文"}]
                },
            )
        return httpx.Response(
            200, json={"url": "https://example.com", "title": "公司", "content": "完整正文"}
        )

    client = client_for(handler)
    assert client.search("甲" * 140, time.monotonic() + 30)[0].content == "正文"
    assert client.fetch("https://example.com", time.monotonic() + 30).content == "完整正文"
    assert seen == [
        ("/v1/tools/search", {"query": "甲" * 128, "count": 5, "need_content": True}),
        ("/v1/tools/fetch", {"url": "https://example.com"}),
    ]


def test_invalid_provider_payload_has_safe_error():
    client = client_for(lambda r: httpx.Response(200, text="secret-test-key not-json"))
    with pytest.raises(DeepSeekError) as caught:
        client.search("公司", time.monotonic() + 30)
    assert caught.value.code == "invalid_response"
    assert "secret-test-key" not in str(caught.value)


def test_extract_validates_customer_evidence_once_before_returning():
    data = {
        "facts": {
            "area": {
                "value": "杭州",
                "evidence": [{"turn_id": 2, "speaker": "customer", "quote": "我们在杭州"}],
            },
            "mendian": {
                "value": 20,
                "evidence": [{"turn_id": 1, "speaker": "sales", "quote": "你们有20家门店"}],
            },
        }
    }
    client = client_for(lambda r: httpx.Response(200, json=response(json.dumps(data))))
    result = client.extract(
        AnalysisInput(transcript="销售：你们有20家门店？\n客户：我们在杭州"), time.monotonic() + 30
    )
    assert result.facts["mendian"].value is None
    assert result.fact_evidence_ratio == 0.5


def test_timeout_retry_exhaustion_is_safe(monkeypatch):
    monkeypatch.setattr("sdr_agent.deepseek.time.sleep", lambda _: None)

    def timeout(request):
        raise httpx.ReadTimeout("secret-test-key", request=request)

    client = client_for(timeout)
    with pytest.raises(DeepSeekError) as caught:
        client.extract(AnalysisInput(transcript="客户：你好"), time.monotonic() + 30)
    assert client.request_count == 3
    assert "secret-test-key" not in str(caught.value)


def test_company_candidate_request_excludes_customer_contact_details():
    from sdr_agent.schemas import Fact, SearchResult

    def handler(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][1]["content"])["data"]
        assert "13800138000" not in json.dumps(payload)
        assert "张三" not in json.dumps(payload, ensure_ascii=False)
        return httpx.Response(200, json=response('{"candidates": []}'))

    client = client_for(handler)
    data = Extraction(
        company_mention="青木",
        facts={"phone": Fact(value="13800138000"), "contact_name": Fact(value="张三")},
    )
    assert (
        client.resolve_company(
            data, [SearchResult(url="https://example.com", content="资料")], time.monotonic() + 30
        ).candidates
        == []
    )


@pytest.mark.parametrize("body", [{"data": []}, {"results": [None]}, {"data": {"results": "bad"}}])
def test_malformed_nested_search_payload_has_typed_error(body):
    client = client_for(lambda r: httpx.Response(200, json=body))
    with pytest.raises(DeepSeekError) as caught:
        client.search("公司", time.monotonic() + 30)
    assert caught.value.code == "invalid_response"


def test_malformed_nested_fetch_payload_has_typed_error():
    client = client_for(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(DeepSeekError) as caught:
        client.fetch("https://example.com", time.monotonic() + 30)
    assert caught.value.code == "invalid_response"
