"""Thin gateway client — the single model egress through the LiteLLM Proxy (llm-gateway.md).

The app talks OpenAI-compatible HTTP to the proxy at an internal `base_url`, authenticating with
only the proxy's **virtual key** (provider keys live in the proxy). Calls name a **slot**
(`profiler`, …); the proxy resolves it to a model per the active profile. `instructor` validates
the response against our schema. Privacy rule: never log request/response bodies (only metadata).
"""

from typing import Literal, Protocol

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.core.config import GatewaySettings, get_gateway_settings
from app.domain.output_schema import RawAttributeGuess, RawProfilerOutput
from app.gateway.prompts import (
    ANONYMIZE_SYSTEM,
    ATTACK_TEXT_SYSTEM,
    JUDGE_OCCUPATION_SYSTEM,
    MATCH_JUDGE_SYSTEM,
    build_anonymize_prompt,
)
from app.gateway.slots import Slot

MatchVerdictLabel = Literal["yes", "partial", "no"]
GeoLevel = Literal["country", "region", "city", "neighborhood"]
EditOperation = Literal["generalize", "remove_span", "remove_item"]


class AnonymizerEdit(BaseModel):
    """The anonymizer emission (anonymize_text_v1): reason-then-edit, one truthful localized edit."""  # noqa: E501

    reasoning: str
    operation: EditOperation
    edited_text: str
    note: str  # a short user-facing description of what changed


class _Equivalence(BaseModel):
    """The judge slot's minimal emission: are two values the same? (shallow, for local models)."""

    equivalent: bool


class MatchJudgeResult(BaseModel):
    """The match-judge emission (match_judge_v1): reason-then-verdict, reference-anchored.

    `reasoning` comes first so the model commits after reasoning; `level` is the finest agreeing
    hierarchy level for geo (None for non-geo); `confidence` routes low/partial to human spot-check.
    """

    reasoning: str
    verdict: MatchVerdictLabel
    level: GeoLevel | None = None
    confidence: float = Field(ge=0, le=1)


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

    async def _joint_pass(
        self, *, content: str, temperature: float, slot: Slot
    ) -> list[RawAttributeGuess]:
        """The joint 8-attribute attack pass through one gateway slot (`attack_text_v1` system
        prompt). Shared by the Profiler and the held-out adversary; the slot picks the model."""
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

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        """The joint pass: one profiler-slot call inferring all 8 attributes (M1.7).

        `content` is the datamarked user prompt (gateway/prompts.build_user_prompt); the system
        prompt is `attack_text_v1`. `temperature` is 0 for the deterministic dev pass and raised
        for the self-consistency ensemble's N runs (confidence-and-self-consistency.md §2). Returns
        the emission guesses; the normalizer canonicalizes them.
        """
        return await self._joint_pass(content=content, temperature=temperature, slot="profiler")

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        """The held-out adversary re-attack (defend/independent-adversary.md, M3.1) — the same joint
        pass through the `adversary` slot (a **different** model than the profiler; the startup
        separation assertion guarantees it). Blind: it sees only the content it is given, never the
        original text or the edit — so the rewriter can't game a judge it can't see.
        """
        return await self._joint_pass(content=content, temperature=temperature, slot="adversary")

    async def feedback_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        """The **feedback** adversary re-attack for the anonymizer loop (defend/text-remediation.md,
        M3.3) — the joint pass through the `feedback_adversary` slot. Distinct from the *evaluator*
        `adversary` slot (the separation chain), so the editor refines against one model but is
        proven by a different, held-out one — it cannot game the final judge.
        """
        return await self._joint_pass(
            content=content, temperature=temperature, slot="feedback_adversary"
        )

    async def anonymize(
        self, *, text: str, spans: list[str], attribute: str, feedback: str | None = None
    ) -> AnonymizerEdit:
        """One truthful localized privacy edit through the `anonymizer` slot (defend M3.3).

        Given the text + the leaking spans (+ optional adversary feedback to refine against),
        returns a minimal, generalization-first rewrite. The anonymizer is separate from the
        feedback/evaluator adversaries and the judges (the separation chain). Content never logged.
        """
        result: AnonymizerEdit = await self._client.chat.completions.create(
            model="anonymizer",
            response_model=AnonymizerEdit,
            max_retries=self._settings.gateway_max_retries,
            temperature=0.3,  # a little latitude to find a good paraphrase; still low
            messages=[
                {"role": "system", "content": ANONYMIZE_SYSTEM},
                {
                    "role": "user",
                    "content": build_anonymize_prompt(text, spans, attribute, feedback),
                },
            ],
        )
        return result

    async def judge_same(self, a: str, b: str) -> bool:
        """Semantic-equivalence judge (the `judge` slot) — clusters occupation guesses (M1.8b).

        The judge model is separate from the profiler (the slot separation chain), so a guess is not
        de-duplicated by the same model that produced it.
        """
        slot: Slot = "judge"
        result: _Equivalence = await self._client.chat.completions.create(
            model=slot,
            response_model=_Equivalence,
            max_retries=self._settings.gateway_max_retries,
            temperature=0,
            messages=[
                {"role": "system", "content": JUDGE_OCCUPATION_SYSTEM},
                {"role": "user", "content": f"A: {a}\nB: {b}\nSame profession?"},
            ],
        )
        return result.equivalent

    async def judge_match(
        self, *, attribute: str, prediction: str, ground_truth: str
    ) -> MatchJudgeResult:
        """Reference-anchored match judge (`match_judge_v1`, `judge` slot) — eval scoring (M2.3).

        Decides whether a predicted value is equivalent to the ground-truth label for ambiguous
        cases (occupation semantics; geo name variants the deterministic matcher can't resolve). The
        judge slot is separate from the profiler, so eval accuracy is not graded by the attacker.
        """
        slot: Slot = "judge"
        result: MatchJudgeResult = await self._client.chat.completions.create(
            model=slot,
            response_model=MatchJudgeResult,
            max_retries=self._settings.gateway_max_retries,
            temperature=0,
            messages=[
                {"role": "system", "content": MATCH_JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"attribute: {attribute}\n"
                        f"PREDICTION: {prediction}\n"
                        f"GROUND_TRUTH: {ground_truth}"
                    ),
                },
            ],
        )
        return result
