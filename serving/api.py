"""Private FastAPI gateway to a genuine, separately deployed vLLM server.

Run one worker per pod: admission limits are per process. Kubernetes backend
network policy makes this gateway the only caller of the raw inference API.
Testing injects HTTP transports explicitly; no mock mode exists in this server.
"""
from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from starlette.responses import JSONResponse as StarletteJSONResponse

from core.llm.contracts import (ContractError, MAX_RESPONSE_BYTES, build_chat_payload,
                                check_context, parse_chat_response, strict_json_loads,
                                validate_symbols)
from .config import Settings


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    task: Literal["portfolio", "sentiment", "research"]
    prompt: str = Field(min_length=1, max_length=24000, strict=True)
    symbols: list[str] | None = Field(default=None, max_length=256)
    max_tokens: StrictInt = Field(default=512, ge=1, le=1024)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0, strict=True)

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value):
        if not value.strip():
            raise ValueError("prompt cannot be blank")
        return value


class Telemetry:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("fund_model_requests_total", "Completed model requests by outcome",
                                ["task", "outcome"], registry=self.registry)
        self.latency = Histogram("fund_model_request_seconds", "Whole inference request latency",
                                 ["task"], buckets=(.05, .1, .25, .5, 1, 2, 5, 10, 20, 30, 60),
                                 registry=self.registry)
        self.tokens = Counter("fund_model_tokens_total", "Measured token usage on valid completions",
                              ["direction"], registry=self.registry)
        self.inflight = Gauge("fund_model_inflight", "Requests admitted by this one-worker process",
                              registry=self.registry)
        self.rejections = Counter("fund_model_rejections_total", "Gateway rejections before inference",
                                 ["reason"], registry=self.registry)


