"""HTTP contract tests using a local test double, NOT GPU/vLLM evidence."""
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.llm import GatewayClient, VLLMClient, VLLMError
from core.llm.contracts import ContractError, strict_json_loads, validate_data


@contextmanager
def _test_double(*, content='{"score":0.5,"confidence":0.8}', count=100,
                finish_reason="stop", status=200, response_model="fund-llm", trickle=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply({"data": [{"id": "fund-llm"}]})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload, dict(self.headers)))
            if self.path == "/tokenize":
                self.reply({"count": count, "max_model_len": 4096})
            elif self.path == "/v1/generate":
                self.reply({"request_id": "ad835835-e784-4d12-a257-9a3846a23182",
                            "model": response_model, "task": payload["task"],
                            "data": json.loads(content),
                            "usage": {"input_tokens": 100, "output_tokens": 16}})
            else:
                self.reply({"model": response_model,
                            "choices": [{"finish_reason": finish_reason,
                                         "message": {"content": content}}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 16}}, status)

        def reply(self, value, http_status=200):
            raw = json.dumps(value).encode()
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                if trickle and self.path == "/v1/chat/completions":
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.01)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_actual_http_compatible_request_and_usage():
    with _test_double() as (url, calls):
        client = VLLMClient(base_url=url, api_key="not-a-real-secret")
        assert client.ready()
        result = client.generate(task="sentiment", prompt="Earnings exceeded forecasts", max_tokens=128)
    assert result.data == {"score": .5, "confidence": .8}
    assert result.input_tokens == 100 and result.output_tokens == 16
    assert [c[0] for c in calls] == ["/tokenize", "/v1/chat/completions"]
    request = calls[-1][1]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert calls[-1][2]["Authorization"] == "Bearer not-a-real-secret"


def test_gateway_same_interface_over_http():
    with _test_double() as (url, calls):
        result = GatewayClient(base_url=url).generate(task="sentiment", prompt="A headline")
    assert result.data["score"] == .5
    assert len(calls) == 1 and calls[0][0] == "/v1/generate"


def test_actual_token_budget_blocks_generation():
    with _test_double(count=4090) as (url, calls):
        with pytest.raises(VLLMError, match="context_limit"):
            VLLMClient(base_url=url).generate(task="sentiment", prompt="headline", max_tokens=128)
    assert len(calls) == 1


def test_slow_body_trickle_cannot_refresh_whole_deadline():
    with _test_double(trickle=True) as (url, calls):
        started = time.monotonic()
        with pytest.raises(VLLMError, match="timeout"):
            VLLMClient(base_url=url, timeout=.1).generate(task="sentiment", prompt="headline")
        elapsed = time.monotonic() - started
    assert len(calls) == 2
    assert elapsed < .35  # each byte arrives faster than the socket timeout


@pytest.mark.parametrize("content", [
    '{"score":NaN,"confidence":0.5}', '{"score":2,"confidence":0.5}',
    '{"score":"0.2","confidence":0.5}', '{"score":true,"confidence":0.5}',
    '{"score":0.2,"confidence":0.5,"extra":"secret"}', 'not json',
    '{"score":0.2,"score":0.3,"confidence":0.5}',
])
def test_bad_generated_values_are_rejected(content):
    with _test_double(content=content) as (url, _):
        with pytest.raises(VLLMError, match="invalid_output"):
            VLLMClient(base_url=url).generate(task="sentiment", prompt="headline")


@pytest.mark.parametrize("kwargs,code", [
    ({"finish_reason": "length"}, "invalid_output"),
    ({"response_model": "unexpected-model"}, "invalid_output"),
    ({"status": 503}, "upstream_http"),
])
def test_truncation_wrong_model_and_upstream_error(kwargs, code):
    with _test_double(**kwargs) as (url, _):
        with pytest.raises(VLLMError, match=code):
            VLLMClient(base_url=url).generate(task="sentiment", prompt="headline")


def test_invalid_client_request_never_sends_http():
    with _test_double() as (url, calls):
        with pytest.raises(VLLMError, match="invalid_request"):
            VLLMClient(base_url=url).generate(task="portfolio", prompt="briefing", symbols=["AAPL", "AAPL"])
    assert not calls


@pytest.mark.parametrize("data", [
    {"positions": [{"symbol": "UNLISTED", "weight": .01}]},
    {"positions": [{"symbol": "AAPL", "weight": .1}]},
    {"positions": [{"symbol": "AAPL", "weight": .01}, {"symbol": "AAPL", "weight": .02}]},
])
def test_portfolio_universe_and_exposure_bounds(data):
    with pytest.raises(ContractError):
        validate_data("portfolio", data, ["AAPL"])


def test_research_output_is_fixed_not_arbitrary_code():
    value = {"hypothesis": "Test momentum after costs", "rules": ["Use held-out dates"],
             "risks": ["Selection bias"], "verdict": "test", "confidence": .5}
    assert validate_data("research", value) == value
    with pytest.raises(ContractError):
        validate_data("research", dict(value, python_code="print('unsafe')"))


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://user:secret@localhost/v1",
                                      "http://localhost/v1?api_key=secret", "http://localhost/wrong"])
def test_backend_url_never_accepts_inline_secret_or_other_protocol(url):
    with pytest.raises(ValueError):
        VLLMClient(base_url=url)


def test_strict_json_rejects_exponential_nonfinite():
    value = strict_json_loads('{"score":1e999,"confidence":0.5}')
    with pytest.raises(ContractError):
        validate_data("sentiment", value)


def test_huge_integer_cannot_overflow_validation_into_server_error():
    with pytest.raises(ContractError):
        validate_data("sentiment", {"score": 10 ** 1000, "confidence": .5})


def test_invalid_unicode_is_rejected_before_http_serialization():
    with pytest.raises(ContractError):
        strict_json_loads('{"text":"\\ud800"}')
