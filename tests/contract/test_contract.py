"""Schemathesis contract test (M5.1) — the published surface conforms to its OpenAPI.

Fuzzes every endpoint from the schema and validates responses against it. The DB-backed
/v1/runs + /v1/inferences endpoints are exercised by test_runs_api / test_inferences_api; the
remaining contract-first stubs return the documented 501 (the contract, not a crash).
Full fuzzing as a CI gate is M7.3.
"""

import uuid
from typing import Any

import schemathesis
from hypothesis import settings
from schemathesis import Case
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from app.main import app

_schema = schemathesis.openapi.from_asgi("/openapi.json", app)
# Exclude the DB-backed /v1/runs + /v1/inferences (covered by test_runs_api / test_inferences_api)
# and the infra probes /healthz, /readyz (need a live DB, not part of the v1 product contract).
_contract = _schema.exclude(path_regex=r"^/(v1/runs|v1/inferences|healthz|readyz)")
# Positive conformance only: responses match the declared schema, status, and content type.
# Negative/coverage probing (unsupported methods → RFC 9110 Allow headers, etc.) is M7.3's gate.
_POSITIVE_CHECKS = [
    response_schema_conformance,
    status_code_conformance,
    content_type_conformance,
]
_DEV_HEADERS = {"X-Dev-User-Id": str(uuid.uuid4())}


@_contract.parametrize()
@settings(max_examples=15, deadline=None)
def test_published_contract_conforms(case: Case[Any]) -> None:
    case.call_and_validate(headers=_DEV_HEADERS, checks=_POSITIVE_CHECKS)
