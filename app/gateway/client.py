"""Thin gateway client — the single model egress through the LiteLLM Proxy (llm-gateway.md).

The app talks OpenAI-compatible HTTP to the proxy at an internal `base_url`, authenticating with
only the proxy's **virtual key** (provider keys live in the proxy). Calls name a **slot**
(`profiler`, …); the proxy resolves it to a model per the active profile. `instructor` validates
the response against our schema. Privacy rule: never log request/response bodies (only metadata).
"""

from typing import Protocol

import instructor
from openai import AsyncOpenAI

from app.core.config import GatewaySettings, get_gateway_settings
from app.domain.output_schema import RawAttributeGuess, RawProfilerOutput
from app.gateway.prompts import ATTACK_TEXT_SYSTEM
from app.gateway.slots import Slot


class Profiler(Protocol):
    """The joint-pass capability the attack service needs (GatewayClient + test fakes conform)."""

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]: ...


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

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        """The joint pass: one profiler-slot call inferring all 8 attributes (M1.7).

        `content` is the datamarked user prompt (gateway/prompts.build_user_prompt); the system
        prompt is `attack_text_v1`. `temperature` is 0 for the deterministic dev pass and raised
        for the self-consistency ensemble's N runs (confidence-and-self-consistency.md §2). Returns
        the emission guesses; the normalizer canonicalizes them.
        """
        slot: Slot = "profiler"
        output: RawProfilerOutput = await self._client.chat.completions.create(
            model=slot,
            response_model=RawProfilerOutput,
            max_retries=self._settings.gateway_max_retries,
            temperature=temperature,
            messages=[
                {"role": "system", "content": ATTACK_TEXT_SYSTEM},
                {"role": "user", "content": content},
            ],
        )
        return output.guesses