class RequestGuard:
    """Authenticate and bound raw bodies before JSON/Pydantic allocation."""
    def __init__(self, app, settings: Settings, telemetry: Telemetry):
        self.app, self.settings, self.telemetry = app, settings, telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        if path.startswith("/v1/") or path == "/metrics":
            expected = ("Bearer " + self.settings.api_key).encode()
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                self.telemetry.rejections.labels("authentication").inc()
                return await StarletteJSONResponse(
                    {"error": "unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        if path == "/v1/generate":
            length = headers.get(b"content-length")
            try:
                declared_length = int(length) if length is not None else 0
            except (ValueError, OverflowError):
                declared_length = -1
            if declared_length < 0 or declared_length > self.settings.max_request_bytes:
                self.telemetry.rejections.labels("body_size").inc()
                return await StarletteJSONResponse({"error": "body_too_large"}, status_code=413)(scope, receive, send)
            chunks, size = [], 0
            upload_deadline = time.monotonic() + self.settings.request_timeout_seconds
            while True:
                try:
                    remaining = upload_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except TimeoutError:
                    self.telemetry.rejections.labels("body_timeout").inc()
                    return await StarletteJSONResponse({"error": "request_body_timeout"}, status_code=408)(scope, receive, send)
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.settings.max_request_bytes:
                    self.telemetry.rejections.labels("body_size").inc()
                    return await StarletteJSONResponse({"error": "body_too_large"}, status_code=413)(scope, receive, send)
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return await receive()

            return await self.app(scope, bounded_receive, send)
        return await self.app(scope, receive, send)


class BackendError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status
        super().__init__(code)


async def _json_request(client, method, url, *, headers, payload=None):
    try:
        async with client.stream(method, url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                raise BackendError("upstream_error", 503 if response.status_code == 429 else 502)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise BackendError("upstream_response_too_large")
            try:
                return strict_json_loads(bytes(body))
            except ContractError:
                raise BackendError("invalid_upstream_output") from None
    except httpx.TimeoutException:
        raise BackendError("upstream_timeout", 504) from None
    except httpx.HTTPError:
        raise BackendError("upstream_unavailable", 503) from None


async def _await_disconnect(request):
    while True:
        # The request body has already been consumed. Listen directly for the
        # ASGI disconnect event; is_disconnected() uses a CancelScope which can
        # swallow task cancellation on some Starlette/AnyIO versions.
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _cancellable_generation(request, coroutine, timeout):
    backend = asyncio.create_task(coroutine)
    disconnect = asyncio.create_task(_await_disconnect(request))
    try:
        done, _ = await asyncio.wait((backend, disconnect), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if backend in done:
            return await backend
        if disconnect in done:
            raise BackendError("client_disconnected", 499)
        raise BackendError("upstream_timeout", 504)
    finally:
        for task in (backend, disconnect):
            if not task.done():
                task.cancel()
        await asyncio.gather(backend, disconnect, return_exceptions=True)


def create_app(settings: Settings | None = None, *, transport=None):
    settings = settings or Settings.from_env()
    telemetry = Telemetry()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                limits=httpx.Limits(max_connections=settings.max_concurrency + 2,
                                   max_keepalive_connections=settings.max_concurrency + 2),
                follow_redirects=False, trust_env=False, transport=transport) as client:
            app.state.backend = client
            yield

    app = FastAPI(title="Fund model-serving API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.inflight = 0
    app.state.telemetry = telemetry
    app.add_middleware(RequestGuard, settings=settings, telemetry=telemetry)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, exc):
        telemetry.rejections.labels("validation").inc()
        # Default Pydantic errors contain input values. Never reflect prompts/secrets.
        return JSONResponse({"error": "invalid_request", "fields": [
            {"location": list(error["loc"]), "type": error["type"]}
            for error in exc.errors()]}, status_code=422)

    def upstream_headers(request_id=None):
        headers = {"Content-Type": "application/json"}
        if settings.backend_api_key:
            headers["Authorization"] = "Bearer " + settings.backend_api_key
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            response = await asyncio.wait_for(_json_request(
                app.state.backend, "GET", settings.backend_url.rstrip("/") + "/models",
                headers=upstream_headers()), timeout=settings.readiness_timeout_seconds)
            available = (isinstance(response, dict) and isinstance(response.get("data"), list)
                         and any(isinstance(item, dict) and item.get("id") == settings.model
                                 for item in response["data"]))
            if available:
                return {"status": "ready", "model": settings.model}
        except (BackendError, TimeoutError):
            pass
        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/v1/model-info")
    async def model_info():
        return {"backend": "vllm", "model": settings.model,
                "deployment_environment": settings.deployment_environment,
                "model_revision": settings.model_revision, "vllm_version": settings.vllm_version,
                "provenance_note": "Deployment metadata is operator-declared; verify GPU and pod evidence separately.",
                "limits": {"max_concurrency": settings.max_concurrency,
                           "max_context_tokens": settings.max_context_tokens,
                           "max_output_tokens": settings.max_output_tokens,
                           "max_prompt_chars": settings.max_prompt_chars,
                           "request_timeout_seconds": settings.request_timeout_seconds}}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(telemetry.registry),
                        media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/v1/generate")
    async def generate(body: GenerateRequest, request: Request):
        try:
            validate_symbols(body.symbols, body.task)
            payload = build_chat_payload(model=settings.model, task=body.task, prompt=body.prompt,
                                         symbols=body.symbols, max_tokens=body.max_tokens,
                                         temperature=body.temperature,
                                         max_output_tokens=settings.max_output_tokens,
                                         max_prompt_chars=settings.max_prompt_chars)
        except ContractError:
            telemetry.rejections.labels("validation").inc()
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        if app.state.inflight >= settings.max_concurrency:
            telemetry.rejections.labels("overload").inc()
            telemetry.requests.labels(body.task, "overload").inc()
            return JSONResponse({"error": "overloaded"}, status_code=429, headers={"Retry-After": "1"})
        app.state.inflight += 1  # no await between check/increment: atomic in one event loop
        telemetry.inflight.inc()
        request_id, started = str(uuid.uuid4()), time.perf_counter()
        outcome = "failed"

        async def work():
            tokenized = await _json_request(app.state.backend, "POST",
                                            settings.backend_url.rstrip("/")[:-3] + "/tokenize",
                                            headers=upstream_headers(request_id), payload={
                                                "model": settings.model, "messages": payload["messages"],
                                                "add_generation_prompt": True})
            try:
                check_context(tokenized, body.max_tokens, settings.max_context_tokens)
            except ContractError as exc:
                if str(exc) == "context token budget exceeded":
                    raise BackendError("context_limit", 422) from None
                raise BackendError("invalid_upstream_output") from None
            response = await _json_request(app.state.backend, "POST",
                                          settings.backend_url.rstrip("/") + "/chat/completions",
                                          headers=upstream_headers(request_id), payload=payload)
            try:
                return parse_chat_response(response, task=body.task, symbols=body.symbols,
                                           model=settings.model, max_tokens=body.max_tokens)
            except ContractError:
                raise BackendError("invalid_upstream_output") from None

        try:
            data, input_tokens, output_tokens = await _cancellable_generation(
                request, work(), settings.request_timeout_seconds)
            outcome = "success"
            telemetry.tokens.labels("input").inc(input_tokens)
            telemetry.tokens.labels("output").inc(output_tokens)
            return {"request_id": request_id, "model": settings.model, "task": body.task,
                    "data": data, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                    "latency_seconds": time.perf_counter() - started}
        except BackendError as exc:
            outcome = exc.code
            return JSONResponse({"error": exc.code, "request_id": request_id}, status_code=exc.status)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            app.state.inflight -= 1
            telemetry.inflight.dec()
            telemetry.requests.labels(body.task, outcome).inc()
            telemetry.latency.labels(body.task).observe(time.perf_counter() - started)

    return app
