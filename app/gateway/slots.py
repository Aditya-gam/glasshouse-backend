"""Model slots + the startup separation assertions (llm-gateway.md §47).

Slots are capability roles the proxy resolves to concrete models per profile; the app passes the
**slot name** to the proxy and never sees provider models or keys. Two invariants are asserted at
startup, fail-closed:

  - **Separation chain** — `profiler ≠ anonymizer ≠ feedback_adversary ≠ adversary ≠ judge` resolve
    to distinct models, so the defend proof is never graded by the model the edit was tuned to beat
    (defend/text-remediation.md §1, feasibility-and-cost.md §53).
  - **Provider-key boundary** — no provider API key in the app env; keys live in the proxy only
    (CLAUDE.md). Raises in prod-like envs; warns in local/test so dev isn't blocked.

The per-profile map mirrors `gateway/config.yaml` (the proxy's authoritative routing) — changing a
slot's model means updating both (the llm-gateway.md traceability edge). Local names need not be
pulled until their slot is first used; only `profiler` runs before M3.
"""

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

Slot = Literal[
    "profiler",
    "vlm",
    "anonymizer",
    "adversary",
    "feedback_adversary",
    "judge",
    "inpaint",
    "tagger",
]

_SLOT_MODELS: dict[str, dict[Slot, str]] = {
    "local": {
        "profiler": "qwen2.5",
        "vlm": "qwen2.5-vl",
        "anonymizer": "phi4",
        "feedback_adversary": "mistral",
        "adversary": "llama3.2",
        "judge": "gemma3",
        "inpaint": "sdxl",
        "tagger": "qwen2.5",
    },
    "cloud": {
        "profiler": "anthropic/claude-sonnet",
        "vlm": "google/gemini-pro-vision",
        "anonymizer": "openai/gpt-4o-mini",
        "feedback_adversary": "google/gemini-flash",
        "adversary": "openai/gpt-4o",
        "judge": "anthropic/claude-haiku",
        "inpaint": "stability/sdxl",
        "tagger": "openai/gpt-4o-mini",
    },
}

# The defend separation chain: these slots must resolve to pairwise-distinct models.
_SEPARATION_CHAIN: tuple[Slot, ...] = (
    "profiler",
    "anonymizer",
    "feedback_adversary",
    "adversary",
    "judge",
)

# Keys that would let the app reach a provider directly, bypassing the proxy boundary.
_PROVIDER_KEY_ENV: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
)

_NON_PROD_ENVIRONMENTS = frozenset({"local", "test"})


class SeparationError(RuntimeError):
    """A forbidden slot collision — the gateway fails closed at startup (rigor invariant)."""


class ProviderKeyLeakError(RuntimeError):
    """A provider API key is set in the app env — it must live in the proxy, never the app."""


def slot_model(profile: str, slot: Slot) -> str:
    """The model a slot resolves to under the active profile."""
    return _SLOT_MODELS[profile][slot]


def assert_slot_separation(profile: str) -> None:
    """Fail closed unless the separation-chain slots resolve to pairwise-distinct models."""
    seen: dict[str, Slot] = {}
    for slot in _SEPARATION_CHAIN:
        model = _SLOT_MODELS[profile][slot]
        clash = seen.get(model)
        if clash is not None:
            raise SeparationError(
                f"slot '{slot}' shares model '{model}' with '{clash}' under profile '{profile}'; "
                "the separation chain requires distinct models"
            )
        seen[model] = slot


def assert_no_provider_keys(environment: str) -> None:
    """Fail closed (prod) / warn (local/test) if any provider key is present in the app env."""
    leaked = [name for name in _PROVIDER_KEY_ENV if os.getenv(name)]
    if not leaked:
        return
    message = (
        "provider keys must live in the LiteLLM proxy, not the app — "
        f"found in env: {', '.join(leaked)}"
    )
    if environment in _NON_PROD_ENVIRONMENTS:
        logger.warning(message)
        return
    raise ProviderKeyLeakError(message)
