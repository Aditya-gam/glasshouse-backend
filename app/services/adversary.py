"""Independent adversary (defend/independent-adversary.md, M3.1) — the held-out re-attack.

The safeguard that makes Defend honest: a rewrite's success is proven by a **different-model**
adversary re-attacking the content, never the rewriter's self-report (editor ≠ judge). It reuses
the full attack ensemble through the gateway `adversary` slot — which the startup separation
assertion keeps distinct from the profiler/anonymizer/judge — and runs **blind**: it sees only the
content it is handed, never the original text, the edit diff, or the attacker's reasoning. Scoped
to the target attribute (cost). Returns the adversary's consensus guess and whether the true value
is still recoverable in its top-3 (the falsifiable safety claim). Content decrypted in memory only,
never logged.

This is the primitive; the before→after delta with intervals + calibration lands with the noise
model (M3.4) and the remediation worker (M3.7), which measure **both** endpoints with this same
adversary so only the edited content changes between the two.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain.eval_match import candidate_hits
from app.domain.output_schema import AttributeCode, AttributeGuess, RawAttributeGuess
from app.gateway.prompts import build_user_prompt
from app.services.geocoding import Geocoder
from app.services.inference import ProfileFn, ensemble_consensus, resolve_ensemble
from app.services.occupation import OccupationJudge, StringMatchJudge


class Adversary(Protocol):
    """The held-out re-attack capability (the gateway's `adversary` slot; test fakes conform)."""

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]: ...


@dataclass(frozen=True)
class AdversaryFinding:
    """One attribute re-attacked by the held-out adversary: its guess + the value-recovery flag."""

    attribute: AttributeCode
    guess: AttributeGuess  # the adversary's consensus (abstained if it can't infer the attribute)
    value_recovered: bool  # the user's true value is still in the adversary's top-3


def value_recovered(attribute: AttributeCode, guess: AttributeGuess, true_value: object) -> bool:
    """Whether the true value is still recoverable — in the adversary's top-3, by the match rules.

    Reuses the benchmark matcher so "recovered" means the same thing here as in eval scoring. The
    safety claim is the *flip* of this across a rewrite (recovered before → not after).
    """
    return any(hit for hit, _ in candidate_hits(attribute, guess, true_value))


def _abstained(attribute: AttributeCode) -> AttributeGuess:
    return AttributeGuess(attribute=attribute, modality="text", status="abstained", candidates=[])


async def attack_content(
    profile_fn: ProfileFn,
    content: Sequence[tuple[str, str]],
    *,
    target_attribute: AttributeCode,
    true_value: object,
    geocoder: Geocoder,
    judge: OccupationJudge | None = None,
    n_runs: int | None = None,
    temperature: float | None = None,
) -> AdversaryFinding:
    """Re-attack `content` (item id, text) through a held-out slot, scoped to one attribute.

    The re-attack primitive parameterized by the slot's profile function — the **evaluator**
    adversary (M3.1) and the **feedback** adversary (M3.3 anonymizer loop) both use it, each through
    its own slot. Runs the same N-run ensemble the Profiler uses, then reports the target
    attribute's consensus + whether `true_value` survives in the top-3. Blind: only content reaches
    the model.
    """
    runs, temp = resolve_ensemble(n_runs, temperature)
    prompt = build_user_prompt(list(content))
    guesses = await ensemble_consensus(
        profile_fn,
        prompt,
        geocoder,
        runs=runs,
        temp=temp,
        allow_special_category=True,  # re-attacks whatever attribute triggered defense
        judge=judge or StringMatchJudge(),
    )
    guess = next((g for g in guesses if g.attribute == target_attribute), None) or _abstained(
        target_attribute
    )
    return AdversaryFinding(
        attribute=target_attribute,
        guess=guess,
        value_recovered=value_recovered(target_attribute, guess, true_value),
    )


async def adversary_attack(
    adversary: Adversary,
    content: Sequence[tuple[str, str]],
    *,
    target_attribute: AttributeCode,
    true_value: object,
    geocoder: Geocoder,
    judge: OccupationJudge | None = None,
    n_runs: int | None = None,
    temperature: float | None = None,
) -> AdversaryFinding:
    """Re-attack `content` with the held-out **evaluator** adversary (the `adversary` slot)."""
    return await attack_content(
        lambda text, temperature: adversary.adversary_profile_all(
            content=text, temperature=temperature
        ),
        content,
        target_attribute=target_attribute,
        true_value=true_value,
        geocoder=geocoder,
        judge=judge,
        n_runs=n_runs,
        temperature=temperature,
    )
