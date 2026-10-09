"""Real vLLM HTTP client, with no optional SDK required by the trading runtime.

The gateway performs a whole-request async deadline. This synchronous client
uses socket timeouts and a deadline across tokenize/generate, including
deadline-aware chunked body reads; no retries hide failed calls. Never log
exceptions' chained contents or request bodies.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

from .contracts import (ContractError, MAX_OUTPUT_TOKENS, MAX_PROMPT_CHARS,
                        MAX_RESPONSE_BYTES, build_chat_payload, check_context,
                        parse_chat_response, strict_json_loads, validate_data)


class VLLMError(RuntimeError):
    def __init__(self, code: str, status_code: int | None = None):
        self.code, self.status_code = code, status_code
        super().__init__("vLLM request failed: " + code)


@dataclass(frozen=True)
class GenerationResult:
    data: dict
    input_tokens: int
    output_tokens: int
    model: str
    request_id: str


def validate_backend_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/v1"):
        raise ValueError("backend URL must be an HTTP(S) /v1 URL without credentials")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Sending a backend bearer secret to a redirect target is never acceptable.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VLLMClient:
    def __init__(self, base_url="http://127.0.0.1:8000/v1", model="fund-llm",
                 api_key=None, timeout=30.0, max_context_tokens=4096,
                 max_output_tokens=MAX_OUTPUT_TOKENS, max_prompt_chars=MAX_PROMPT_CHARS):
        self.base_url = validate_backend_url(base_url)
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("invalid served model name")
        if type(timeout) not in (int, float) or not 0 < timeout <= 600:
            raise ValueError("timeout must be in (0,600]")
        if type(max_context_tokens) is not int or max_context_tokens < 256:
            raise ValueError("invalid context limit")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError("invalid output limit")
        self.model, self.api_key, self.timeout = model, api_key, float(timeout)
        self.max_context_tokens = max_context_tokens
        self.max_output_tokens, self.max_prompt_chars = max_output_tokens, max_prompt_chars
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, url, payload, deadline, request_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VLLMError("timeout")
        headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(url, headers=headers,
                                     data=None if payload is None else json.dumps(payload, allow_nan=False).encode())
        try:
            with self._opener.open(req, timeout=remaining) as response:
                raw = bytearray()
                # read(n) can keep accepting a slow trickle indefinitely. read1
                # performs at most one underlying read, then we recompute the
                # remaining wall-clock budget, including TLS sockets.
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                while True:
                    if response.isclosed():
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VLLMError("timeout")
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise VLLMError("response_too_large")
                if time.monotonic() > deadline:
                    raise VLLMError("timeout")
                return strict_json_loads(bytes(raw))
        except urllib.error.HTTPError as exc:
            raise VLLMError("upstream_http", exc.code) from None
        except (TimeoutError, socket.timeout):
            raise VLLMError("timeout") from None
        except urllib.error.URLError as exc:
            code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "unavailable"
            raise VLLMError(code) from None
        except ContractError:
            raise VLLMError("invalid_output") from None

    def ready(self):
        response = self._request(self.base_url + "/models", None,
                                 time.monotonic() + self.timeout, str(uuid.uuid4()))
        return (isinstance(response, dict) and isinstance(response.get("data"), list)
                and any(isinstance(item, dict) and item.get("id") == self.model
                        for item in response["data"]))

    def generate(self, *, task, prompt, symbols=None, max_tokens=512, temperature=0.0):
        try:
            payload = build_chat_payload(model=self.model, task=task, prompt=prompt,
                                        symbols=symbols, max_tokens=max_tokens, temperature=temperature,
                                        max_output_tokens=self.max_output_tokens,
                                        max_prompt_chars=self.max_prompt_chars)
        except ContractError:
            raise VLLMError("invalid_request") from None
        request_id, deadline = str(uuid.uuid4()), time.monotonic() + self.timeout
        tokenized = self._request(self.base_url[:-3] + "/tokenize", {
            "model": self.model, "messages": payload["messages"], "add_generation_prompt": True,
        }, deadline, request_id)
        try:
            check_context(tokenized, max_tokens, self.max_context_tokens)
        except ContractError as exc:
            code = "context_limit" if str(exc) == "context token budget exceeded" else "invalid_output"
            raise VLLMError(code) from None
        response = self._request(self.base_url + "/chat/completions", payload, deadline, request_id)
        try:
            data, input_tokens, output_tokens = parse_chat_response(
                response, task=task, symbols=symbols, model=self.model, max_tokens=max_tokens)
        except ContractError:
            raise VLLMError("invalid_output") from None
        return GenerationResult(data, input_tokens, output_tokens, self.model, request_id)


class GatewayClient(VLLMClient):
    """Same interface over the private validated gateway, not raw vLLM.

    Use this for fund callers to preserve gateway admission, cancellation,
    context checks, and metrics. The API key is the gateway key, never the raw
    backend key. It inherits bounded reads, socket timeout, and redirect refusal.
    """
    def __init__(self, base_url="http://127.0.0.1:8081/v1", **kwargs):
        super().__init__(base_url=base_url, **kwargs)

    def ready(self):
        response = self._request(self.base_url[:-3] + "/health/ready", None,
                                 time.monotonic() + self.timeout, str(uuid.uuid4()))
        return (isinstance(response, dict) and response.get("status") == "ready"
                and response.get("model") == self.model)

    def model_info(self):
        """Operator-declared provenance only; this does not certify GPU usage."""
        response = self._request(self.base_url + "/model-info", None,
                                 time.monotonic() + self.timeout, str(uuid.uuid4()))
        if (not isinstance(response, dict) or response.get("backend") != "vllm"
                or response.get("model") != self.model):
            raise VLLMError("invalid_output")
        allowed = ("backend", "model", "deployment_environment", "model_revision",
                   "vllm_version", "provenance_note", "limits")
        return {key: response[key] for key in allowed if key in response}

    def generate(self, *, task, prompt, symbols=None, max_tokens=512, temperature=0.0):
        try:
            build_chat_payload(model=self.model, task=task, prompt=prompt, symbols=symbols,
                               max_tokens=max_tokens, temperature=temperature,
                               max_output_tokens=self.max_output_tokens,
                               max_prompt_chars=self.max_prompt_chars)
        except ContractError:
            raise VLLMError("invalid_request") from None
        payload = {"task": task, "prompt": prompt, "max_tokens": max_tokens,
                   "temperature": temperature}
        if symbols is not None:
            payload["symbols"] = symbols
        response = self._request(self.base_url + "/generate", payload,
                                 time.monotonic() + self.timeout, str(uuid.uuid4()))
        try:
            if (not isinstance(response, dict) or response.get("model") != self.model
                    or response.get("task") != task):
                raise ContractError("gateway response mismatch")
            data = validate_data(task, response.get("data"), symbols)
            usage = response.get("usage")
            if (not isinstance(usage, dict)
                    or type(usage.get("input_tokens")) is not int
                    or not 0 <= usage["input_tokens"] <= self.max_context_tokens
                    or type(usage.get("output_tokens")) is not int
                    or not 0 <= usage["output_tokens"] <= max_tokens):
                raise ContractError("invalid gateway measured usage")
            request_id = str(uuid.UUID(response["request_id"]))
        except (ContractError, KeyError, ValueError, TypeError, AttributeError):
            raise VLLMError("invalid_output") from None
        return GenerationResult(data, usage["input_tokens"], usage["output_tokens"],
                                self.model, request_id)
