"""Thin gateway client: a local model via an OpenAI-compatible endpoint, validated by instructor.

Tracer-bullet egress — points straight at Ollama. M1.5 swaps `base_url` to the self-hosted
LiteLLM Proxy (virtual key) and adds named slots, budget caps, and the startup separation
assertion. Privacy rule: never log request/response bodies (only `run_metrics`, later).
"""

import os
from dataclasses import dataclass

import instructor
from openai import AsyncOpenAI

from app.domain.output_schema import RawAttributeGuess


@dataclass(frozen=True)
class GatewayConfig:
    """Where to reach the model and how hard to retry a malformed structured response."""

    base_url: str = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
    model: str = "qwen2.5"
    api_key: str = "ollama"  # Ollama ignores it; M1.5 replaces it with the proxy virtual key
    max_retries: int = 2  # bounded repair-retry (llm-gateway.md) — never an infinite loop


def default_gateway_config() -> GatewayConfig:
    """Build the gateway config from the environment, defaulting to local Ollama."""
    base = GatewayConfig()
    return GatewayConfig(
        base_url=os.getenv("LLM_BASE_URL", base.base_url),
        model=os.getenv("LLM_MODEL", base.model),
        api_key=os.getenv("LLM_API_KEY", base.api_key),
    )


class GatewayClient:
    """Wraps an instructor-patched OpenAI-compatible client to return validated objects."""

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self._config = config or default_gateway_config()
        self._client = instructor.from_openai(
            AsyncOpenAI(base_url=self._config.base_url, api_key=self._config.api_key),
            mode=instructor.Mode.JSON,
        )

    async def profile_attribute(self, *, system_prompt: str, content: str) -> RawAttributeGuess:
        """Run one Profiler pass over `content` → a schema-validated `RawAttributeGuess`."""
        guess: RawAttributeGuess = await self._client.chat.completions.create(
            model=self._config.model,
            response_model=RawAttributeGuess,
            max_retries=self._config.max_retries,
            temperature=0,  # deterministic single pass (output-schema.md §11)
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
        )
        return guess
