"""Gateway contract/reliability tests. MockTransport is a TEST DOUBLE only."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from serving.api import create_app
from serving.config import Settings

KEY = "test-double-key-1234567890"
AUTH = {"Authorization": "Bearer " + KEY}
PROMPT = "PRIVATE-PROMPT-CONTENT"


def upstream_reply(content='{"score":0.5,"confidence":0.8}', *, model="fund-llm", finish="stop"):
    return {"model": model, "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 16}}


def transport_for(*, content='{"score":0.5,"confidence":0.8}', count=100,
                  ready_model="fund-llm", status=200, finish="stop", model="fund-llm", calls=None):
    async def handler(request):
        if calls is not None:
            calls.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": ready_model}]})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": count, "max_model_len": 4096})
        return httpx.Response(status, json=upstream_reply(content, finish=finish, model=model))
    return httpx.MockTransport(handler)


def app_for(transport=None, **kwargs):
    return create_app(Settings(api_key=KEY, deployment_environment="test-double", **kwargs),
                      transport=transport or transport_for())


def generate(client, **kwargs):
    return client.post("/v1/generate", headers=AUTH,
                       json={"task": "sentiment", "prompt": PROMPT, "max_tokens": 128, **kwargs})


def test_http_contract_metrics_and_model_presence():
    calls = []
    with TestClient(app_for(transport_for(calls=calls), backend_api_key="separate-backend-key")) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        response = generate(client)
        assert response.status_code == 200
        body = response.json()
        assert body["data"] == {"score": .5, "confidence": .8}
        assert body["usage"] == {"input_tokens": 100, "output_tokens": 16}
        assert body["latency_seconds"] >= 0
        assert client.get("/v1/model-info", headers=AUTH).json()["deployment_environment"] == "test-double"
        metrics = client.get("/metrics", headers=AUTH).text
        assert 'fund_model_requests_total{outcome="success",task="sentiment"} 1.0' in metrics
        assert "fund_model_request_seconds_count" in metrics
        assert "fund_model_tokens_total" in metrics
        assert PROMPT not in metrics and KEY not in metrics
    assert calls[-1].headers["Authorization"] == "Bearer separate-backend-key"
    payload = json.loads(calls[-1].content)
    assert payload["response_format"]["type"] == "json_schema"


def test_auth_and_validation_never_reflect_private_input():
    calls = []
    with TestClient(app_for(transport_for(calls=calls))) as client:
        assert client.post("/v1/generate", json={"task": "sentiment", "prompt": PROMPT}).status_code == 401
        assert client.get("/metrics").status_code == 401
        assert generate(client, max_tokens=True).status_code == 422
        invalid = generate(client, extra=PROMPT)
        assert invalid.status_code == 422 and PROMPT not in invalid.text
        assert generate(client, prompt=" ").status_code == 422
        assert generate(client, temperature="0.1").status_code == 422
        assert generate(client, task="portfolio", symbols=["AAPL", "AAPL"]).status_code == 422
    assert not calls


def test_body_and_prompt_limits_before_backend():
    calls = []
    with TestClient(app_for(transport_for(calls=calls), max_request_bytes=1024, max_prompt_chars=100)) as client:
        assert generate(client, prompt="x" * 101).status_code == 422
        assert generate(client, prompt="x" * 2000).status_code == 413
    assert not calls


def test_context_counts_actual_upstream_tokens():
    calls = []
    with TestClient(app_for(transport_for(count=4090, calls=calls))) as client:
        assert generate(client).json()["error"] == "context_limit"
    assert len(calls) == 1 and calls[0].url.path == "/tokenize"


@pytest.mark.parametrize("content", [
    '{"score":NaN,"confidence":0.5}', '{"score":2,"confidence":0.5}',
    '{"score":"0.2","confidence":0.5}', '{"score":true,"confidence":0.5}',
    '{"score":0.2,"confidence":0.5,"extra":"PRIVATE-PROMPT-CONTENT"}',
    'PRIVATE-PROMPT-CONTENT', '{"score":0.2,"score":0.3,"confidence":0.5}',
])
def test_malformed_output_returns_redacted_error(content):
    with TestClient(app_for(transport_for(content=content))) as client:
        response = generate(client)
        assert response.status_code == 502
        assert PROMPT not in response.text
        assert 'outcome="invalid_upstream_output"' in client.get("/metrics", headers=AUTH).text


@pytest.mark.parametrize("kwargs,expected", [
    ({"status": 500}, 502), ({"status": 429}, 503),
    ({"finish": "length"}, 502), ({"model": "wrong"}, 502),
])
def test_upstream_error_and_wrong_model(kwargs, expected):
    with TestClient(app_for(transport_for(**kwargs))) as client:
        assert generate(client).status_code == expected


def test_readiness_requires_configured_served_model():
    with TestClient(app_for(transport_for(ready_model="wrong"))) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_research_surrogate_output_never_counts_as_success():
    malformed = '{"hypothesis":"\\ud800","rules":["test"],"risks":["bias"],"verdict":"test","confidence":0.5}'
    with TestClient(app_for(transport_for(content=malformed))) as client:
        response = generate(client, task="research")
        assert response.status_code == 502
        metrics = client.get("/metrics", headers=AUTH).text
        assert 'outcome="invalid_upstream_output",task="research"' in metrics
        assert 'outcome="success",task="research"' not in metrics


def test_config_fails_closed_and_repr_redacts_credentials():
    with pytest.raises(ValueError):
        Settings(api_key="")
    configured = Settings(api_key=KEY, backend_api_key="backend-secret")
    assert KEY not in repr(configured) and "backend-secret" not in repr(configured)


def test_deadline_cancels_upstream_and_releases_admission():
    async def exercise():
        cancelled = asyncio.Event()

        async def handler(request):
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"count": 100})
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return httpx.Response(200, json=upstream_reply())

        app = app_for(httpx.MockTransport(handler), request_timeout_seconds=.05, max_concurrency=1)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test-double") as client:
                response = await client.post("/v1/generate", headers=AUTH, json={"task": "sentiment", "prompt": PROMPT})
                assert response.status_code == 504
                assert cancelled.is_set() and app.state.inflight == 0
                metrics = (await client.get("/metrics", headers=AUTH)).text
                assert 'outcome="upstream_timeout"' in metrics
    asyncio.run(exercise())


def test_concurrency_limit_rejects_instead_of_unbounded_queue():
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"count": 100})
            started.set()
            await release.wait()
            return httpx.Response(200, json=upstream_reply())

        app = app_for(httpx.MockTransport(handler), max_concurrency=1)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test-double") as client:
                body = {"task": "sentiment", "prompt": PROMPT}
                first = asyncio.create_task(client.post("/v1/generate", headers=AUTH, json=body))
                await asyncio.wait_for(started.wait(), timeout=1)
                rejected = await client.post("/v1/generate", headers=AUTH, json=body)
                assert rejected.status_code == 429 and rejected.headers["Retry-After"] == "1"
                release.set()
                assert (await first).status_code == 200
                assert app.state.inflight == 0
    asyncio.run(exercise())


def _scope():
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/v1/generate",
            "raw_path": b"/v1/generate", "query_string": b"", "root_path": "",
            "headers": [(b"authorization", ("Bearer " + KEY).encode()),
                        (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234), "server": ("test-double", 80)}


def test_client_disconnect_cancels_real_gateway_http_work():
    async def exercise():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"count": 100})
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        events, responses = asyncio.Queue(), []
        await events.put({"type": "http.request", "body": json.dumps(
            {"task": "sentiment", "prompt": PROMPT}).encode(), "more_body": False})

        async def send(message):
            responses.append(message)

        app = app_for(httpx.MockTransport(handler))
        async with app.router.lifespan_context(app):
            call = asyncio.create_task(app(_scope(), events.get, send))
            await asyncio.wait_for(started.wait(), timeout=1)
            await events.put({"type": "http.disconnect"})
            await asyncio.wait_for(call, timeout=1)
            assert cancelled.is_set() and app.state.inflight == 0
            assert responses[0]["status"] == 499
    asyncio.run(exercise())


def test_chunked_body_limit_and_upload_deadline():
    async def exercise(chunks, expected_status):
        events, responses = asyncio.Queue(), []
        for chunk in chunks:
            await events.put(chunk)

        async def send(message):
            responses.append(message)

        app = app_for(max_request_bytes=1024, request_timeout_seconds=.05)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(_scope(), events.get, send), timeout=1)
            assert responses[0]["status"] == expected_status
            assert app.state.inflight == 0

    asyncio.run(exercise([
        {"type": "http.request", "body": b"x" * 800, "more_body": True},
        {"type": "http.request", "body": b"x" * 800, "more_body": True},
    ], 413))
    asyncio.run(exercise([
        {"type": "http.request", "body": b"x", "more_body": True},
    ], 408))
