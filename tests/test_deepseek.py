import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


@pytest.fixture
def local_api_and_proxy():
    received = {"api": [], "proxy": []}
    servers, threads = [], []

    def start(role):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received[role].append(self.path)
                if self.path == "/chat/completions":
                    payload = response('{"facts": {}}')
                elif self.path == "/v1/tools/search":
                    payload = {"results": []}
                else:
                    payload = {"url": "https://example.com", "content": "公司资料"}
                body = json.dumps(payload).encode()
                self.send_response(403 if role == "proxy" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
        return f"http://127.0.0.1:{server.server_port}"

    try:
        yield start("api"), start("proxy"), received
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@pytest.mark.parametrize("proxy_source", ["environment", "system"])
def test_all_deepseek_endpoints_ignore_external_proxy_settings(
    monkeypatch, local_api_and_proxy, proxy_source
):
    api_url, proxy_url, received = local_api_and_proxy
    if proxy_source == "environment":
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.delenv(name.lower(), raising=False)
            monkeypatch.setenv(name, proxy_url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
    else:
        # Simulate proxy discovery from the OS, independently of the test runner's platform.
        monkeypatch.setattr(
            "httpx._utils.getproxies", lambda: {"http": proxy_url, "https": proxy_url}
        )
    with DeepSeekClient(Settings(api_key="test-key-not-real", base_url=api_url)) as client:
        deadline = time.monotonic() + 10
        assert client.extract(AnalysisInput(transcript="客户：你好"), deadline).facts == {}
        assert client.search("测试公司", deadline) == []
        assert client.fetch("https://example.com", deadline).content == "公司资料"
        assert client.request_count == 3
    assert received["api"] == ["/chat/completions", "/v1/tools/search", "/v1/tools/fetch"]
    assert received["proxy"] == []


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


@pytest.mark.parametrize("tool", ["search", "fetch"])
def test_company_tools_fail_fast_without_retrying(monkeypatch, tool):
    monkeypatch.setattr("sdr_agent.deepseek.time.sleep", lambda _: None)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503)

    with client_for(handler) as client:
        with pytest.raises(DeepSeekError):
            getattr(client, tool)(
                "https://example.com" if tool == "fetch" else "测试公司", time.monotonic() + 30
            )
        assert client.request_count == 1
    assert seen[0].extensions["timeout"]["read"] <= 8


def test_continuous_slow_response_cannot_bypass_wall_clock_deadline(tmp_path, monkeypatch):
    from sdr_agent.repository import Repository
    from sdr_agent.schemas import Fact, SearchResult, Signal
    from sdr_agent.workflow import Workflow

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b" " * 25 + b'{"results": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for _ in range(25):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.04)
                self.wfile.write(b'{"results": []}')
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with DeepSeekClient(
            Settings(api_key="test-key-not-real", base_url=f"http://127.0.0.1:{server.server_port}")
        ) as client:
            start = time.monotonic()
            with pytest.raises(DeepSeekError) as caught:
                client.search("测试公司", start + 0.15)
            elapsed = time.monotonic() - start
            # HTTPX's inactivity timer may expire just before the cancellation deadline.
            assert caught.value.code in {"deadline_exceeded", "provider_unavailable"}
            assert elapsed < 0.6
            assert client.request_count == 1

            extraction = Extraction(
                company_mention="测试主体有限公司",
                facts={"area": Fact(value="杭州")},
                signals={"pain": Signal(value=1), "engagement": Signal(value=1)},
            )
            source = SearchResult(
                url="https://example.com/company", content="测试主体有限公司位于杭州。"
            )
            monkeypatch.setattr(client, "extract", lambda *_: extraction)
            monkeypatch.setattr(client, "search", lambda *_: [source])
            repo = Repository(tmp_path / "slow.sqlite3")
            start = time.monotonic()
            result = Workflow(Settings(company_timeout=0.15), repo, client=client).analyze(
                AnalysisInput(transcript="客户：我们在杭州，出图慢，请安排演示。")
            )
            assert time.monotonic() - start < 0.6
            assert result.run_status == "partial"
            assert result.lead.area == "杭州"
            assert result.lead.intent_level == "medium"
            assert result.lead.company_name is None
            assert result.company.evidence == [source]
            assert len(repo.export([result.run_id])[0]) == 15
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
