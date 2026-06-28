"""Unit (M1.5): the gateway startup assertions — separation chain + provider-key boundary."""

import pytest

from app.gateway.slots import (
    _PROVIDER_KEY_ENV,
    _SLOT_MODELS,
    ProviderKeyLeakError,
    SeparationError,
    assert_no_provider_keys,
    assert_slot_separation,
    slot_model,
)


def test_separation_holds_for_shipped_profiles() -> None:
    # The local + cloud profiles must satisfy the chain (no raise).
    assert_slot_separation("local")
    assert_slot_separation("cloud")


def test_separation_fails_on_a_chain_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    collided = dict(_SLOT_MODELS["local"])
    collided["adversary"] = collided["profiler"]  # evaluator == profiler is forbidden
    monkeypatch.setitem(_SLOT_MODELS, "_collided", collided)
    with pytest.raises(SeparationError):
        assert_slot_separation("_collided")


def test_profiler_slot_resolves() -> None:
    assert slot_model("local", "profiler") == "qwen2.5"


def test_provider_key_in_app_env_raises_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-in-the-app")
    with pytest.raises(ProviderKeyLeakError):
        assert_no_provider_keys("prod")


def test_provider_key_only_warns_in_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dev")
    assert_no_provider_keys("local")  # warns, does not block dev


def test_no_provider_keys_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROVIDER_KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    assert_no_provider_keys("prod")  # nothing set → no raise
