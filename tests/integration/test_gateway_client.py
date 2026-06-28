"""Gateway client: mocked wiring (runs in CI) + an opt-in live call to Ollama (local only)."""

from typing import get_args
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.output_schema import AttributeCode, RawAttributeGuess, RawCandidate
from app.gateway.client import GatewayClient, GatewayConfig, default_gateway_config

# Minimal tracer prompt; the real attack prompt is built at M1.7 (prompts/attack-text.md).
_TRACER_PROMPT = (
    "You are a privacy auditor. From the user's text, infer their most likely LOCATION. "
    "Return candidates (value_text, self_confidence 0-1, evidence), or status=abstained "
    "with no candidates if there is no signal."
)
_SAMPLE_TEXT = "Just moved — love my morning walk to Gas Works Park before my PST standup."

_ATTRIBUTES = set(get_args(AttributeCode))


async def test_profile_attribute_delegates_to_instructor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client asks for our response_model at a deterministic temperature — no live model."""
    client = GatewayClient(GatewayConfig(model="stub"))
    canned = RawAttributeGuess(
        attribute="location",
        status="inferred",
        candidates=[RawCandidate(value_text="Seattle", self_confidence=0.8)],
    )
    mock_create = AsyncMock(return_value=canned)
    monkeypatch.setattr(client._client.chat.completions, "create", mock_create)

    result = await client.profile_attribute(system_prompt="sys", content="text")

    assert result is canned
    mock_create.assert_awaited_once()
    assert mock_create.await_args is not None
    kwargs = mock_create.await_args.kwargs
    assert kwargs["response_model"] is RawAttributeGuess
    assert kwargs["temperature"] == 0
    assert kwargs["max_retries"] == 2


def _ollama_has_model(model: str) -> bool:
    """True if a local Ollama is serving `model` (so the live test can run; else it skips)."""
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        names = [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return False
    return any(name == model or name.startswith(f"{model}:") for name in names)


_CONFIG = default_gateway_config()


@pytest.mark.skipif(
    not _ollama_has_model(_CONFIG.model),
    reason="requires a running Ollama serving the configured model",
)
async def test_live_profile_attribute_returns_validated_guess() -> None:
    client = GatewayClient(_CONFIG)
    guess = await client.profile_attribute(system_prompt=_TRACER_PROMPT, content=_SAMPLE_TEXT)
    # Stochastic output: assert the contract, not exact values (instructor already validated shape).
    assert guess.attribute in _ATTRIBUTES
    assert guess.status in {"inferred", "abstained"}
    for candidate in guess.candidates:
        assert 0.0 <= candidate.self_confidence <= 1.0
