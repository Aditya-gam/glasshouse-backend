"""Remediations endpoints — the defend screen's proven, advise-only frontier (M3.8 read).

`GET /v1/remediations/{id}` reads one remediation **run**'s frontier (the id is the `run_id` from
`POST /v1/runs {type:"remediation"}`); `?inference_id=` lists the frontier(s) for an inference, and
synthesizes a `cant_break` result when a run localized nothing. Each option carries its proven
`after` reliability + value-recovery, the utility, and the original/edited text (the FE diffs).
Advise-only: responses are suggestions + proven deltas; the product never applies them. Not logged.
"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_current_user, get_scoped_session
from app.api.errors import NotFound
from app.api.v1.remediation_assembly import assemble_remediation
from app.api.v1.schemas import Reliability, RemediationRead
from app.db.crypto import get_master_key
from app.domain.eval_match import render_value
from app.domain.output_schema import AttributeCode, AttributeValue
from app.repositories import inferences as inferences_repo
from app.repositories import items as items_repo
from app.repositories import remediations as remediations_repo

router = APIRouter(prefix="/v1/remediations", tags=["remediations"])

_VALUE_ADAPTER: TypeAdapter[AttributeValue] = TypeAdapter(AttributeValue)
_ZERO = Reliability(point=0.0, lo=0.0, hi=0.0)


def _rendered_value(attribute_code: str, value: dict[str, object]) -> str:
    return render_value(cast(AttributeCode, attribute_code), _VALUE_ADAPTER.validate_python(value))


async def _assemble_run(
    conn: AsyncConnection, run_id: UUID, master_key: str
) -> RemediationRead | None:
    """Assemble one run's frontier, or None when the run produced no options (not readable)."""
    rows = await remediations_repo.list_frontier_by_run(conn, run_id, master_key)
    if not rows:
        return None
    inference_id = await remediations_repo.get_run_inference_id(conn, run_id)
    target = (
        None
        if inference_id is None
        else await inferences_repo.get_inference_target(conn, inference_id, master_key)
    )
    if target is None or target.value is None:
        return None
    items = await items_repo.list_items_with_text(conn, target.profile_id, master_key)
    item_texts = {str(item.id): item.text for item in items}
    # the before endpoint is shared across options (measured once) — take it from any row.
    before = Reliability(
        point=float(rows[0].confidence_before),
        lo=float(rows[0].ci_before.get("lo", rows[0].confidence_before)),
        hi=float(rows[0].ci_before.get("hi", rows[0].confidence_before)),
    )
    return assemble_remediation(
        attribute=target.attribute_code,
        value=_rendered_value(target.attribute_code, target.value),
        before=before,
        rows=rows,
        item_texts=item_texts,
    )


@router.get("/{remediation_id}")
async def get_remediation(
    remediation_id: UUID,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> RemediationRead:
    """One remediation run's proven frontier (`remediation_id` = the run id). 404 if absent."""
    result = await _assemble_run(conn, remediation_id, get_master_key())
    if result is None:
        raise NotFound("remediation not found")
    return result


@router.get("")
async def list_remediations(
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    user_id: Annotated[UUID, Depends(get_current_user)],
    inference_id: UUID | None = None,
) -> list[RemediationRead]:
    """The frontier(s) for `inference_id`, newest first; a `cant_break` result when none localized.

    Without `inference_id` the list is empty for now (a global cursor list lands with M5.2). RLS
    hides another user's inferences, so a foreign id yields an empty list (no IDOR signal).
    """
    if inference_id is None:
        return []
    master_key = get_master_key()
    run_ids = await remediations_repo.latest_remediation_run_ids(conn, inference_id)
    if run_ids:
        assembled = [await _assemble_run(conn, run_id, master_key) for run_id in run_ids]
        return [result for result in assembled if result is not None]
    # no proven options persisted → surface the honest cant_break for the (existing) target.
    target = await inferences_repo.get_inference_target(conn, inference_id, master_key)
    if target is None or target.value is None:
        return []
    reliability = await inferences_repo.get_inference_reliability(conn, inference_id)
    before = (
        Reliability(point=float(reliability), lo=float(reliability), hi=float(reliability))
        if reliability is not None
        else _ZERO
    )
    return [
        assemble_remediation(
            attribute=target.attribute_code,
            value=_rendered_value(target.attribute_code, target.value),
            before=before,
            rows=[],
            item_texts={},
        )
    ]
