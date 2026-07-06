"""Decoy edit (defend/text-remediation.md §2, opt-in M3.6) — false-attribute injection.

OFF by default. Decoy mode (IncogniText) injects a plausible FALSE cue so an adversary confidently
guesses the WRONG value (>90% leakage reduction). Because it suggests publishing a falsehood, it
is **double-gated** (services.consent.require_decoy): a standing `decoy` consent AND a per-use
confirm — a decoy is never auto-selected, and the truthful options are always shown alongside (FE).

This module is the pure edit engine. It refuses to fabricate unless `confirmed` is true — a
belt-and-suspenders guard so no code path can auto-inject a falsehood even if a caller forgets the
DB gate. The standing-consent factor lives in require_decoy (it needs the connection). Advise-only:
returns a *suggested* edit, never writes anywhere. The edit runs on the editor slot, distinct from
the held-out proving adversary (the separation chain), so M3.7 proves it against a model it can't
game. Content decrypted in memory, never logged.
"""

from dataclasses import dataclass
from typing import Protocol

from app.domain.output_schema import AttributeCode
from app.gateway.client import DecoyEdit
from app.services.consent import DecoyNotConfirmedError


class DecoyEditor(Protocol):
    """One decoy edit (the gateway `anonymizer`/editor slot; test fakes conform)."""

    async def decoy(
        self, *, text: str, spans: list[str], attribute: str, decoy_value: str | None = None
    ) -> DecoyEdit: ...


@dataclass(frozen=True)
class DecoyResult:
    """The suggested decoy edit for one item + the false value the adversary is now steered to."""

    item_id: str
    original_text: str
    edited_text: str
    decoy_value: str  # the plausible-but-false value now implied (M3.7 shows it as `misled`)
    note: str


async def generate_decoy(
    editor: DecoyEditor,
    item: tuple[str, str],
    *,
    target_attribute: AttributeCode,
    spans: list[str],
    confirmed: bool,
    decoy_value: str | None = None,
) -> DecoyResult:
    """Produce one decoy edit for `item`, injecting a plausible false cue for `target_attribute`.

    Fails closed unless `confirmed` is true — the engine itself refuses to fabricate without the
    per-use confirm, so no path auto-injects a falsehood (the standing-consent factor is enforced
    separately by services.consent.require_decoy at the edge/worker). Returns the suggested edit and
    the false value now implied; M3.7 proves it against the held-out adversary and shows it beside
    the truthful options.
    """
    if not confirmed:
        raise DecoyNotConfirmedError()
    item_id, original = item
    edit = await editor.decoy(
        text=original, spans=spans, attribute=target_attribute, decoy_value=decoy_value
    )
    return DecoyResult(
        item_id=item_id,
        original_text=original,
        edited_text=edit.edited_text,
        decoy_value=edit.decoy_value,
        note=edit.note,
    )
