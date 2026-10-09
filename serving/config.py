"""Environment-only serving configuration; credentials are never persisted."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from core.llm.vllm import validate_backend_url


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    backend_url: str = "http://127.0.0.1:8000/v1"
    backend_api_key: str | None = field(default=None, repr=False)
    model: str = "fund-llm"
    request_timeout_seconds: float = 30.0
    readiness_timeout_seconds: float = 5.0
    max_concurrency: int = 8
    max_context_tokens: int = 4096
    max_output_tokens: int = 1024
    max_prompt_chars: int = 24000
    max_request_bytes: int = 65536
    deployment_environment: str = "unverified"
    model_revision: str = "unknown"
    vllm_version: str = "unknown"

    def __post_init__(self):
        if not isinstance(self.api_key, str) or len(self.api_key) < 16:
            raise ValueError("MODEL_API_KEY must be an environment secret of at least 16 characters")
        validate_backend_url(self.backend_url)
        if not isinstance(self.model, str) or not 1 <= len(self.model) <= 256:
            raise ValueError("invalid served model name")
        for name in ("request_timeout_seconds", "readiness_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
                raise ValueError("invalid serving timeout")
        bounds = {"max_concurrency": (1, 128), "max_context_tokens": (256, 131072),
                  "max_output_tokens": (1, 1024), "max_prompt_chars": (1, 24000),
                  "max_request_bytes": (1024, 65536)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid serving resource bound")
        if self.max_output_tokens >= self.max_context_tokens:
            raise ValueError("output budget must be smaller than context")
        if self.deployment_environment not in ("unverified", "gpu-kubernetes", "test-double"):
            raise ValueError("invalid deployment environment declaration")

    @classmethod
    def from_env(cls):
        return cls(
            api_key=os.environ.get("MODEL_API_KEY", ""),
            backend_url=os.environ.get("MODEL_BACKEND_URL", "http://127.0.0.1:8000/v1"),
            backend_api_key=os.environ.get("MODEL_BACKEND_API_KEY"),
            model=os.environ.get("MODEL_NAME", "fund-llm"),
            request_timeout_seconds=float(os.environ.get("MODEL_REQUEST_TIMEOUT_SECONDS", "30")),
            readiness_timeout_seconds=float(os.environ.get("MODEL_READINESS_TIMEOUT_SECONDS", "5")),
            max_concurrency=int(os.environ.get("MODEL_MAX_CONCURRENCY", "8")),
            max_context_tokens=int(os.environ.get("MODEL_MAX_CONTEXT_TOKENS", "4096")),
            max_output_tokens=int(os.environ.get("MODEL_MAX_OUTPUT_TOKENS", "1024")),
            max_prompt_chars=int(os.environ.get("MODEL_MAX_PROMPT_CHARS", "24000")),
            max_request_bytes=int(os.environ.get("MODEL_MAX_REQUEST_BYTES", "65536")),
            deployment_environment=os.environ.get("MODEL_DEPLOYMENT_ENVIRONMENT", "unverified"),
            model_revision=os.environ.get("MODEL_REVISION", "unknown"),
            vllm_version=os.environ.get("VLLM_VERSION", "unknown"),
        )
