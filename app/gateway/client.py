"""Thin gateway client — the single model egress through the LiteLLM Proxy (llm-gateway.md).

The app talks OpenAI-compatible HTTP to the proxy at an internal `base_url`, authenticating with
only the proxy's **virtual key** (provider keys live in the proxy). Calls name a **slot**
(`profiler`, …); the proxy resolves it to a model per the active profile. `instructor` validates
the response against our schema. Privacy rule: never log request/response bodies (only metadata).
"""

import instructor
from openai import AsyncOpenAI

from app.core.config import GatewaySettings, get_gateway_settings
from app.domain.output_schema import RawAttributeGuess
from app.gateway.slots import Slot


class GatewayClient:
    """Wraps an instructor-patched OpenAI-compatible client pointed at the proxy."""

    def __init__(self, settings: GatewaySettings | None = None) -> None:
        self._settings = settings or get_gateway_settings()
        self._client = instructor.from_openai(
            AsyncOpenAI(
                base_url=self._settings.litellm_base_url,
                # The OpenAI client requires a non-empty key; a keyless local proxy still needs a
                # placeholder. The proxy enforces the real virtual key (+ budget) in prod.
                api_key=self._settings.litellm_virtual_key or "sk-local-dev",
            ),
            mode=instructor.Mode.JSON,
        )

    async def profile_attribute(self, *, system_prompt: str, content: str) -> RawAttributeGuess:
        """Run one Profiler pass over `content` → a schema-validated `RawAttributeGuess`."""
        slot: Slot = "profiler"
        guess: RawAttributeGuess = await self._client.chat.completions.create(
            model=slot,  # the proxy resolves the slot to a model per the active profile
            response_model=RawAttributeGuess,
            max_retries=self._settings.gateway_max_retries,
            temperature=0,  # deterministic single pass (output-schema.md §11)
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
        )
        return guess
